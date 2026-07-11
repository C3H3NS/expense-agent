"""规则引擎批量检查测试 —— 月度限额 + 整体状态"""
from datetime import date, timedelta

from models import InvoiceType, Severity


class TestBatchCheck:
    """batch_check: 批量检查 + 月度限额"""

    def test_batch_under_monthly_limit(self, make_invoice, engine):
        """3 张发票各 200 元，月累计 600 < 3000 → 全通过"""
        invoices = [
            make_invoice(amount=200.0),
            make_invoice(amount=200.0),
            make_invoice(amount=200.0),
        ]
        result = engine.batch_check(invoices, monthly_spent_so_far=0)
        assert result.invoice_count == 3
        assert result.total_amount == 600.0
        assert result.overall_status == "passed"

    def test_batch_over_monthly_limit(self, make_invoice, engine):
        """3 张发票各 400 元 = 1200，加月累计 2000 → 3200 > 3000 → warning"""
        invoices = [
            make_invoice(amount=400.0),
            make_invoice(amount=400.0),
            make_invoice(amount=400.0),
        ]
        result = engine.batch_check(invoices, monthly_spent_so_far=2000.0)
        assert result.total_amount == 1200.0
        assert result.monthly_total == 3200.0
        assert result.overall_status == "warning"
