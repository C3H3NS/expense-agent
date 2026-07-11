# Phase 3 调试排错记录

> 记录飞书集成阶段遇到的所有问题、原因和解决方案，供后续参考。

---

## 问题一：群机器人完全不发消息

### 症状
提交飞书审批后，webhook 没收到任何回调，群机器人无响应。

### 根因分析（3个原因，逐层排查）

#### 原因1：飞书事件类型选错

**错误操作**：在飞书开放平台「事件订阅」里订阅了 `approval.approval.created_v4`（审批定义创建）。

**正确操作**：应该订阅 **审批实例状态变更** 事件，事件类型为 `approval_instance`。

> 区别：`approval.approval.created_v4` 是"审批流程模板被创建"时触发的，跟员工提交报销单无关。员工提交报销单触发的是审批实例状态变更事件。

#### 原因2：未调用 subscribe API 订阅审批定义

**现象**：事件类型选对了，但飞书还是不推送事件。

**根因**：飞书审批事件需要**主动调用 subscribe API** 订阅指定审批定义，否则不会推送事件。

**解决方案**：在代码中新增 `/debug/subscribe-approval` 端点，调用：
```
POST https://open.feishu.cn/open-apis/approval/v4/approvals/{approval_code}/subscribe
```

其中 `approval_code` 是审批定义的唯一标识（在飞书审批管理后台的 URL 里可以找到，格式类似 `58EAD7E5-FADD-465F-8680-124833F13E2A`）。

调用一次后，该审批定义的事件就会开始推送到 webhook 地址。

#### 原因3：ngrok 版本过旧

**现象**：`ngrok http 8000` 启动报错 `authentication failed: your ngrok-agent version "3.3.1" is too old`。

**解决方案**：从 ngrok 官网下载 v3.39.9，替换旧版本。

---

## 问题二：群机器人重复发消息（发了两条）

### 症状
每次提交审批，群机器人发两条相同消息。

### 根因分析（3个原因叠加）

#### 原因1：DEBUG=true 导致 uvicorn 双进程

**根因**：`.env` 中 `DEBUG=true` → uvicorn 启动参数 `reload=True` → uvicorn 启动两个进程：
- **reloader 进程**：监控文件变化
- **worker 进程**：实际处理请求

两个进程都注册了 FastAPI 路由，都接收并处理了飞书回调，导致每条消息发两次。

**日志证据**：每行日志都出现两次（同一时间戳、同一内容）：
```
2026-07-11 18:29:13.649 | INFO | services.rule_engine:batch_check:242 - [Rules] 检查完成...
2026-07-11 18:29:13.649 | INFO | services.rule_engine:batch_check:242 - [Rules] 检查完成...
```

**解决方案**：`.env` 改为 `DEBUG=false`，关闭 reload 模式，只启动单进程。

#### 原因2：同步处理超时导致飞书重试

**根因**：webhook 处理链路耗时 5-7 秒（下载图片→OCR→规则→AI→推送），飞书 webhook 有 **3 秒超时**限制，超时后飞书会**重试投递**同一个事件，导致重复处理。

**解决方案**：改为**异步处理**：
```python
# 立即启动后台任务，不阻塞响应
task = asyncio.create_task(_process_approval_async(...))

# 立即返回200（<0.3秒），飞书不会重试
return JSONResponse({"code": 0, "msg": "accepted"})
```

后台任务跑完整链路（5-7秒），但 webhook 已经返回了，飞书不会重试。

#### 原因3：loguru 日志 sink 重复添加（非功能bug，但干扰排查）

**根因**：`main.py` 被加载两次（一次作为 `__main__` 直接运行，一次被 uvicorn 作为 `main:app` 导入），`logger.add("logs/app.log")` 被调用两次，导致同一条日志写两遍。

**解决方案**：用模块级标记防止重复添加：
```python
import sys as _sys
if not getattr(_sys, "_file_sink_added", False):
    logger.add("logs/app.log", rotation="10 MB", retention="7 days")
    _sys._file_sink_added = True
```

### 验证结果

三个修复全部应用后（18:54 测试）：
- ✅ 只发一条消息
- ✅ 日志全部单条
- ✅ webhook 0.2秒返回，后台任务 4.76秒完成
- ✅ 飞书未重试

---

## 问题三：审批实例事件不含表单数据

### 症状
webhook 收到了飞书回调，但事件体里没有发票图片、事由等表单数据。

### 根因
飞书审批实例事件（`approval_instance` 类型）的回调体**只包含元数据**：
```json
{
  "instance_code": "82AB1298-...",
  "approval_code": "58EAD7E5-...",
  "status": "PENDING",
  "type": "approval_instance"
}
```

不包含表单内容（图片、事由、日期等）。

### 解决方案
通过 API 兜底拉取审批实例详情：
```
GET https://open.feishu.cn/open-apis/approval/v4/instances/{instance_code}
```

返回的 `form` 字段是 JSON 字符串，解析后通过字段 name 关键词匹配提取：
- 图片：name 包含 "发票/图片/附件/票据"，value 是逗号分隔的 URL 列表
- 事由：name 包含 "事由/原因/说明"
- 日期：name 包含 "日期/时间/出差"

新增方法：
- `FeishuService.get_instance_detail(instance_code)` — 调 API 获取实例详情
- `FeishuService.extract_form_from_instance(detail)` — 解析 form 提取表单字段
- `FeishuService.download_images_from_urls(urls)` — 从 URL 列表下载图片

---

## 问题四：飞书延迟重投旧事件

### 症状
用户未提交审批，但群机器人突然发了一条消息。

### 根因
之前提交的审批（约17:44），当时服务器正在重启（18:29~18:36 多次重启），飞书 webhook 投递失败。飞书会**按指数退避策略重试**，约65分钟后（18:49）重试成功，送达了这条旧事件。

### 识别方法
对比事件体中的两个时间戳：
- `instance_operate_time`：审批实际提交时间（毫秒）
- `ts`：事件投递时间（秒）

两者差值大于几分钟，说明是延迟重投的旧事件。

### 结论
这不是 bug，是飞书正常的事件重试机制。服务器稳定运行后不会再出现。

---

## 排查方法论总结

| 步骤 | 方法 | 工具 |
|------|------|------|
| 1. 看日志 | `tail -200 logs/app.log` | 确认 webhook 是否收到回调 |
| 2. 看重复 | 日志同一时间戳出现两次 | 双进程 or 飞书重试 |
| 3. 看时间戳 | 对比 `instance_operate_time` 和 `ts` | 区分实时事件 vs 延迟重投 |
| 4. 看进程 | `Get-Process python*` | 确认是否双进程 |
| 5. 看返回速度 | webhook 响应时间 | 超过3秒飞书会重试 |

### 关键经验
1. **飞书 webhook 3秒超时** — 长耗时任务必须异步处理，先返回200
2. **DEBUG=false 在生产** — uvicorn reload 模式会启动双进程
3. **飞书审批事件需要主动 subscribe** — 光在开放平台配置事件订阅不够，还要调 API
4. **审批实例事件不含表单** — 必须二次调 API 拉取实例详情
5. **飞书会重试失败的事件** — 服务器不稳定时积累的事件会在恢复后集中送达
