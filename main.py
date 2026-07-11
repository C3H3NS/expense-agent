"""
FastAPI 主入口 —— 交通费报销 AI 智能体

处理链路：
  飞书审批回调 → 解析数据 → OCR识别 → 规则检查 → AI审核 → 推送飞书
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from loguru import logger
import time
import json
from datetime import datetime, date
from typing import List, Optional

from config import settings
from models import (
    FeishuApprovalData, FinalReviewReport, EmployeeContext,
    OcrResult, BatchRuleCheckResult,
)
from services.ocr_service import OcrService
from services.rule_engine import RuleEngine
from services.ai_reviewer import AiReviewer
from services.feishu_service import FeishuService

app = FastAPI(title=settings.app_name)
logger.add("logs/app.log", rotation="10 MB", retention="7 days")


# ====== 初始化服务实例（全局复用连接池等）======
ocr_service = OcrService()
rule_engine = RuleEngine()
ai_reviewer = AiReviewer()
feishu_service = FeishuService()


# ====== 健康检查 ======

@app.get("/")
def health_check():
    return {"status": "ok", "service": settings.app_name, "time": datetime.now().isoformat()}


# ====== 调试接口（开发阶段用，上线删除）======

@app.get("/debug/review-test")
async def debug_review_test():
    """手动触发一次完整审核流程（模拟数据，不调 OCR）"""
    from datetime import date

    logger.info("[Debug] === 开始测试审核流程 ===")

    # 模拟飞书传来的数据
    test_ocr_results = [
        OcrResult(
            invoice_type=None,
            amount=380.00,
            buyer_name="XX科技有限公司",
            issue_date=date(2026, 7, 8),
            seller_name="中国石油朝阳路加油站",
            confidence=0.97,
        ),
        OcrResult(
            invoice_type=None,
            amount=290.00,
            buyer_name="XX科技有限公司",
            issue_date=date(2026, 7, 9),
            seller_name="中石化望京加油站",
            confidence=0.95,
        ),
    ]

    test_employee = EmployeeContext(
        name="张三",
        department="销售部",
        position="客户经理",
        recent_expense_count_3m=4,
        recent_expense_total_3m=1680,
        rejection_count_3m=0,
    )

    start = time.time()

    # Step 1: 规则检查（OCR 在真实流程里才有，这里跳过）
    rule_result = rule_engine.batch_check(
        test_ocr_results,
        event_start_date=date(2026, 7, 8),
    )
    logger.info(f"[Debug] 规则检查完成: {rule_result.overall_status}")

    # Step 2: AI 审核
    ai_report = ai_reviewer.review(
        test_ocr_results,
        rule_result,
        test_employee,
        reason="7月8-9日拜访客户往返",
    )
    logger.info(f"[Debug] AI 审核完成: {ai_report.action.value}")

    # Step 3: 组装最终报告
    report = _assemble_report(
        instance_code="TEST-001",
        ocr_results=test_ocr_results,
        rule_result=rule_result,
        ai_report=ai_report,
        employee=test_employee,
    )

    elapsed = time.time() - start
    logger.info(f"[Debug] === 流程结束，耗时 {elapsed:.2f}s ===")

    return {
        "status": "success",
        "elapsed_seconds": round(elapsed, 2),
        "report": report.model_dump(),
        "ai_detail": ai_report.model_dump(),
        "rule_detail": rule_result.model_dump(),
    }


@app.post("/debug/ocr-test")
async def debug_ocr_test(request: Request):
    """
    用真实发票图片测试 OCR → 规则 → AI 全流程。
    
    请求体：
    {
        "image_urls": ["https://example.com/invoice1.jpg"],
        "applicant_name": "张三",
        "department": "销售部",
        "reason": "拜访客户"
    }
    
    或直接在 URL 参数中传一张图片：
    POST /debug/ocr-test?url=https://xxx/invoice.jpg
    """
    body = await request.json()

    # 支持两种传参方式
    image_urls = body.get("image_urls", [])
    single_url = body.get("url")
    if single_url:
        image_urls = [single_url]

    if not image_urls:
        return JSONResponse({"error": "请提供 image_urls 或 url 参数"}, status_code=400)

    applicant_name = body.get("applicant_name", "测试用户")
    department = body.get("department", "测试部门")
    reason = body.get("reason", "测试事由")

    logger.info(f"[Debug-OCR] 开始测试，图片数: {len(image_urls)}")

    start = time.time()

    try:
        # Step 1: OCR 识别
        ocr_results = await ocr_service.batch_recognize(image_urls)
        logger.info(f"[Debug-OCR] OCR 完成，成功 {sum(1 for r in ocr_results if r.confidence > 0)}/{len(ocr_results)}")

        # Step 2: 规则检查
        rule_result = rule_engine.batch_check(ocr_results)
        logger.info(f"[Debug-OCR] 规则检查: {rule_result.overall_status}")

        # Step 3: AI 审核
        employee = EmployeeContext(name=applicant_name, department=department)
        ai_report = ai_reviewer.review(ocr_results, rule_result, employee, reason=reason)
        logger.info(f"[Debug-OCR] AI 审核: {ai_report.action.value}")

        # Step 4: 组装报告
        report = _assemble_report(
            instance_code=f"OCR-TEST-{int(time.time())}",
            ocr_results=ocr_results,
            rule_result=rule_result,
            ai_report=ai_report,
            employee=employee,
        )

        elapsed = time.time() - start

        return {
            "status": "success",
            "elapsed_seconds": round(elapsed, 2),
            "ocr_results": [r.model_dump() for r in ocr_results],
            "rule_detail": rule_result.model_dump(),
            "ai_detail": ai_report.model_dump(),
            "report": report.model_dump(),
        }

    except Exception as e:
        logger.error(f"[Debug-OCR] 测试失败: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# ====== 飞书 Webhook 入口 ======

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    """
    飞书审批事件回调。
    
    触发时机：员工在飞书提交交通费报销审批单时
    """
    body = await request.json()
    logger.info(f"[Webhook] 收到回调: {json.dumps(body, ensure_ascii=False)[:500]}")

    # 解析回调数据
    approval_info = FeishuService.parse_approval_webhook(body)

    # URL 验证请求（飞书配置事件订阅时发的验证请求）
    if approval_info.get("is_verification"):
        logger.info(f"[Webhook] URL 验证请求，返回 challenge")
        return JSONResponse({"challenge": approval_info["challenge"]})

    instance_code = approval_info.get("instance_code", "")
    event_type = approval_info.get("event_type", "")
    logger.info(f"[Webhook] 处理审批实例: {instance_code}, 事件类型: {event_type}")

    # 只处理审批创建事件
    if event_type and "create" not in event_type.lower():
        logger.info(f"[Webhook] 非创建事件({event_type})，跳过")
        return JSONResponse({"code": 0, "msg": "ignored", "event_type": event_type})

    try:
        invoice_tokens = approval_info.get("invoice_file_tokens", [])
        reason = approval_info.get("reason", "")
        date_str = approval_info.get("event_start_date_str", "")

        logger.info(f"[Webhook] 发票图片: {len(invoice_tokens)} 张, 事由: {reason}")

        if not invoice_tokens:
            logger.warning("[Webhook] 未找到发票图片，跳过处理")
            return JSONResponse({"code": 0, "msg": "no invoices found"})

        # ===== 核心处理链 =====
        start = time.time()

        # 1. 下载发票图片
        logger.info(f"[Webhook] 开始下载 {len(invoice_tokens)} 张发票图片...")
        image_data_list = await feishu_service.download_multiple_images(invoice_tokens)
        valid_images = [img for img in image_data_list if img is not None]
        logger.info(f"[Webhook] 图片下载完成: {len(valid_images)}/{len(invoice_tokens)}")

        if not valid_images:
            raise Exception("所有发票图片下载失败")

        # 2. OCR 识别
        ocr_results = await ocr_service.batch_recognize_bytes(valid_images)
        logger.info(f"[Webhook] OCR 完成: {sum(1 for r in ocr_results if r.confidence > 0)}/{len(ocr_results)}")

        # 3. 规则检查
        event_start_date = None
        if date_str:
            try:
                event_start_date = date.fromisoformat(date_str[:10])
            except ValueError:
                pass

        rule_result = rule_engine.batch_check(
            ocr_results,
            event_start_date=event_start_date,
        )
        logger.info(f"[Webhook] 规则检查: {rule_result.overall_status}")

        # 4. AI 审核
        employee = EmployeeContext(
            name="飞书用户",
            department="未知部门",
        )
        ai_report = ai_reviewer.review(ocr_results, rule_result, employee, reason=reason)
        logger.info(f"[Webhook] AI 审核: {ai_report.action.value}")

        # 5. 组装报告
        report = _assemble_report(
            instance_code=instance_code,
            ocr_results=ocr_results,
            rule_result=rule_result,
            ai_report=ai_report,
            employee=employee,
        )

        # 6. 推送飞书
        if settings.feishu_bot_webhook:
            await feishu_service.send_review_message(report)
            logger.info(f"[Webhook] 审核结果已推送到飞书群")
        else:
            logger.warning("[Webhook] 未配置飞书机器人 Webhook，跳过推送")

        elapsed = time.time() - start
        logger.info(f"[Webhook] 处理完成: {instance_code}, 耗时 {elapsed:.2f}s")

        return JSONResponse({
            "code": 0,
            "msg": "ok",
            "processing_time": round(elapsed, 2),
            "invoice_count": len(valid_images),
            "rule_status": rule_result.overall_status,
            "ai_action": ai_report.action.value,
        })

    except Exception as e:
        logger.error(f"[Webhook] 处理异常: {e}", exc_info=True)
        return JSONResponse({"code": -1, "msg": str(e)}, status_code=500)


# ====== 辅助函数 ======

def _assemble_report(
    instance_code: str,
    ocr_results: List[OcrResult],
    rule_result: BatchRuleCheckResult,
    ai_report,
    employee: EmployeeContext,
) -> FinalReviewReport:
    """组装最终审核报告"""
    risk_emoji_map = {"low": "\U0001f7e2", "medium": "\U0001f7e1", "high": "\U0001f534"}

    # 构建发票明细
    invoice_details = []
    for i, ocr in enumerate(ocr_results):
        pr = rule_result.per_invoice_results[i] if i < len(rule_result.per_invoice_results) else None
        invoice_details.append({
            "type": ocr.invoice_type.value if ocr.invoice_type else "未知",
            "amount": ocr.amount,
            "seller": ocr.seller_name or "未知",
            "date": str(ocr.issue_date) if ocr.issue_date else "未知",
            "is_passed": pr.is_passed if pr else True,
            "violations": [v.description for v in pr.violations] if pr else [],
        })

    return FinalReviewReport(
        instance_code=instance_code,
        applicant_name=employee.name,
        department=employee.department,
        submit_time=datetime.now().isoformat(),
        total_amount=rule_result.total_amount,
        invoice_count=len(ocr_results),
        invoice_details=invoice_details,
        rule_summary="\u2705 全部规则通过" if rule_result.overall_status == "passed" else f"\u26a0\ufe0f {rule_result.overall_status}",
        ai_action=ai_report.action.value,
        ai_reason=ai_report.reason,
        risk_level=ai_report.risk_level.value,
        risk_emoji=risk_emoji_map.get(ai_report.risk_level.value, "\u2754"),
        processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# ====== 启动 ======

if __name__ == "__main__":
    import uvicorn
    print(f"""
    ========================================
      交通费报销 AI 智能体 v1.1
      http://localhost:{settings.port}
      Debug: http://localhost:{settings.port}/debug/review-test
      OCR Test: POST http://localhost:{settings.port}/debug/ocr-test
      Webhook: POST http://localhost:{settings.port}/webhook/feishu
    ========================================
    """)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.debug,
    )
