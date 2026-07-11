"""pytest 公共 fixtures —— 发票数据工厂 + 规则引擎实例"""
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from models import OcrResult, InvoiceType
from services.rule_engine import RuleEngine


# ===== 路径 fixtures =====

@pytest.fixture
def test_rules_path():
    """测试专用规则文件路径"""
    return str(Path(__file__).parent / "test_rules.json")


@pytest.fixture
def engine(test_rules_path):
    """初始化好的 RuleEngine 实例（使用测试规则）"""
    return RuleEngine(rules_path=test_rules_path)


# ===== 数据工厂 fixtures =====

@pytest.fixture
def make_invoice():
    """
    发票数据工厂 —— 返回一个函数，调用时可覆盖任意字段。
    默认生成一张完全合规的加油票。
    """
    def _make(**kwargs) -> OcrResult:
        defaults = {
            "invoice_type": InvoiceType.GAS,
            "invoice_code": "12345678901",
            "invoice_number": "12345678",
            "issue_date": date.today() - timedelta(days=3),
            "amount": 200.0,
            "seller_name": "中国石化加油站",
            "buyer_name": "测试科技有限公司",
            "confidence": 0.95,
        }
        defaults.update(kwargs)
        return OcrResult(**defaults)
    return _make


@pytest.fixture
def today():
    """今天的日期"""
    return date.today()
