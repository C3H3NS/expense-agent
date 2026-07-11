# 交通费报销 AI 智能体 (Expense-Agent)

> 员工在飞书提交交通费报销审批 → 自动 OCR 识别发票 → 规则引擎硬判断 + AI 软判断 → 审核建议推送到飞书群

全链路耗时约 5 秒，从飞书审批提交到群机器人推送消息。

---

## 目录

- [架构总览](#架构总览)
- [目录结构](#目录结构)
- [核心模块详解](#核心模块详解)
- [一次请求的完整流程](#一次请求的完整流程)
- [配置说明 (.env)](#配置说明-env)
- [API 接口](#api-接口)
- [本地运行](#本地运行)
- [日志与排错](#日志与排错)
- [测试](#测试)
- [设计决策](#设计决策)
- [部署 Checklist](#部署-checklist)

---

## 架构总览

```
飞书审批提交
  ↓ event callback
main.py (Webhook Handler)
  ↓ 立即返回200 + asyncio.create_task 后台异步处理
  ├──→ feishu_service.py  →  API拉取审批实例详情 + 下载发票图片
  ├──→ ocr_service.py     →  百度OCR识别 (图片→结构化数据)
  ├──→ rule_engine.py     →  规则硬判断 (金额/抬头/日期/类型)
  ├──→ ai_reviewer.py     →  DeepSeek AI软判断 (合理性/风险)
  └──→ feishu_service.py  →  组装报告 + 群机器人推送卡片消息
```

**技术栈**: FastAPI + httpx + Pydantic + loguru + DeepSeek(OpenAI兼容) + 百度OCR + 飞书Open API

**为什么不用 LangChain**: 场景是线性流水线（OCR→规则→AI→推送），没有多步推理/工具选择/记忆管理，直接调 API + FastAPI 400 行搞定，每一行都看得懂。

---

## 目录结构

```
expense-agent/
├── main.py                    # FastAPI 入口，Webhook 接收 + 异步处理链
├── config.py                  # 配置加载（从 .env 读取，Pydantic 校验）
├── models.py                  # 所有数据模型定义（OcrResult/RuleCheckResult/AiReviewReport等）
├── requirements.txt           # Python 依赖（8个核心 + 1个测试）
├── .env.example               # 配置模板（复制为 .env 后填入密钥）
│
├── services/                  # 四个核心服务模块
│   ├── ocr_service.py         # 百度OCR封装：图片→OcrResult
│   ├── rule_engine.py         # 规则引擎：OcrResult→BatchRuleCheckResult
│   ├── ai_reviewer.py         # AI审核：规则结果+OCR→AiReviewReport
│   └── feishu_service.py      # 飞书API封装：回调解析/Token管理/图片下载/消息推送
│
├── rules/
│   └── expense_rules.json     # 报销规则配置（改规则不改代码）
│
├── prompts/
│   └── review_prompt.txt      # LLM Prompt 模板（用 {{变量}} 占位）
│
├── tests/                     # 单元测试（34个用例）
│   ├── conftest.py            # pytest fixtures
│   ├── test_rule_engine.py    # 规则引擎测试
│   ├── test_rule_engine_batch.py
│   ├── test_rule_engine_edge.py
│   ├── test_ai_reviewer.py    # AI审核测试
│   └── sample_invoices/       # 测试用发票图片
│
├── docs/                      # 文档
│   ├── phase3-setup-guide.md      # 飞书+百度OCR+ngrok 配置指南
│   └── phase3-troubleshooting.md  # Phase3 调试排错记录（4个问题+解决方案）
│
└── logs/                      # 运行日志（自动生成，10MB轮转，保留7天）
    └── app.log
```

---

## 核心模块详解

### 1. `main.py` — FastAPI 入口

**职责**: 接收飞书 Webhook 回调，协调四个服务模块完成审核流程。

**关键设计**:
- **异步处理**: Webhook 收到请求后立即返回 HTTP 200（<0.3秒），审核流程在 `asyncio.create_task` 后台跑（5-7秒）。如果同步处理超过3秒，飞书会超时重试，导致重复消息。
- **旧事件过滤**: 对比 `instance_operate_time`（审批提交时间）和当前时间，超过10分钟的旧事件直接跳过。飞书对未送达的事件会持续重试（可能延迟65分钟），调试期间积压的事件会被自动过滤。
- **实例去重**: `_pending_tasks` 字典防止同一个审批实例被重复创建后台任务。

**全局变量**:
```python
ocr_service = OcrService()       # 百度OCR，全局复用（Token缓存）
rule_engine = RuleEngine()       # 规则引擎，启动时加载JSON规则
ai_reviewer = AiReviewer()       # AI审核，全局复用OpenAI client
feishu_service = FeishuService() # 飞书API，全局复用（Token缓存）

_processed_instances: dict = {}  # {instance_code: timestamp} 去重
_pending_tasks: dict = {}        # {instance_code: asyncio.Task} 后台任务追踪
```

**关键函数**:
| 函数 | 作用 |
|------|------|
| `feishu_webhook()` | Webhook 入口，验签→过滤→去重→启动后台任务→返回200 |
| `_process_approval_async()` | 后台异步处理全链路（6个Step），用 `short_id` 标记每条日志 |
| `_assemble_report()` | 把 OCR/规则/AI 三个结果组装成 `FinalReviewReport` |

---

### 2. `config.py` — 配置管理

**职责**: 从 `.env` 文件加载所有配置，启动时检查关键 Key。

**设计**:
- LLM Key 缺失 → 强警告（AI审核不可用）
- 百度OCR/飞书 Key 缺失 → 弱提示（对应功能不可用，但不阻止启动）
- 所有配置通过 `settings.xxx` 访问，不在其他文件写死任何值

**LLM 可无缝切换**: 修改 `.env` 的 `LLM_BASE_URL` 和 `LLM_MODEL` 即可：
| 模型 | LLM_BASE_URL | LLM_MODEL |
|------|-------------|-----------|
| DeepSeek（默认） | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |

---

### 3. `models.py` — 数据模型

**职责**: 所有业务数据的结构定义，用 Pydantic BaseModel。修改数据格式只改这里。

**数据流**:
```
OcrResult ← 百度OCR返回值解析
     ↓
BatchRuleCheckResult ← 规则引擎检查
     ↓
AiReviewReport ← DeepSeek AI审核
     ↓
FinalReviewReport ← 组装三者，推送给飞书
```

**关键模型**:
| 模型 | 说明 | 关键字段 |
|------|------|----------|
| `OcrResult` | 单张发票OCR结果 | invoice_type, amount, seller_name, buyer_name, issue_date, confidence |
| `RuleViolation` | 单条违规 | rule_name, description, severity(error/warning) |
| `BatchRuleCheckResult` | 整批发票检查结果 | total_amount, overall_status(passed/warning/rejected), per_invoice_results |
| `AiReviewReport` | AI审核报告 | risk_level(low/medium/high), action(pass/warn/reject), reason |
| `FinalReviewReport` | 最终推送给飞书的报告 | instance_code, invoice_details, rule_summary, ai_action, risk_emoji |
| `EmployeeContext` | 员工上下文（当前为占位，后续接入HR系统） | name, department, recent_expense_count_3m |

**枚举**:
- `InvoiceType`: 加油票 / 停车票 / 过路费票
- `RiskLevel`: low / medium / high
- `ReviewAction`: pass / warn / reject
- `Severity`: error（必须驳回）/ warning（提醒即可）

---

### 4. `services/ocr_service.py` — 百度 OCR 封装

**职责**: 把发票图片变成结构化的 `OcrResult`。

**工作流程**:
1. `_get_access_token()` — 获取百度OAuth Token（30天有效，自动缓存）
2. `_call_ocr_api()` — 调用百度 `vat_invoice` 接口（支持 URL 和 base64 两种输入）
3. `_parse_ocr_result()` — 解析百度返回的JSON，映射到 `OcrResult`

**两种调用方式**:
| 方法 | 输入 | 场景 |
|------|------|------|
| `recognize(image_url)` | 公网URL | 调试接口 `/debug/ocr-test` |
| `recognize_bytes(image_data)` | 二进制bytes | 飞书下载的图片（非公开URL） |
| `batch_recognize(urls)` | URL列表 | 批量 |
| `batch_recognize_bytes(data_list)` | bytes列表 | 飞书主流程用这个 |

**关键细节**:
- 百度OCR返回**英文字段名**（`SellerName`/`AmountInFiguers`/`InvoiceDate`），不是中文
- `get_field()` 兼容三种返回格式：字符串 / 字典 `{word: "xxx"}` / 列表
- 并发控制：`asyncio.Semaphore(2)` 限制2并发（百度免费版QPS=2）
- 置信度 <0.85 会打 warning 日志

---

### 5. `services/rule_engine.py` — 规则引擎

**职责**: 硬判断，只做能写成 if/else 的条件检查。规则配置外置在 `rules/expense_rules.json`。

**五条规则**:
| 规则 | 检查内容 | severity |
|------|----------|----------|
| 单张金额上限 | `amount > 500` | error |
| 发票抬头匹配 | `buyer_name` 不含公司名 | error |
| 日期合理性 | 开票日期>今天 或 早于事由7天前 | error |
| 发票类型允许 | 类型不在 [加油票/停车票/过路费票] | warning |
| 月度累计上限 | `total + monthly_spent_so_far > 3000` | warning |

**整体状态判定**:
- 有 error → `rejected`
- 只有 warning → `warning`
- 全通过 → `passed`

**关键方法**:
- `check_single_invoice(ocr, index, event_start_date, monthly_spent_so_far)` — 检查单张
- `batch_check(ocr_results, event_start_date)` — 批量检查 + 汇总

---

### 6. `services/ai_reviewer.py` — AI 审核

**职责**: 软判断，让 LLM 做规则做不到的事（合理性分析、异常模式检测、风险评级）。

**工作流程**:
1. `_load_prompt_template()` — 从 `prompts/review_prompt.txt` 加载模板
2. `_build_messages()` — 把 OCR结果/规则结果/员工信息填入模板的 `{{变量}}`
3. 调用 OpenAI 兼容接口（默认 DeepSeek）
4. `_parse_ai_response()` — 解析 LLM 返回的 JSON（容错：去 markdown 标记、提取 JSON 块）
5. LLM 不可用时 `_fallback_to_rules()` 降级到规则引擎结果

**Prompt 设计要点** (见 `prompts/review_prompt.txt`):
- 身份设定："你不是最终决策者，你是审批人的副驾驶"
- 判断维度：金额合理性 / 时间一致性 / 行为模式 / 事由可信度
- 三个示例（正常/提醒/有问题），few-shot 引导输出格式
- 严格 JSON 输出：`{risk_level, action, reason, concerns}`

**降级方案** (LLM API 不可用时):
| 规则状态 | 降级action | 降级risk |
|----------|-----------|----------|
| rejected | reject | high |
| warning | warn | medium |
| passed | pass | low |

---

### 7. `services/feishu_service.py` — 飞书 API 封装

**职责**: 四件事 — 回调解析、Token管理、附件下载、消息推送。

**Token 管理**:
- `_get_tenant_token()` — 获取飞书 Tenant Access Token（2小时有效，自动缓存）
- 飞书所有 API 调用都需要 Bearer Token

**回调解析** (`parse_approval_webhook`):
- 静态方法，解析飞书 V2 事件格式的 Webhook 请求体
- 提取 `instance_code` / `event_type` / `instance_operate_time`
- 注意：飞书审批实例事件**不含表单数据**，只有元数据

**审批实例详情** (`get_instance_detail` + `extract_form_from_instance`):
- 调 `GET /open-apis/approval/v4/instances/{instance_code}` 拉取完整详情
- `form` 是 JSON 字符串，需 `json.loads` 解析
- 图片字段：靠 name 关键词匹配（"发票/图片/附件/票据"），value 是 URL 列表
- 事由字段：靠 name 匹配（"事由/原因/说明/备注"）
- 日期字段：靠 name 匹配（"日期/时间/出差"）

**图片下载**:
| 方法 | 场景 |
|------|------|
| `download_form_image(file_token)` | 通过飞书API下载附件（file_token方式） |
| `download_images_from_urls(urls)` | 通过临时URL下载（实际主流程用这个） |

**消息推送** (`send_review_message`):
- 方式A（当前使用）: 通过群机器人 Webhook 发送交互式卡片消息
- 方式B（备选）: 通过飞书 Open API 发送给指定用户/群组
- 卡片颜色根据风险等级：low=绿色 / medium=橙色 / high=红色

**事件订阅** (`subscribe_approval`):
- 调 `POST /open-apis/approval/v4/approvals/{approval_code}/subscribe`
- 飞书要求调用一次此API才能收到审批事件回调
- 通过 `/debug/subscribe-approval` 端点手动触发

---

## 一次请求的完整流程

以员工提交一张加油票 ¥22.9 为例：

```
1. 员工在飞书审批表单上传发票图片，填写事由"拜访客户往返"，提交
   ↓ 飞书推 event_callback 到 webhook URL

2. main.py: feishu_webhook() 收到请求
   → 解析 instance_code = "82AB1298-..."
   → 检查 instance_operate_time，2秒前提交，时效检查通过
   → 检查 _pending_tasks，无重复
   → asyncio.create_task() 启动后台处理
   → 立即返回 {"code":0, "msg":"accepted"}  ← 飞书收到200，不再重试

3. 后台任务 _process_approval_async() 执行：
   ━━━━━━━━━━ [82AB1298] 开始处理审批 ━━━━━━━━━━

   Step 1/6: API拉取审批实例
   → feishu_service.get_instance_detail("82AB1298-...")
   → feishu_service.extract_form_from_instance(detail)
   → 提取到 1张图片URL, 事由="拜访客户往返", 日期="2019-06-09"

   Step 2/6: 下载图片
   → feishu_service.download_images_from_urls(urls)
   → 下载成功 256783 bytes (251KB)

   Step 3/6: OCR识别
   → ocr_service.batch_recognize_bytes([image_data])
   → 图片转base64 → 发给百度 vat_invoice API
   → 返回: 加油票 | ¥22.9 | 成都京东世纪贸易有限公司 | 置信度0.95

   Step 4/6: 规则检查
   → rule_engine.batch_check(ocr_results, event_start_date)
   → 金额22.9 < 500 ✓ | 抬头不匹配 ✗ | 日期早于事由 ✗
   → 状态=rejected, 违规2项

   Step 5/6: AI审核
   → ai_reviewer.review(ocr_results, rule_result, employee, reason)
   → 构建 Prompt → 调 DeepSeek API → 解析JSON返回
   → action=warn, risk_level=medium
   → 耗时~2秒, 消耗~830 tokens

   Step 6/6: 群机器人推送
   → _assemble_report() 组装 FinalReviewReport
   → feishu_service.send_review_message(report)
   → 发送橙色卡片消息到飞书群

   ━━━━━━━━━━ [82AB1298] 实例处理结束 | 总耗时 4.76s ━━━━━━━━━━
```

---

## 配置说明 (.env)

```ini
# ===== 应用配置 =====
APP_NAME=Expense-Agent
DEBUG=false                   # 生产必须false！true会导致uvicorn双进程
PORT=8000

# ===== LLM 配置 =====
LLM_API_KEY=sk-xxx            # DeepSeek API Key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat

# ===== 百度 OCR =====
BAIDU_API_KEY=xxx             # 百度智能云控制台获取
BAIDU_SECRET_KEY=xxx

# ===== 飞书配置 =====
FEISHU_APP_ID=cli_xxx         # 飞书开放平台 → 应用详情
FEISHU_APP_SECRET=xxx         # 同上
FEISHU_BOT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx  # 群机器人Webhook
FEISHU_VERIFICATION_TOKEN=xxx # 事件订阅页面的 Verification Token

# ===== 规则配置 =====
COMPANY_NAME=XX科技有限公司     # 发票抬头匹配用
EXPENSE_MONTHLY_LIMIT=3000    # 月度累计上限
EXPENSE_SINGLE_LIMIT=500      # 单张金额上限
EXPENSE_DATE_RANGE_DAYS=7     # 发票日期距事由的最大天数
```

---

## API 接口

| 方法 | 路径 | 说明 | 上线处理 |
|------|------|------|----------|
| GET | `/` | 健康检查，返回 `{"status":"ok"}` | 保留 |
| POST | `/webhook/feishu` | 飞书审批回调入口 | 保留 |
| GET | `/debug/review-test` | 模拟数据测试审核流程（不调OCR） | **删除或加auth** |
| POST | `/debug/ocr-test` | 真实图片测试 OCR→规则→AI 全流程 | **删除或加auth** |
| POST | `/debug/subscribe-approval` | 订阅飞书审批事件 | **删除或加auth** |

---

## 本地运行

```bash
# 1. 克隆 + 虚拟环境
git clone https://github.com/C3H3NS/expense-agent.git
cd expense-agent
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Linux/Mac

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置
cp .env.example .env
# 编辑 .env 填入 API Key（至少 LLM_API_KEY）

# 4. 启动
python main.py
# 或: venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1

# 4. 验证
curl http://localhost:8000/
# {"status":"ok","service":"Expense-Agent"}

# 5. 内网穿透（飞书Webhook需要公网URL）
ngrok http 8000
# 把生成的 https://xxx.ngrok-free.dev/webhook/feishu 填入飞书事件订阅地址
```

---

## 日志与排错

### 日志文件

```
logs/app.log    # loguru格式，10MB轮转，保留7天
```

### 日志格式

每个审批实例的日志用 `━━━` 分隔线标记生命周期，短ID（前8位）贯穿所有日志：

```
[Webhook] 收到审批事件 | 实例=82AB1298 | 全ID=82AB1298-15A4-... | 事件类型=approval_instance
[Webhook] 时效检查通过 | 实例=82AB1298 | 审批提交于2秒前
[Webhook] 后台任务已启动 | 实例=82AB1298
━━━━━━━━━━ [82AB1298] 开始处理审批 ━━━━━━━━━━
[82AB1298] Step 1/6 API拉取审批实例...
[82AB1298] Step 1/6 完成 | 1张图片, 事由=拜访客户往返, 日期=2019-06-09
[82AB1298] Step 2/6 图片下载 | 1/1成功 (250KB)
[82AB1298] Step 3/6 OCR识别 | 1/1成功 | 加油票 ¥22.9
[82AB1298] Step 4/6 规则检查 | 状态=rejected, 违规2项
[82AB1298] Step 5/6 AI审核 | 动作=warn, 风险=medium
[82AB1298] Step 6/6 群机器人推送 | 已发送
━━━━━━━━━━ [82AB1298] 实例处理结束 | 总耗时 4.76s ━━━━━━━━━━
```

### 常见问题排查

| 症状 | 排查方向 | 详见 |
|------|----------|------|
| 群机器人不发消息 | 事件类型/subscribe API/ngrok版本 | [排错文档-问题一](docs/phase3-troubleshooting.md) |
| 群机器人发两条消息 | DEBUG=true双进程/同步超时/日志重复 | [排错文档-问题二](docs/phase3-troubleshooting.md) |
| 事件不含表单数据 | 飞书审批实例事件只含元数据，需API拉取 | [排错文档-问题三](docs/phase3-troubleshooting.md) |
| 没人提交却收到消息 | 飞书延迟重投旧事件，已被10分钟过滤跳过 | [排错文档-问题四](docs/phase3-troubleshooting.md) |

### 实时跟踪日志

```bash
# Windows PowerShell
Get-Content D:\projects\expense-agent\logs\app.log -Wait -Tail 20

# Linux/Mac
tail -f logs/app.log
```

---

## 测试

```bash
# 运行全部测试
venv\Scripts\python.exe -m pytest tests/ -v

# 当前: 34个测试用例全通过
# - test_rule_engine.py: 金额/抬头/日期/类型 (8个)
# - test_rule_engine_batch.py: 批量检查 (2个)
# - test_rule_engine_edge.py: 边界&异常 (3个)
# - test_ai_reviewer.py: JSON解析/降级/Prompt构建/Mock (17个)
```

---

## 设计决策

### 为什么规则引擎和AI分开？

- **规则引擎**管硬约束（金额>500必须驳回），确定性强，可测试，不依赖外部API
- **AI**管软判断（连续多笔接近上限的加油票是否在拆分报销），需要语义理解
- 各司其职，不越界。AI不可用时降级到规则引擎结果

### 为什么用异步处理？

飞书Webhook有3秒超时，超过就重试。OCR+AI审核全链路需要5-7秒。
如果同步处理，飞书会重试3次，导致群机器人发3条消息。
异步处理：Webhook立即返回200，审核在后台跑完只推送1次。

### 为什么 DEBUG=false？

`DEBUG=true` → uvicorn `reload=True` → 启动两个进程（reloader + worker），
两个进程都会处理请求，导致每条消息发两次。生产环境必须 `false`。

### 为什么用 `sys._file_sink_added` 标记？

loguru 的 `logger.add()` 会在每次调用时添加一个新的文件输出 sink。
`main.py` 会被导入两次（`__main__` 和 `main:app`），导致 `logger.add()` 执行两次，
同一条日志写入文件两行。用标记确保只添加一次。

---

## 部署 Checklist

- [ ] 所有 API Key 换成正式环境的
- [ ] `.env` 文件权限设为 600
- [ ] `DEBUG=false`
- [ ] `/debug/*` 接口删除或加 IP 白名单
- [ ] 飞书事件订阅地址改为正式域名（去掉 ngrok）
- [ ] SSL 证书配置（飞书强制 HTTPS）
- [ ] 进程管理工具（systemd / supervisor / docker）
- [ ] 端到端测试通过
- [ ] 监控告警配置（进程挂了要通知）

---

## License

MIT
