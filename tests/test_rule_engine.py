"""规则引擎核心测试 —— 单张发票规则检查"""
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
