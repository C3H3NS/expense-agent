"""
规则引擎 —— 只做硬判断，不做模糊推测。
规则 = 能写成 if/else 的条件。

设计原则：
1. 每条规则独立运行，互不影响
2. 规则配置外置（JSON 文件），改规则不改代码
3. 每条违规都要有明确的原因说明
4. 区分 error（必须驳回）和 warning（提醒即可）
"""
import json
import os
from datetime import date, timedelta
from typing import List

from loguru import logger
from config import settings

from models import (
    OcrResult, RuleCheckResult, RuleViolation,
    BatchRuleCheckResult, Severity, InvoiceType
)


class RuleEngine:
    def __init__(self, rules_path: str = "rules/expense_rules.json"):
        self.rules = self._load_rules(rules_path)
        self.company_name = settings.company_name

        expense_rules = self.rules.get("expense_types", {}).get("交通费", {})
        buyer_rule = expense_rules.get("rules", {}).get("buyer_name_match", {})
        if not buyer_rule.get("required_company_name"):
            buyer_rule["required_company_name"] = self.company_name

    def _load_rules(self, path: str) -> dict:
        """加载规则配置文件"""
        if not os.path.exists(path):
            logger.warning(f"[Rules] 规则文件不存在: {path}，使用默认规则")
            return self._default_rules()

        with open(path, "r", encoding="utf-8") as f:
            rules = json.load(f)
        logger.info(f"[Rules] 规则已加载: {path}, 版本={rules.get('version', '?')}")
        return rules

    def _default_rules(self) -> dict:
        """内置默认规则（当 JSON 文件不存在时兜底）"""
        return {
            "expense_types": {
                "交通费": {
                    "allowed_invoice_types": ["加油票", "停车票", "过路费票"],
                    "rules": {
                        "single_limit": {
                            "name": "单张金额上限",
                            "limit": settings.expense_single_limit,
                            "severity": "error",
                            "message": "单张发票金额 {actual}元超过上限{limit}元"
                        },
                        "monthly_limit": {
                            "name": "月度累计上限",
                            "limit": settings.expense_monthly_limit,
                            "severity": "warning",
                            "message": "本月累计 {actual}元，月度上限{limit}元"
                        },
                        "buyer_name_match": {
                            "name": "发票抬头匹配",
                            "required_company_name": settings.company_name,
                            "severity": "error",
                            "message": "发票抬头 '{actual}' 与公司名不符"
                        },
                        "date_reasonable": {
                            "name": "日期合理性",
                            "max_days_before": settings.expense_date_range_days,
                            "severity": "error",
                            "message": "发票日期 {actual} 可能不属于本次事由期间"
                        },
                    }
                }
            }
        }

    def check_single_invoice(
        self,
        ocr_result: OcrResult,
        index: int = 0,
        event_start_date: date = None,
        monthly_spent_so_far: float = 0,
    ) -> RuleCheckResult:
        """
        对单张发票执行所有规则检查。
        """
        violations: List[RuleViolation] = []
        rules_config = self.rules["expense_types"]["交通费"]["rules"]

        # ---- 规则1：单张金额上限 ----
        single_limit_cfg = rules_config.get("single_limit", {})
        if ocr_result.amount > single_limit_cfg.get("limit", 999999):
            violations.append(RuleViolation(
                rule_name=single_limit_cfg["name"],
                description=single_limit_cfg["message"].format(
                    actual=ocr_result.amount,
                    limit=single_limit_cfg["limit"]
                ),
                severity=Severity(single_limit_cfg.get("severity", "error")),
                actual_value=ocr_result.amount,
                limit_value=float(single_limit_cfg["limit"]),
            ))

        # ---- 规则2：发票抬头匹配 ----
        buyer_cfg = rules_config.get("buyer_name_match", {})
        required_name = buyer_cfg.get("required_company_name", "")
        if required_name and ocr_result.buyer_name:
            if required_name.replace(" ", "") not in ocr_result.buyer_name.replace(" ", ""):
                violations.append(RuleViolation(
                    rule_name=buyer_cfg["name"],
                    description=buyer_cfg["message"].format(actual=ocr_result.buyer_name),
                    severity=Severity(buyer_cfg.get("severity", "error")),
                ))
        elif required_name and not ocr_result.buyer_name:
            violations.append(RuleViolation(
                rule_name=buyer_cfg["name"],
                description="无法识别发票抬头，请确认是否为公司抬头发票",
                severity=Severity.WARNING,
            ))

        # ---- 规则3：日期合理性 ----
        date_cfg = rules_config.get("date_reasonable", {})
        max_days_before = date_cfg.get("max_days_before", 7)
        today = date.today()

        if ocr_result.issue_date:
            if ocr_result.issue_date > today:
                violations.append(RuleViolation(
                    rule_name=date_cfg["name"],
                    description=date_cfg["message"].format(actual=str(ocr_result.issue_date)),
                    severity=Severity(date_cfg.get("severity", "error")),
                ))
            elif event_start_date and ocr_result.issue_date < (event_start_date - timedelta(days=max_days_before)):
                days_diff = (event_start_date - ocr_result.issue_date).days
                violations.append(RuleViolation(
                    rule_name=date_cfg["name"],
                    description=f"发票日期({ocr_result.issue_date})早于事由开始日({event_start_date})共{days_diff}天",
                    severity=Severity(date_cfg.get("severity", "error")),
                ))

        # ---- 规则4：发票类型是否在允许范围内 ----
        allowed_types = self.rules["expense_types"]["交通费"].get("allowed_invoice_types", [])
        if ocr_result.invoice_type and ocr_result.invoice_type.value not in allowed_types:
            violations.append(RuleViolation(
                rule_name="发票类型检查",
                description=f"发票类型为'{ocr_result.invoice_type.value}'，不在交通费允许范围内",
                severity=Severity.WARNING,
            ))

        # ---- 汇总结果 ----
        has_error = any(v.severity == Severity.ERROR for v in violations)

        return RuleCheckResult(
            invoice_index=index,
            is_passed=not has_error,
            violations=violations,
            summary=self._generate_summary(violations, ocr_result),
        )

    def _generate_summary(self, violations: List[RuleViolation], ocr: OcrResult) -> str:
        """生成一句话总结"""
        type_str = ocr.invoice_type.value if ocr.invoice_type else "发票"

        if not violations:
            return f"{type_str} \u00a5{ocr.amount} - 无违规"

        errors = [v for v in violations if v.severity == Severity.ERROR]
        warns = [v for v in violations if v.severity == Severity.WARNING]

        parts = []
        if errors:
            parts.append(f"{len(errors)}项违规")
        if warns:
            parts.append(f"{len(warns)}项提醒")

        return f"{type_str} \u00a5{ocr.amount} - {'; '.join(parts)}"

    def batch_check(
        self,
        ocr_results: List[OcrResult],
        event_start_date: date = None,
        monthly_spent_so_far: float = 0,
    ) -> BatchRuleCheckResult:
        """
        对一批发票批量检查，包含汇总统计。
        """
        per_invoice_results = []
        total_amount = 0.0
        all_violations = []

        for i, ocr in enumerate(ocr_results):
            result = self.check_single_invoice(ocr, index=i, event_start_date=event_start_date, monthly_spent_so_far=monthly_spent_so_far)
            per_invoice_results.append(result)
            total_amount += ocr.amount
            all_violations.extend(result.violations)

        # 月度限额检查（整批级别）
        monthly_limit_cfg = self.rules["expense_types"]["交通费"]["rules"].get("monthly_limit", {})
        monthly_limit = monthly_limit_cfg.get("limit", 999999)
        grand_total = total_amount + monthly_spent_so_far

        if grand_total > monthly_limit:
            all_violations.append(RuleViolation(
                rule_name=monthly_limit_cfg["name"],
                description=monthly_limit_cfg["message"].format(actual=grand_total, limit=monthly_limit),
                severity=Severity(monthly_limit_cfg.get("severity", "warning")),
                actual_value=grand_total,
                limit_value=float(monthly_limit),
            ))

        # 判定整体状态
        has_error = any(v.severity == Severity.ERROR for v in all_violations)
        has_warning = any(v.severity == Severity.WARNING for v in all_violations)

        if has_error:
            overall = "rejected"
        elif has_warning:
            overall = "warning"
        else:
            overall = "passed"

        logger.info(
            f"[Rules] 检查完成: {len(ocr_results)}张, 总额\u00a5{total_amount}, "
            f"状态={overall}, 违规{len(all_violations)}项"
        )

        return BatchRuleCheckResult(
            total_amount=total_amount,
            invoice_count=len(ocr_results),
            monthly_total=grand_total,
            per_invoice_results=per_invoice_results,
            overall_status=overall,
        )
