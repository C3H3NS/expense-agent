"""
飞书服务封装 —— 两件事：
1. 解析飞书审批回调数据
2. 向飞书群/审批人推送审核建议消息
"""
import json
import hashlib
import hmac
import base64
from typing import Optional, Dict, Any

import httpx
from loguru import logger

from config import settings
from models import FinalReviewReport


class FeishuService:
    def __init__(self):
        self.app_id = settings.feishu_app_id
        self.app_secret = settings.feishu_app_secret
        self.bot_webhook = settings.feishu_bot_webhook
        self._tenant_access_token: Optional[str] = None
        self._token_expires_at: float = 0

    # ==================== 消息推送 ====================

    async def send_review_message(self, report: FinalReviewReport, chat_id: str = None):
        """
        将审核报告推送到飞书。

        两种方式：
        - 方式A（MVP）：通过机器人 Webhook 直接发消息（最简单）
        - 方式B（正式）：通过 API 发送给指定用户/群组
        """
        content = self._format_report_message(report)

        if self.bot_webhook:
            await self._send_by_webhook(content, report)
        else:
            target = chat_id or "oc_xxxxxxxx"
            await self._send_by_api(target, content)

        logger.info(f"[Feishu] 审核建议已推送: 实例={report.instance_code}, 动作={report.ai_action}")

    def _format_report_message(self, report: FinalReviewReport) -> str:
        """把 FinalReviewReport 格式化为飞书消息文本"""
        lines = [
            f"**{report.applicant_name} 的交通费报销审核报告**",
            "",
            f"**基本信息**",
            f"申请人：{report.applicant_name}",
            f"部门：{report.department}",
            f"金额：**\u00a5{report.total_amount:.2f}** | 发票 {report.invoice_count} 张",
            "",
            f"**发票明细**",
        ]

        for i, detail in enumerate(report.invoice_details, 1):
            status_icon = "\u2705" if detail.get("is_passed") else "\u274c"
            lines.append(
                f"{i}. {status_icon} {detail.get('type', '?')} "
                f"**\u00a5{detail.get('amount', 0):.2f}** | "
                f"{detail.get('seller', '?')}"
            )
            if detail.get("violations"):
                for v in detail["violations"]:
                    lines.append(f"   \u26a0\ufe0f {v}")

        lines += [
            "",
            f"**规则检查**",
            report.rule_summary,
            "",
            f"**AI 审核建议**",
            f"{report.risk_emoji} **{report.ai_action.upper()}** — {report.ai_reason}",
            "",
            f"{report.processed_at} | v{report.version}",
        ]

        return "\n".join(lines)

    async def _send_by_webhook(self, content: str, report: FinalReviewReport):
        """方式A：通过机器人 Webhook 发送（MVP 推荐）"""
        template = "green" if report.risk_level == "low" else ("orange" if report.risk_level == "medium" else "red")

        payload = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "交通费报销审核建议"},
                    "template": template,
                },
                "elements": [
                    {"tag": "markdown", "content": content},
                ],
            },
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self.bot_webhook, json=payload)

        if resp.status_code != 200:
            logger.error(f"[Feishu] Webhook 发送失败: {resp.text}")

    async def _send_by_api(self, chat_id: str, content: str):
        """方式B：通过飞书 Open API 发送（需要 Tenant Access Token）"""
        token = await self._get_tenant_token()

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {"Authorization": f"Bearer {token}"}

        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps({
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "交通费报销审核"},
                },
                "elements": [{"tag": "markdown", "content": content}],
            }),
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=payload)

        if resp.status_code != 200:
            logger.error(f"[Feishu] API 发送失败: {resp.text}")

    # ==================== 回调验签 ====================

    def verify_callback(self, timestamp: str, nonce: str, signature: str) -> bool:
        """验证飞书回调请求的真实性"""
        token = settings.feishu_verification_token
        if not token:
            logger.warning("[Feishu] 未配置 verification_token，跳过验签（仅开发环境可用）")
            return True

        sign_str = f"{timestamp}{nonce}{token}"
        expected = base64.b64encode(
            hmac.new(sign_str.encode(), digestmod=hashlib.sha256).digest()
        ).decode()

        return expected == signature

    # ==================== Token 管理 ====================

    async def _get_tenant_token(self) -> str:
        """获取飞书 Tenant Access Token（带缓存）"""
        import time
        now = time.time()

        if self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            data = resp.json()

        if data.get("code") != 0:
            raise Exception(f"获取飞书Token失败: {data}")

        self._tenant_access_token = data["tenant_access_token"]
        self._token_expires_at = now + data.get("expire", 7200)
        return self._tenant_access_token

    # ==================== 审批数据解析 ====================

    @staticmethod
    def parse_approval_webhook(body: dict) -> Optional[Dict[str, Any]]:
        """
        解析飞书审批回调的请求体，提取我们需要的信息。

        飞书审批回调的结构比较深，这里做一层扁平化。
        具体字段需要对照实际飞书审批表单结构来调整。
        """
        return {
            "instance_code": body.get("instance_code", ""),
            "event_type": body.get("event_type", ""),
        }
