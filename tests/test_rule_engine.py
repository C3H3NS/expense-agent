"""规则引擎核心测试 —— 单张发票规则检查"""
import pytest
from models import Severity


class TestSingleAmountLimit:
    """规则1：单张金额上限（limit=500, severity=error）"""

    def test_single_amount_within_limit(self, make_invoice, engine):
        """金额 200 < 500 → 通过，无违规"""
        invoice = make_invoice(amount=200.0)
        result = engine.check_single_invoice(invoice)
        assert result.is_passed is True
        assert len(result.violations) == 0

    def test_single_amount_at_limit(self, make_invoice, engine):
        """金额 500 = 500（边界值）→ 通过，无违规"""
        invoice = make_invoice(amount=500.0)
        result = engine.check_single_invoice(invoice)
        assert result.is_passed is True
        assert len(result.violations) == 0

    def test_single_amount_over_limit(self, make_invoice, engine):
        """金额 501 > 500 → 标记 error"""
        invoice = make_invoice(amount=501.0)
        result = engine.check_single_invoice(invoice)
        assert result.is_passed is False
        assert len(result.violations) == 1
        assert result.violations[0].severity == Severity.ERROR
        assert result.violations[0].rule_name == "单张金额上限"


class TestBuyerNameMatch:
    """规则2：发票抬头匹配（required_company_name=测试科技有限公司, severity=error）"""

    def test_company_name_exact_match(self, make_invoice, engine):
        """抬头完全匹配 → 通过"""
        invoice = make_invoice(buyer_name="测试科技有限公司")
        result = engine.check_single_invoice(invoice)
        buyer_violations = [v for v in result.violations if v.rule_name == "发票抬头匹配"]
        assert len(buyer_violations) == 0

    def test_company_name_partial_match(self, make_invoice, engine):
        """抬头包含公司名关键词 → 通过（代码用 in 做包含匹配）"""
        invoice = make_invoice(buyer_name="测试科技有限公司第一分公司")
        result = engine.check_single_invoice(invoice)
        buyer_violations = [v for v in result.violations if v.rule_name == "发票抬头匹配"]
        assert len(buyer_violations) == 0

    def test_company_name_mismatch(self, make_invoice, engine):
        """抬头不匹配 → 标记 error"""
        invoice = make_invoice(buyer_name="别的什么公司")
        result = engine.check_single_invoice(invoice)
        buyer_violations = [v for v in result.violations if v.rule_name == "发票抬头匹配"]
        assert len(buyer_violations) == 1
        assert buyer_violations[0].severity == Severity.ERROR


from datetime import timedelta


class TestDateReasonable:
    """规则3：日期合理性（max_days_before_event=7, severity=error）"""

    def test_date_within_range(self, make_invoice, engine, today):
        """发票日期 3 天前，event_start_date=今天 → 3 < 7 → 通过"""
        invoice = make_invoice(issue_date=today - timedelta(days=3))
        result = engine.check_single_invoice(
            invoice, event_start_date=today
        )
        date_violations = [v for v in result.violations if "日期" in v.rule_name]
        assert len(date_violations) == 0

    def test_date_too_old(self, make_invoice, engine, today):
        """发票日期 90 天前，event_start_date=今天 → 90 > 7 → 标记 error"""
        invoice = make_invoice(issue_date=today - timedelta(days=90))
        result = engine.check_single_invoice(
            invoice, event_start_date=today
        )
        date_violations = [v for v in result.violations if "日期" in v.rule_name]
        assert len(date_violations) == 1
        assert date_violations[0].severity == Severity.ERROR

    def test_date_uses_config_max_days(self, make_invoice, engine, today):
        """
        回归保护：验证 date_reasonable 规则正确读取配置值。
        test_rules.json 中 max_days_before_event=7。
        发票日期 8 天前 → 8 > 7 → 应该触发违规。
        """
        invoice = make_invoice(issue_date=today - timedelta(days=8))
        result = engine.check_single_invoice(
            invoice, event_start_date=today
        )
        date_violations = [v for v in result.violations if "日期" in v.rule_name]
        assert len(date_violations) == 1, "8天前应该超过7天限制，触发违规"


from models import InvoiceType


class TestInvoiceType:
    """规则4：发票类型是否在允许范围内"""

    def test_valid_invoice_type(self, make_invoice, engine):
        """加油票/停车票/过路费票 → 全部通过"""
        for invoice_type in [InvoiceType.GAS, InvoiceType.PARKING, InvoiceType.TOLL]:
            invoice = make_invoice(invoice_type=invoice_type)
            result = engine.check_single_invoice(invoice)
            type_violations = [v for v in result.violations if "类型" in v.rule_name]
            assert len(type_violations) == 0, f"{invoice_type.value} 不应该触发类型违规"

    def test_invoice_type_none(self, make_invoice, engine):
        """invoice_type=None（OCR未识别出类型）→ 不崩溃，不产生类型违规"""
        invoice = make_invoice(invoice_type=None)
        result = engine.check_single_invoice(invoice)
        type_violations = [v for v in result.violations if "类型" in v.rule_name]
        assert len(type_violations) == 0


class TestMonthlySpentInSingleCheck:
    """Bug修复2: check_single_invoice 应该检查月度累计"""

    def test_single_invoice_monthly_exceed(self, make_invoice, engine):
        """
        单张发票金额 200（未超单张限额），
        但 monthly_spent_so_far=2900，200+2900=3100 > 3000 月度限额 → 应标记 warning。
        """
        invoice = make_invoice(amount=200.0)
        result = engine.check_single_invoice(
            invoice, monthly_spent_so_far=2900.0
        )
        monthly_violations = [v for v in result.violations if "月度" in v.rule_name]
        assert len(monthly_violations) == 1
        assert monthly_violations[0].severity == Severity.WARNING
