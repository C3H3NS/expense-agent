"""
百度 OCR 封装 —— 把发票图片变成结构化数据。
这是第一个被调用的服务，它的质量直接影响后续所有环节。
"""
import base64
import json
from typing import List, Optional
from datetime import datetime, date

import httpx
from loguru import logger

from config import settings
from models import OcrResult, InvoiceType


class BaiduOcrError(Exception):
    """百度 OCR 调用异常"""
    pass


class OcrService:
    def __init__(self):
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0

    async def _get_access_token(self) -> str:
        """
        获取百度 OAuth Access Token（带缓存）。
        有效期 30 天，过期前自动刷新。
        """
        import time
        current_time = time.time()

        if self._access_token and current_time < self._token_expires_at - 60:
            return self._access_token

        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": settings.baidu_api_key,
            "client_secret": settings.baidu_secret_key,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, params=params)
            data = resp.json()

        if "access_token" not in data:
            raise BaiduOcrError(f"获取Token失败: {data}")

        self._access_token = data["access_token"]
        self._token_expires_at = current_time + data.get("expires_in", 2592000)
        logger.info(f"[OCR] Access Token 已获取，有效期至 {datetime.fromtimestamp(self._token_expires_at)}")
        return self._access_token

    async def _call_ocr_api(self, image_url: str) -> dict:
        """
        调用百度 OCR API 识别一张图片。
        使用「增值税发票识别」接口（对汽车发票效果最好）。
        """
        token = await self._get_access_token()

        api_url = f"https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice?access_token={token}"
        payload = {"url": image_url}

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(api_url, data=payload)
            result = resp.json()

        if "error_code" in result and result["error_code"] != 0:
            raise BaiduOcrError(f"OCR识别失败 [{result['error_code']}]: {result.get('error_msg', '')}")

        return result

    def _parse_ocr_result(self, raw: dict, image_url: str) -> OcrResult:
        """
        将百度 OCR 的原始返回解析成我们的 OcrResult 模型。
        """
        words_result = raw.get("words_result", {})
        words = raw.get("words", [])

        def get_field(field_name: str) -> Optional[str]:
            field_data = words_result.get(field_name, {})
            return field_data.get("word") if field_data else None

        def parse_money(val: Optional[str]) -> float:
            if not val:
                return 0.0
            cleaned = val.replace(",", "").replace("\u00a5", "").replace(" ", "")
            try:
                return round(float(cleaned), 2)
            except ValueError:
                logger.warning(f"[OCR] 无法解析金额: {val}")
                return 0.0

        def parse_date(val: Optional[str]) -> Optional[date]:
            if not val:
                return None
            for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"]:
                try:
                    return datetime.strptime(val.strip(), fmt).date()
                except ValueError:
                    continue
            logger.warning(f"[OCR] 无法解析日期: {val}")
            return None

        def detect_invoice_type(seller_name: Optional[str], invoice_type_raw: Optional[str]) -> InvoiceType:
            name = (seller_name or "").lower()
            type_str = (invoice_type_raw or "").lower()

            if any(kw in name for kw in ["石油", "石化", "加油站", "能源"]):
                return InvoiceType.GAS
            elif any(kw in name for kw in ["停车场", "停车", "泊车"]):
                return InvoiceType.PARKING
            elif any(kw in name for kw in ["高速", "通行", "公路", "交通"]):
                return InvoiceType.TOLL
            else:
                if "加油" in type_str or "汽油" in type_str or "柴油" in type_str:
                    return InvoiceType.GAS
                return InvoiceType.GAS

        result = OcrResult(
            invoice_type=detect_invoice_type(get_field("销售方名称"), get_field("发票类型")),
            invoice_code=get_field("发票代码"),
            invoice_number=get_field("发票号码"),
            issue_date=parse_date(get_field("开票日期")),
            amount=parse_money(get_field("价税合计(小写)") or get_field("金额")),
            seller_name=get_field("销售方名称"),
            buyer_name=get_field("购买方名称"),
            confidence=raw.get("probability", {}).get("average", 0.95),
            image_url=image_url,
            raw_text="\n".join(words[:20]) if words else "",
        )

        logger.info(
            f"[OCR] 识别成功: {result.invoice_type.value} "
            f"| \u00a5{result.amount} | {result.seller_name} | "
            f"置信度 {result.confidence:.2f}"
        )

        return result

    async def recognize(self, image_url: str) -> OcrResult:
        """
        公开方法：识别单张发票图片。

        Args:
            image_url: 图片 URL（支持 http/https 或本地路径转 base64）

        Returns:
            OcrResult: 结构化识别结果
        """
        logger.info(f"[OCR] 开始识别: {image_url}")

        raw_result = await self._call_ocr_api(image_url)
        parsed = self._parse_ocr_result(raw_result, image_url)

        if parsed.confidence < 0.85:
            logger.warning(
                f"[OCR] 低置信度 ({parsed.confidence:.2f})，"
                f"建议人工复核: {image_url}"
            )

        return parsed

    async def batch_recognize(self, image_urls: List[str]) -> List[OcrResult]:
        """
        批量识别多张发票。

        注意：百度 OCR 有 QPS 限制（免费版 2QPS），
        所以这里做了简单的并发控制。
        """
        import asyncio

        semaphore = asyncio.Semaphore(2)

        async def _recognize_one(url: str) -> OcrResult:
            async with semaphore:
                return await self.recognize(url)

        tasks = [_recognize_one(url) for url in image_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"[OCR] 第{i+1}张发票识别失败: {r}")
                final_results.append(OcrResult(
                    image_url=image_urls[i],
                    confidence=0.0,
                    raw_text=f"ERROR: {str(r)}"
                ))
            else:
                final_results.append(r)

        logger.info(f"[OCR] 批量识别完成: {len(final_results)}/{len(image_urls)} 成功")
        return final_results
