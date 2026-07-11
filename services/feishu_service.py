"""
飞书服务封装 —— 两件事：
1. 解析飞书审批回调数据
2. 向飞书群/审批人推送审核建议消息
"""
import json
import hashlib
import hmac
import base64
from typing import Optional, Dict, Any, List

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

    # ==================== 附件下载 ====================

    async def download_form_image(self, file_token: str, file_name: str = "invoice.jpg") -> bytes:
        """
        通过飞书 API 下载审批表单中的附件图片。
        
        飞书审批附件下载接口：
        POST https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download
        
        Returns:
            bytes: 图片二进制数据
        """
        token = await self._get_tenant_token()

        url = f"https://open.feishu.cn/open-apis/drive/v1/medias/{file_token}/download"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"extra": json.dumps({"bitablePerm": {"tableId": "", "rev": 0}})}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, params=params)

        if resp.status_code != 200:
            logger.error(f"[Feishu] 图片下载失败: {file_token}, status={resp.status_code}")
            raise Exception(f"图片下载失败: HTTP {resp.status_code}")

        logger.info(f"[Feishu] 图片下载成功: {file_name} ({len(resp.content)} bytes)")
        return resp.content

    async def download_multiple_images(self, file_tokens: List[str]) -> List[bytes]:
        """批量下载多张图片"""
        import asyncio

        semaphore = asyncio.Semaphore(3)  # 限制并发

        async def _download_one(token: str) -> bytes:
            async with semaphore:
                return await self.download_form_image(token)

        tasks = [_download_one(token) for token in file_tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[Feishu] 第{i+1}张图片下载失败: {r}")
                final.append(None)
            else:
                final.append(r)

        return final

    # ==================== 审批数据解析 ====================

    @staticmethod
    def parse_approval_webhook(body: dict) -> Optional[Dict[str, Any]]:
        """
        解析飞书审批回调的请求体，提取我们需要的信息。
        支持 V2 事件格式。
        """
        # URL 验证请求
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", ""), "is_verification": True}

        # V2 事件格式
        header = body.get("header", {})
        event = body.get("event", body)  # 兼容 V1 和 V2
        event_type = header.get("event_type") or body.get("event_type", "")

        instance_code = event.get("instance_code", body.get("instance_code", ""))

        # 解析表单
        form_raw = event.get("form", "")
        if isinstance(form_raw, str):
            try:
                form_items = json.loads(form_raw)
            except json.JSONDecodeError:
                form_items = []
        elif isinstance(form_raw, list):
            form_items = form_raw
        else:
            form_items = []

        # 提取各字段
        invoice_file_tokens = []
        reason = ""
        event_start_date_str = ""

        for item in form_items:
            name = item.get("name", "")
            value = item.get("value", "")

            # 发票图片字段（名称可能包含"发票"、"图片"、"附件"等关键词）
            if any(kw in name for kw in ["发票", "图片", "附件", "票据"]):
                if isinstance(value, str):
                    try:
                        files = json.loads(value)
                        for f in files:
                            token = f.get("file_token") or f.get("file_code")
                            if token:
                                invoice_file_tokens.append(token)
                    except json.JSONDecodeError:
                        pass
                elif isinstance(value, list):
                    for f in value:
                        token = f.get("file_token") or f.get("file_code")
                        if token:
                            invoice_file_tokens.append(token)

            # 报销事由
            elif any(kw in name for kw in ["事由", "原因", "说明", "备注"]):
                reason = value if isinstance(value, str) else str(value)

            # 日期字段
            elif any(kw in name for kw in ["日期", "时间", "出差"]):
                if isinstance(value, str):
                    event_start_date_str = value

        return {
            "instance_code": instance_code,
            "event_type": event_type,
            "is_verification": False,
            "invoice_file_tokens": invoice_file_tokens,
            "reason": reason,
            "event_start_date_str": event_start_date_str,
            "open_id": event.get("open_id", ""),
            "department_id": event.get("department_id", ""),
        }
