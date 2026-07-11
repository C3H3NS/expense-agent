# Phase 3 配置指南：百度 OCR + 飞书审批集成

本文档指导你完成两个外部服务的注册、配置和对接，然后进行真实测试。

---

## 整体架构

```
飞书审批提交 → 飞书事件订阅 → 你的服务器(webhook) → 下载发票图片
    → 百度OCR识别 → 规则引擎检查 → DeepSeek AI审核 → 推送结果到飞书群
```

你需要完成的配置：
1. **百度 OCR** — 发票图片识别服务
2. **飞书自建应用** — 审批事件回调 + 群机器人推送
3. **ngrok** — 内网穿透（让飞书能访问你本地的服务器）

---

## 一、百度 OCR 配置

### 1.1 注册百度智能云

1. 打开 https://console.bce.baidu.com/
2. 用百度账号登录（没有就注册一个）
3. 完成实名认证（个人认证即可，免费）

### 1.2 创建文字识别应用

1. 进入控制台，搜索「文字识别」或直接访问 https://console.bce.baidu.com/ai/#/ai/ocr/overview/index
2. 点击「创建应用」
3. 填写：
   - 应用名称：`expense-agent`
   - 应用描述：交通费报销发票识别
4. 勾选需要的接口（默认勾选「通用文字识别」即可，增值税发票识别也会自动包含）
5. 创建完成后，在应用列表中可以看到：
   - **API Key** — 复制这个
   - **Secret Key** — 复制这个

### 1.3 填入 .env

打开 `D:/projects/expense-agent/.env`，找到这两行：

```
BAIDU_API_KEY=
BAIDU_SECRET_KEY=
```

填入你的百度 API Key 和 Secret Key。

> **免费额度**：增值税发票识别每天 500 次免费调用，足够测试用。

---

## 二、飞书自建应用配置

### 2.1 创建应用

1. 打开 https://open.feishu.cn/app
2. 点击「创建企业自建应用」
3. 填写：
   - 应用名称：`报销审核助手`
   - 应用描述：自动审核交通费报销
4. 创建后进入应用详情页，记录：
   - **App ID** — 复制
   - **App Secret** — 复制

### 2.2 配置应用权限

进入「权限管理」，搜索并开通以下权限：

**必须开通的权限：**
- `im:message` — 发送消息
- `approval:approval` — 读取审批实例
- `approval:approval.instance:read` — 读取审批实例详情
- `drive:drive` — 读取云文档（用于下载审批附件）

**开通方式：** 搜索权限名称 → 点击开通 → 提交审核（企业自建应用通常秒过）

### 2.3 创建审批流程

1. 在飞书管理后台（https://feishu.cn/admin）进入「审批」
2. 点击「创建审批」
3. 创建一个「交通费报销」审批流程
4. 表单字段设置如下（**字段名称很重要，代码按名称关键词匹配**）：

| 字段名称 | 组件类型 | 说明 |
|----------|----------|------|
| 发票图片 | 附件 | 上传发票照片（必填） |
| 报销事由 | 单行文本 | 如"拜访客户往返" |
| 出差开始日期 | 日期 | 如 2026-07-08 |

> **关键**：字段名称中必须包含「发票」或「图片」关键词（代码靠这个匹配），事由字段必须包含「事由」关键词，日期字段必须包含「日期」关键词。

5. 审批流程设置：审批人可以随便设（比如自己审自己，只是为了测试）
6. 发布审批流程

### 2.4 配置事件订阅

1. 回到应用详情页 → 「事件订阅」
2. 请求地址填写：`https://你的ngrok地址/webhook/feishu`（ngrok 配置见第三节）
3. 添加事件：
   - 搜索 `approval` 关键词
   - 订阅「审批实例创建」事件（`approval_instance.create`）
4. 飞书会发送验证请求，你的服务器需要正确返回 challenge（代码已实现）
5. 记录 **Verification Token**（在事件订阅页面可以看到）

### 2.5 创建群机器人（用于推送结果）

1. 在飞书中创建一个群（比如叫「报销审核通知」）
2. 群设置 → 「群机器人」→「添加机器人」→「自定义机器人」
3. 机器人名称：`报销审核助手`
4. 创建后会得到一个 **Webhook 地址**，格式如：
   `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`
5. 复制这个地址

### 2.6 填入 .env

打开 `D:/projects/expense-agent/.env`，填入飞书配置：

```
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
FEISHU_BOT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx
FEISHU_VERIFICATION_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
```

### 2.7 发布应用

回到应用详情页：
1. 「版本管理与发布」→ 创建版本 → 提交发布
2. 企业自建应用一般秒过审
3. 确保应用状态为「已启用」

---

## 三、ngrok 内网穿透

飞书的事件订阅需要一个公网可访问的 URL。用 ngrok 把你本地的 8000 端口暴露出去。

### 3.1 安装 ngrok

1. 打开 https://ngrok.com/
2. 注册账号（免费）
3. 下载 ngrok 并解压
4. 在命令行运行：
   ```
   ngrok config add-authtoken 你的authtoken
   ```

### 3.2 启动隧道

```bash
ngrok http 8000
```

你会看到类似输出：
```
Forwarding   https://a1b2c3d4.ngrok-free.app -> http://localhost:8000
```

把这个 `https://a1b2c3d4.ngrok-free.app` 填到飞书事件订阅的请求地址里（第 2.4 步）。

> **注意**：ngrok 免费版每次重启 URL 会变，需要重新配置飞书事件订阅地址。如果需要固定 URL，可以升级 ngrok 付费版。

---

## 四、完整测试流程

### 前置检查

确保 `.env` 中以下配置都已填好：
- ✅ `LLM_API_KEY` — DeepSeek 密钥
- ✅ `BAIDU_API_KEY` + `BAIDU_SECRET_KEY` — 百度 OCR
- ✅ `FEISHU_APP_ID` + `FEISHU_APP_SECRET` — 飞书应用
- ✅ `FEISHU_BOT_WEBHOOK` — 飞书群机器人
- ✅ `FEISHU_VERIFICATION_TOKEN` — 飞书事件订阅验证

### 测试 1：模拟数据全流程（不依赖 OCR）

```bash
cd D:/projects/expense-agent
venv/Scripts/python.exe main.py
```

浏览器访问：http://localhost:8000/debug/review-test

✅ 预期：返回 JSON 报告，AI 审核结果为 pass/low

### 测试 2：真实发票 OCR 测试

找一张真实的加油发票照片，上传到图床（或用百度可访问的 URL）。

用 curl 测试：
```bash
curl -X POST http://localhost:8000/debug/ocr-test \
  -H "Content-Type: application/json" \
  -d '{"url": "https://你的发票图片URL.jpg", "applicant_name": "张三", "reason": "拜访客户"}'
```

✅ 预期：返回 OCR 识别结果 + 规则检查 + AI 审核报告

### 测试 3：飞书审批全流程

1. 确保 ngrok 正在运行（`ngrok http 8000`）
2. 确保飞书事件订阅地址已更新为最新的 ngrok URL
3. 确保服务正在运行（`venv/Scripts/python.exe main.py`）
4. 在飞书中提交一个交通费报销审批单，上传发票图片
5. 等待 2-5 秒
6. 检查飞书群是否收到审核结果消息

✅ 预期：飞书群收到审核报告卡片

---

## 五、常见问题

### Q: 飞书事件订阅验证失败？
A: 确保 ngrok 正在运行，且 URL 地址末尾是 `/webhook/feishu`。检查服务是否已启动。

### Q: OCR 识别失败？
A: 检查百度 API Key 和 Secret Key 是否正确。确认应用已开通「增值税发票识别」接口权限。

### Q: 图片下载失败？
A: 检查飞书应用是否开通了 `drive:drive` 权限。确认应用已发布且处于启用状态。

### Q: 没有收到飞书群消息？
A: 检查 `FEISHU_BOT_WEBHOOK` 是否正确。确认机器人还在群里（没有被移除）。

### Q: ngrok URL 变了怎么办？
A: 重新运行 `ngrok http 8000`，把新 URL 填到飞书事件订阅里，点击保存（飞书会重新发送验证请求）。
