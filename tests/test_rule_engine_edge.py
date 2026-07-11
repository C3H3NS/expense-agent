"""规则引擎边界 & 异常测试"""
import os
from datetime import date, timedelta

import pytest

from models import InvoiceType, OcrResult
from services.rule_engine import RuleEngine


class TestEdgeCases:
    """边界 & 异常情况"""

    def test_empty_invoice_list(self, engine):
        """空列表 → 返回空结果，不崩溃"""
        result = engine.batch_check([])
        assert result.invoice_count == 0
        assert result.total_amount == 0.0
        assert result.per_invoice_results == []
        assert result.overall_status == "passed"

    def test_missing_fields_in_ocr(self, engine):
        """
        OCR 缺字段（amount=0, buyer_name=None, issue_date=None, type=None）
        → 规则引擎不崩溃，amount=0 不超限，其他字段 None 时跳过检查
        """
        minimal_invoice = OcrResult()  # 所有字段用默认值
        result = engine.check_single_invoice(minimal_invoice)
        # 不应该崩溃，is_passed 取决于具体规则
        # amount=0 不超 500 限额 → 无金额违规
        # buyer_name=None → 代码有 elif 分支处理，标记 warning
        # issue_date=None → 代码 if ocr_result.issue_date 跳过
        # invoice_type=None → 代码 if ocr_result.invoice_type 跳过
        assert result is not None
        assert isinstance(result.is_passed, bool)

    def test_rules_file_not_found(self):
        """规则文件不存在 → 使用默认规则，不崩溃"""
        engine = RuleEngine(rules_path="nonexistent_rules.json")
        # 默认规则应该能正常工作
        from models import OcrResult, InvoiceType
        from datetime import date, timedelta
        invoice = OcrResult(
            invoice_type=InvoiceType.GAS,
            amount=200.0,
            buyer_name="",
            issue_date=date.today() - timedelta(days=3),
        )
        result = engine.check_single_invoice(invoice)
        assert result is not None
        assert isinstance(result.is_passed, bool)
