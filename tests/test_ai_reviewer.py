"""AI 审核器单元测试 —— 不调真实 API，只测内部逻辑

测试覆盖：
1. _parse_ai_response: JSON 解析容错（干净JSON/markdown包裹/前后有文字/格式错误）
2. _fallback_to_rules: API 不可用时的降级方案
3. _build_messages: Prompt 构建逻辑 + invoice_type=None 防御
"""
import json
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

from models import (
    OcrResult, InvoiceType, BatchRuleCheckResult, RuleCheckResult,
    RuleViolation, Severity, RiskLevel, ReviewAction, EmployeeContext,
)
from services.ai_reviewer import AiReviewer


# ===== Fixture =====

@pytest.fixture
def reviewer():
    """AiReviewer 实例（OpenAI client 不会发起真实请求，安全实例化）"""
    return AiReviewer()


@pytest.fixture
def make_ocr():
    """发票数据工厂"""
    def _make(**kwargs) -> OcrResult:
        defaults = {
            "invoice_type": InvoiceType.GAS,
            "amount": 200.0,
            "buyer_name": "测试科技有限公司",
            "seller_name": "中国石化加油站",
            "issue_date": date.today() - timedelta(days=3),
            "confidence": 0.95,
        }
        defaults.update(kwargs)
        return OcrResult(**defaults)
    return _make


@pytest.fixture
def passed_rule_result():
    """规则全部通过的 BatchRuleCheckResult"""
    return BatchRuleCheckResult(
        total_amount=400.0,
        invoice_count=2,
        monthly_total=400.0,
        per_invoice_results=[
            RuleCheckResult(invoice_index=0, is_passed=True, violations=[], summary="ok"),
            RuleCheckResult(invoice_index=1, is_passed=True, violations=[], summary="ok"),
        ],
        overall_status="passed",
    )


@pytest.fixture
def rejected_rule_result():
    """规则检测到 error 的 BatchRuleCheckResult"""
    return BatchRuleCheckResult(
        total_amount=600.0,
        invoice_count=1,
        monthly_total=600.0,
        per_invoice_results=[
            RuleCheckResult(
                invoice_index=0,
                is_passed=False,
                violations=[RuleViolation(
                    rule_name="单张金额上限",
                    description="单张发票金额 600元超过上限500元",
                    severity=Severity.ERROR,
                    actual_value=600.0,
                    limit_value=500.0,
                )],
                summary="error",
            ),
        ],
        overall_status="rejected",
    )


# ===== 1. JSON 解析容错 =====

class TestParseAiResponse:
    """_parse_ai_response: LLM 返回的各种格式容错"""

    def test_clean_json(self, reviewer):
        """干净的 JSON 字符串 → 正常解析"""
        resp = '{"risk_level":"low","action":"pass","reason":"正常","concerns":[]}'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.LOW
        assert report.action == ReviewAction.PASS
        assert report.reason == "正常"

    def test_json_in_markdown_block(self, reviewer):
        """JSON 被 ```json ... ``` 包裹 → 正常解析"""
        resp = '```json\n{"risk_level":"high","action":"reject","reason":"违规","concerns":["金额异常"]}\n```'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.HIGH
        assert report.action == ReviewAction.REJECT
        assert "金额异常" in report.concerns

    def test_json_in_plain_code_block(self, reviewer):
        """JSON 被 ``` ... ``` 包裹（无 json 标记）→ 正常解析"""
        resp = '```\n{"risk_level":"medium","action":"warn","reason":"注意","concerns":["频率高"]}\n```'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.MEDIUM
        assert report.action == ReviewAction.WARN

    def test_json_with_surrounding_text(self, reviewer):
        """JSON 前后有多余文字 → 提取 JSON 部分解析"""
        resp = '根据分析，结果如下：\n{"risk_level":"low","action":"pass","reason":"OK","concerns":[]}\n以上是审核结果。'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.LOW
        assert report.action == ReviewAction.PASS

    def test_malformed_json_fallback(self, reviewer):
        """格式错误的 JSON → 降级为 MEDIUM/WARN，reason 提示人工复核"""
        resp = '这不是JSON，就是一段乱七八糟的文字'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.MEDIUM
        assert report.action == ReviewAction.WARN
        assert "人工复核" in report.reason

    def test_missing_fields_use_defaults(self, reviewer):
        """JSON 缺少部分字段 → 使用默认值，不崩溃"""
        resp = '{"risk_level":"low"}'
        report = reviewer._parse_ai_response(resp)
        assert report.risk_level == RiskLevel.LOW
        assert report.action == ReviewAction.PASS  # 默认值
        assert report.reason == ""  # 默认值
        assert report.concerns == []  # 默认值


# ===== 2. 降级方案 =====

class TestFallbackToRules:
    """_fallback_to_rules: LLM 不可用时的降级逻辑"""

    def test_fallback_when_rejected(self, reviewer, rejected_rule_result):
        """规则判定 rejected → 降级为 REJECT/HIGH"""
        report = reviewer._fallback_to_rules(rejected_rule_result)
        assert report.action == ReviewAction.REJECT
        assert report.risk_level == RiskLevel.HIGH
        assert "规则" in report.reason

    def test_fallback_when_warning(self, reviewer):
        """规则判定 warning → 降级为 WARN/MEDIUM"""
        rule_result = BatchRuleCheckResult(overall_status="warning")
        report = reviewer._fallback_to_rules(rule_result)
        assert report.action == ReviewAction.WARN
        assert report.risk_level == RiskLevel.MEDIUM

    def test_fallback_when_passed(self, reviewer, passed_rule_result):
        """规则判定 passed → 降级为 PASS/LOW"""
        report = reviewer._fallback_to_rules(passed_rule_result)
        assert report.action == ReviewAction.PASS
        assert report.risk_level == RiskLevel.LOW

    def test_fallback_has_concerns(self, reviewer, passed_rule_result):
        """降级报告应包含"AI不可用"的 concerns 提示"""
        report = reviewer._fallback_to_rules(passed_rule_result)
        assert len(report.concerns) > 0
        assert any("不可用" in c for c in report.concerns)


# ===== 3. Prompt 构建 =====

class TestBuildMessages:
    """_build_messages: 组装发送给 LLM 的消息"""

    def test_prompt_contains_invoice_details(self, reviewer, make_ocr, passed_rule_result):
        """Prompt 中应包含发票金额、卖方名称"""
        ocr_results = [
            make_ocr(amount=380.0, seller_name="中石化望京加油站"),
            make_ocr(amount=290.0, seller_name="中国石油朝阳路加油站"),
        ]
        messages = reviewer._build_messages(ocr_results, passed_rule_result)
        prompt_text = messages[0]["content"]
        assert "380.00" in prompt_text
        assert "290.00" in prompt_text
        assert "中石化望京加油站" in prompt_text

    def test_prompt_contains_rule_summary(self, reviewer, make_ocr, rejected_rule_result):
        """Prompt 中应包含规则违规摘要"""
        messages = reviewer._build_messages([make_ocr()], rejected_rule_result)
        prompt_text = messages[0]["content"]
        assert "单张金额上限" in prompt_text or "error" in prompt_text.lower()

    def test_prompt_with_invoice_type_none(self, reviewer, make_ocr, passed_rule_result):
        """invoice_type=None → Prompt 中显示'未知'，不崩溃"""
        ocr_results = [make_ocr(invoice_type=None)]
        messages = reviewer._build_messages(ocr_results, passed_rule_result)
        prompt_text = messages[0]["content"]
        assert "未知" in prompt_text

    def test_prompt_contains_employee_context(self, reviewer, make_ocr, passed_rule_result):
        """Prompt 中应包含申请人信息"""
        employee = EmployeeContext(
            name="张三", department="销售部", position="客户经理",
            recent_expense_count_3m=5, recent_expense_total_3m=2000,
        )
        messages = reviewer._build_messages([make_ocr()], passed_rule_result, employee)
        prompt_text = messages[0]["content"]
        assert "张三" in prompt_text
        assert "销售部" in prompt_text

    def test_prompt_no_employee(self, reviewer, make_ocr, passed_rule_result):
        """没有 employee_ctx → Prompt 中申请人显示'未知'，不崩溃"""
        messages = reviewer._build_messages([make_ocr()], passed_rule_result, None)
        prompt_text = messages[0]["content"]
        assert "未知" in prompt_text


# ===== 4. review 方法集成（Mock API）=====

class TestReviewWithMock:
    """review 方法：Mock OpenAI client，测试完整调用链"""

    def test_review_success(self, reviewer, make_ocr, passed_rule_result):
        """Mock API 返回正常 JSON → review 返回完整报告"""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = '{"risk_level":"low","action":"pass","reason":"正常","concerns":[]}'
        mock_response.usage.total_tokens = 500

        with patch.object(reviewer.client.chat.completions, 'create', return_value=mock_response):
            report = reviewer.review([make_ocr()], passed_rule_result)

        assert report.risk_level == RiskLevel.LOW
        assert report.action == ReviewAction.PASS
        assert report.model_used == reviewer.model
        assert report.tokens_used == 500
        assert report.processing_time_ms >= 0  # Mock 调用瞬时完成，可能为 0

    def test_review_api_error_fallback(self, reviewer, make_ocr, passed_rule_result):
        """Mock API 抛异常 → review 降级到规则判定"""
        with patch.object(reviewer.client.chat.completions, 'create', side_effect=Exception("API timeout")):
            report = reviewer.review([make_ocr()], passed_rule_result)

        assert report.action == ReviewAction.PASS  # passed_rule_result → PASS
        assert "不可用" in report.concerns[0]
