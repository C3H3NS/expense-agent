"""
数据模型定义 —— 所有业务数据的"身份证"。
修改数据格式只改这里，不用满项目找。
"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import date
from enum import Enum


class InvoiceType(str, Enum):
    """发票类型枚举"""
    GAS = "加油票"
    PARKING = "停车票"
    TOLL = "过路费票"


class RiskLevel(str, Enum):
    """风险等级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReviewAction(str, Enum):
    """审核动作"""
    PASS = "pass"
    WARN = "warn"
    REJECT = "reject"


class Severity(str, Enum):
    """违规严重程度"""
    ERROR = "error"
    WARNING = "warning"


# ========== 输入数据 ==========

class FeishuApprovalData(BaseModel):
    """飞书审批回调解析后的结构化数据"""
    instance_code: str = Field(description="审批实例编码")
    applicant_name: str = Field(description="申请人姓名")
    applicant_id: str = Field(description="申请人 open_id")
    department: str = Field(default="", description="部门")
    form_data: dict = Field(description="表单数据，含发票URLs、事由、金额等")


# ========== OCR 相关 ==========

class OcrResult(BaseModel):
    """单张发票 OCR 识别结果"""
    invoice_type: Optional[InvoiceType] = None
    invoice_code: Optional[str] = None
    invoice_number: Optional[str] = None
    issue_date: Optional[date] = None
    amount: float = 0.0
    seller_name: Optional[str] = None
    buyer_name: Optional[str] = None
    confidence: float = 0.0
    image_url: Optional[str] = None
    raw_text: Optional[str] = None


# ========== 规则引擎相关 ==========

class RuleViolation(BaseModel):
    """单条违规记录"""
    rule_name: str
    description: str
    severity: Severity
    actual_value: Optional[float] = None
    limit_value: Optional[float] = None


class RuleCheckResult(BaseModel):
    """单张发票的规则检查结果"""
    invoice_index: int = 0
    is_passed: bool = True
    violations: List[RuleViolation] = []
    summary: str = ""


class BatchRuleCheckResult(BaseModel):
    """整批发票的规则检查结果"""
    total_amount: float = 0.0
    invoice_count: int = 0
    monthly_total: Optional[float] = None
    per_invoice_results: List[RuleCheckResult] = []
    overall_status: str = "passed"


# ========== AI 审核相关 ==========

class EmployeeContext(BaseModel):
    """员工上下文（用于 AI 判断合理性）"""
    name: str
    department: str
    position: str = ""
    recent_expense_count_3m: int = 0
    recent_expense_total_3m: float = 0.0
    rejection_count_3m: int = 0


class AiReviewReport(BaseModel):
    """AI 审核报告（最终输出）"""
    risk_level: RiskLevel = RiskLevel.LOW
    action: ReviewAction = ReviewAction.PASS
    reason: str = ""
    concerns: List[str] = []
    model_used: str = ""
    tokens_used: int = 0
    processing_time_ms: int = 0


# ========== 最终报告 ==========

class FinalReviewReport(BaseModel):
    """推送给审批人的完整报告"""
    instance_code: str = ""
    applicant_name: str = ""
    department: str = ""
    submit_time: str = ""

    total_amount: float = 0.0
    invoice_count: int = 0

    invoice_details: List[dict] = []

    rule_summary: str = ""

    ai_action: str = ""
    ai_reason: str = ""
    risk_level: str = ""
    risk_emoji: str = ""

    processed_at: str = ""
    version: str = "v1.0"
