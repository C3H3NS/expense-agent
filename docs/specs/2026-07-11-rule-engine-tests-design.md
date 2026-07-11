# 规则引擎单元测试设计

> **日期**: 2026-07-11
> **模块**: services/rule_engine.py
> **状态**: 已确认，待实现

## 1. 目标

为 `rule_engine.py` 编写 pytest 单元测试，覆盖所有规则分支、边界情况和异常处理。同时修复测试中发现的 2 个 bug。

## 2. 范围

- **包含**: 4 条单张规则（金额限额、抬头匹配、日期合理性、发票类型）+ 批量月度限额检查 + 边界异常
- **不包含**: OCR 服务、AI 审核、飞书对接（后续阶段）

## 3. 文件结构

```
D:/projects/expense-agent/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # pytest fixtures（发票数据工厂）
│   ├── test_rule_engine.py      # 规则引擎核心测试（15 个用例）
│   └── test_rule_engine_batch.py # 批量检查+月度限额测试
├── pytest.ini                   # pytest 配置
```

## 4. 环境搭建

- Python venv 创建在项目目录下 `venv/`
- 安装 `pytest`（项目 requirements.txt 需补充 dev 依赖）
- `pytest.ini` 配置:
  ```ini
  [pytest]
  testpaths = tests
  python_files = test_*.py
  ```

## 5. Fixtures 设计

### conftest.py

- `make_invoice()` — 工厂函数，接收 kwargs 覆盖默认值，生成 `OcrResult` 对象
  - 默认值: 金额 200, 抬头 "测试科技有限公司", 日期 3 天前, 类型 "加油票"
- `rules_path` — 返回项目自带 `rules/expense_rules.json` 的路径
- `engine` — 用 rules_path 初始化的 `RuleEngine` 实例

## 6. 测试用例清单

### 6.1 单张金额限额 (3 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_single_amount_within_limit` | 金额 200 | 通过，无警告 |
| `test_single_amount_at_limit` | 金额 500（边界） | 通过，无警告 |
| `test_single_amount_over_limit` | 金额 501 | 标记警告 |

### 6.2 抬头匹配 (3 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_company_name_exact_match` | 抬头完全匹配 | 通过 |
| `test_company_name_partial_match` | 抬头包含关键词 | 通过 |
| `test_company_name_mismatch` | 抬头不匹配 | 标记警告 |

### 6.3 日期合理性 (2 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_date_within_range` | 3 天前 | 通过 |
| `test_date_too_old` | 90 天前 | 标记警告 |

### 6.4 发票类型校验 (2 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_valid_invoice_type` | 加油票/停车费/过路费 | 通过 |
| `test_invalid_invoice_type` | 餐饮发票 | 标记警告 |

### 6.5 批量月度限额 (2 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_batch_under_monthly_limit` | 月累计 2000 < 3000 | 全通过 |
| `test_batch_over_monthly_limit` | 月累计 3500 > 3000 | 标记超额 |

### 6.6 边界 & 错误 (3 个)

| 测试函数 | 输入 | 预期 |
|---------|------|------|
| `test_empty_invoice_list` | 空列表 | 返回空结果 |
| `test_missing_fields_in_ocr` | OCR 缺字段 | 优雅降级不崩溃 |
| `test_rules_file_not_found` | 规则文件不存在 | 明确报错 |

**合计: 15 个测试用例**

## 7. Bug 修复

### Bug 1: JSON 配置键名不匹配

- **问题**: `rules/expense_rules.json` 用 `max_days_before_event`，代码读 `max_days_before`
- **修复**: 改代码侧，统一用 `max_days_before_event`
- **影响范围**: `rule_engine.py` 第 ~45 行

### Bug 2: monthly_spent_so_far 参数未使用

- **问题**: `check_single_invoice` 接收了 `monthly_spent_so_far` 但没用到
- **修复**: 在单张检查里加月度累计预检 — 当前金额 + 已报销 > 月度限额时标记警告
- **影响范围**: `rule_engine.py` `check_single_invoice` 方法

## 8. 验收标准

- [ ] `pytest tests/ -v` 全部 15 个测试通过
- [ ] 2 个 bug 已修复
- [ ] `requirements.txt` 包含 pytest dev 依赖
- [ ] 代码已提交到 git 并推送 GitHub
