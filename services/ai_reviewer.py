"""
AI 审核 —— 让 Claude 做"软判断"。

规则引擎管硬约束（金额、抬头、日期），
LLM 管软判断（合理性、异常模式、风险评级）。

各司其职，不越界。
"""
import json
import time
import os
from typing import List

import anthropic
from loguru import logger

from config import settings
from models import (
    OcrResult, BatchRuleCheckResult, AiReviewReport,
    RiskLevel, ReviewAction, EmployeeContext
)


class AiReviewer:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model
        self.prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        """加载 Prompt 模板"""
        template_path = "prompts/review_prompt.txt"
        if os.path.exists(template_path):
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read()
        else:
            logger.warning("[AI] Prompt模板不存在，使用内置默认模板")
            return self._default_prompt()

    def _default_prompt(self) -> str:
        """内置默认 Prompt（当文件不存在时兜底）"""
        return (
            "你是一个财务报销审核助手。分析交通费报销的合理性。\n\n"
            "{{invoice_details}}\n\n{{rule_summary}}\n\n"
            "申请人：{{applicant_name}}，部门：{{department}}，事由：{{reason}}\n"
            "本月累计：{{monthly_total}}（限额{{monthly_limit}}）\n\n"
            "输出JSON：{\"risk_level\":\"low|medium|high\",\"action\":\"pass|warn|reject\","
            "\"reason\":\"一句话理由\",\"concerns\":[\"关注点\"]}"
        )

    def _build_messages(
        self,
        ocr_results: List[OcrResult],
        rule_result: BatchRuleCheckResult,
        employee_ctx: EmployeeContext = None,
        reason: str = "",
    ) -> list:
        """
        将所有数据组装成发送给 Claude 的消息。
        """
        invoice_lines = []
        for i, ocr in enumerate(ocr_results):
            line = (
                f"{i+1}. [{ocr.invoice_type.value or '未知'}] "
                f"\u00a5{ocr.amount:.2f} | "
                f"{ocr.seller_name or '未知'} | "
                f"日期:{ocr.issue_date or '未知'} | "
                f"抬头:{ocr.buyer_name or '未识别'} | "
                f"置信度:{ocr.confidence:.0%}"
            )
            invoice_lines.append(line)
        invoice_details = "\n".join(invoice_lines) if invoice_lines else "无"

        rule_parts = []
        for pr in rule_result.per_invoice_results:
            if not pr.is_passed:
                for v in pr.violations:
                    rule_parts.append(f"- [{v.severity.value}] {v.description}")

        if rule_result.monthly_total and rule_result.monthly_total > self._get_monthly_limit():
            rule_parts.append(f"- 本月累计 \u00a5{rule_result.monthly_total:.0f} 超过/接近月度限额")

        rule_summary = "\n".join(rule_parts) if rule_parts else "\u2705 全部通过"

        prompt = self.prompt_template.replace("{{invoice_details}}", invoice_details)
        prompt = prompt.replace("{{rule_summary}}", rule_summary)
        prompt = prompt.replace("{{applicant_name}}", employee_ctx.name if employee_ctx else "未知")
        prompt = prompt.replace("{{department}}", employee_ctx.department if employee_ctx else "未知")
        prompt = prompt.replace("{{reason}}", reason or "未填写")
        prompt = prompt.replace("{{monthly_total}}", f"\u00a5{rule_result.monthly_total or rule_result.total_amount:.0f}")
        prompt = prompt.replace("{{monthly_limit}}", f"\u00a5{self._get_monthly_limit()}")

        if employee_ctx:
            prompt = prompt.replace("{{recent_count}}", str(employee_ctx.recent_expense_count_3m))
            prompt = prompt.replace("{{recent_total}}", str(employee_ctx.recent_expense_total_3m))
            prompt = prompt.replace("{{rejection_count}}", str(employee_ctx.rejection_count_3m))
        else:
            prompt = prompt.replace("{{recent_count}}", "暂无数据")
            prompt = prompt.replace("{{recent_total}}", "0")
            prompt = prompt.replace("{{rejection_count}}", "0")

        return [{"role": "user", "content": prompt}]

    def _get_monthly_limit(self) -> int:
        return settings.expense_monthly_limit

    def _parse_ai_response(self, response_text: str) -> AiReviewReport:
        """
        解析 Claude 返回的 JSON。
        LLM 有时不严格按格式输出，所以要做容错处理。
        """
        text = response_text.strip()

        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1:
            text = text[start_idx:end_idx + 1]

        try:
            data = json.loads(text)
            return AiReviewReport(
                risk_level=RiskLevel(data.get("risk_level", "low")),
                action=ReviewAction(data.get("action", "pass")),
                reason=data.get("reason", ""),
                concerns=data.get("concerns", []),
            )
        except json.JSONDecodeError as e:
            logger.error(f"[AI] 解析JSON失败: {e}\n原始内容: {text[:500]}")
            return AiReviewReport(
                risk_level=RiskLevel.MEDIUM,
                action=ReviewAction.WARN,
                reason="(系统无法解析AI回复，建议人工复核)",
                concerns=[f"AI返回格式异常: {text[:200]}"],
            )

    def review(
        self,
        ocr_results: List[OcrResult],
        rule_result: BatchRuleCheckResult,
        employee_ctx: EmployeeContext = None,
        reason: str = "",
    ) -> AiReviewReport:
        """
        执行 AI 审核（公开方法）。
        这是整个服务的核心方法。
        """
        start_time = time.time()

        messages = self._build_messages(ocr_results, rule_result, employee_ctx, reason)

        logger.info(f"[AI] 开始审核，使用模型: {self.model}")

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                temperature=0.1,
                messages=messages,
            )

            response_text = response.content[0].text
            elapsed_ms = int((time.time() - start_time) * 1000)

            logger.info(f"[AI] 审核完成，耗时 {elapsed_ms}ms, tokens={response.usage.output_tokens}")

            report = self._parse_ai_response(response_text)
            report.model_used = self.model
            report.tokens_used = response.usage.input_tokens + response.usage.output_tokens
            report.processing_time_ms = elapsed_ms

            return report

        except anthropic.APIError as e:
            logger.error(f"[AI] Claude API 错误: {e}")
            return self._fallback_to_rules(rule_result)
        except Exception as e:
            logger.error(f"[AI] 未预期错误: {e}")
            return self._fallback_to_rules(rule_result)

    def _fallback_to_rules(self, rule_result: BatchRuleCheckResult) -> AiReviewReport:
        """Claude API 不可用时的降级方案"""
        if rule_result.overall_status == "rejected":
            action = ReviewAction.REJECT
            risk = RiskLevel.HIGH
            reason = "规则检测到硬性违规（AI 服务不可用，基于规则判定）"
        elif rule_result.overall_status == "warning":
            action = ReviewAction.WARN
            risk = RiskLevel.MEDIUM
            reason = "规则检测到需关注项（AI 服务不可用，建议人工复核）"
        else:
            action = ReviewAction.PASS
            risk = RiskLevel.LOW
            reason = "规则检查全部通过（AI 服务不可用，仅基于规则判定）"

        return AiReviewReport(
            risk_level=risk,
            action=action,
            reason=reason,
            concerns=["AI 审核服务暂时不可用，当前结果仅基于规则引擎"],
        )
