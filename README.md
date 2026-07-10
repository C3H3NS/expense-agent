# 交通费报销 AI 智能体

> 基于大语言模型的交通费报销自动审核系统，结合飞书审批流实现端到端自动化。

## 功能概述

员工在飞书提交交通费报销单 → AI 智能体自动完成：
1. **OCR 识别**：从发票图片中提取金额、日期、抬头等关键字段
2. **规则检查**：硬性规则校验（金额上限、抬头匹配、日期合理性）
3. **AI 审核**：Claude 做软判断（合理性分析、风险评估、异常模式检测）
4. **结果推送**：审核建议推送到飞书，审批人一键查看

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| Web 框架 | FastAPI | 接收飞书 Webhook，返回响应 |
| LLM | Anthropic Claude | 软判断：合理性分析、风险评估 |
| OCR | 百度云文字识别 | 增值税发票专用模型，识别率 >98% |
| 数据校验 | Pydantic | 结构化数据模型 |
| 审批流 | 飞书 Open API | 审批回调 + 消息推送 |

## 项目结构

```
expense-agent/
├── .env.example              # 配置模板
├── .gitignore
├── requirements.txt          # Python 依赖
├── config.py                 # 配置加载
├── main.py                   # FastAPI 入口
├── models.py                 # 数据模型定义
├── services/
│   ├── ocr_service.py        # 百度 OCR 封装
│   ├── rule_engine.py        # 规则引擎（硬判断）
│   ├── ai_reviewer.py        # LLM 审核（软判断）
│   └── feishu_service.py     # 飞书 API 封装
├── rules/
│   └── expense_rules.json    # 报销规则配置
├── prompts/
│   └── review_prompt.txt     # LLM Prompt 模板
└── tests/
    └── sample_invoices/      # 测试用发票图片
```

## 快速开始

### 1. 环境准备

```bash
# 克隆项目
git clone https://github.com/YOUR_USERNAME/expense-agent.git
cd expense-agent

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env，填入你的 API Key：
# - ANTHROPIC_API_KEY: https://console.anthropic.com/
# - BAIDU_API_KEY / BAIDU_SECRET_KEY: https://console.bce.baidu.com/
# - FEISHU_APP_ID / FEISHU_APP_SECRET: https://open.feishu.cn/
```

### 3. 运行

```bash
python main.py
```

访问 `http://localhost:8000/` 确认服务启动。

访问 `http://localhost:8000/debug/review-test` 触发一次测试审核流程。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 健康检查 |
| GET | `/debug/review-test` | 手动触发测试审核（开发调试用） |
| POST | `/webhook/feishu` | 飞书审批回调入口 |

## 审核流程

```
飞书提交报销单
    ↓
Webhook 触发
    ↓
OCR 识别发票图片 → 提取金额/日期/抬头/销售方
    ↓
规则引擎硬判断 → 金额上限/抬头匹配/日期合理性/月度累计
    ↓
Claude AI 软判断 → 合理性分析/异常模式检测/风险评级
    ↓
组装审核报告 → 推送飞书消息给审批人
```

## 规则配置

报销规则在 `rules/expense_rules.json` 中配置，修改规则不需要改代码：

```json
{
  "single_limit": { "limit": 500, "severity": "error" },
  "monthly_limit": { "limit": 3000, "severity": "warning" }
}
```

- `error`：硬性违规，建议驳回
- `warning`：提醒项，建议通过但关注

## License

MIT
