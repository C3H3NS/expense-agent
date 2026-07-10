"""
FastAPI 主入口 —— 交通费报销 AI 智能体

处理链路：
  飞书审批回调 → 解析数据 → OCR识别 → 规则检查 → AI审核 → 推送飞书
"""
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
import time
import json
from datetime import datetime

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
    """手动触发一次完整审核流程（用于开发调试）"""
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
    risk_emoji_map = {"low": "\U0001f7e2", "medium": "\U0001f7e1", "high": "\U0001f534"}
    report = FinalReviewReport(
        instance_code="TEST-001",
        applicant_name=test_employee.name,
        department=test_employee.department,
        submit_time=datetime.now().isoformat(),
        total_amount=rule_result.total_amount,
        invoice_count=len(test_ocr_results),
        invoice_details=[
            {
                "type": ocr.invoice_type.value if ocr.invoice_type else "加油票",
                "amount": ocr.amount,
                "seller": ocr.seller_name,
                "date": str(ocr.issue_date),
                "is_passed": True,
                "violations": [],
            }
            for ocr in test_ocr_results
        ],
        rule_summary="\u2705 全部规则通过" if rule_result.overall_status == "passed" else f"\u26a0\ufe0f {rule_result.overall_status}",
        ai_action=ai_report.action.value,
        ai_reason=ai_report.reason,
        risk_level=ai_report.risk_level.value,
        risk_emoji=risk_emoji_map.get(ai_report.risk_level.value, "\u2754"),
        processed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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


# ====== 飞书 Webhook 入口 ======

@app.post("/webhook/feishu")
async def feishu_webhook(request: Request):
    """
    飞书审批事件回调。

    触发时机：员工在飞书提交/更新交通费报销审批单时
    """
    body = await request.json()
    logger.info(f"[Webhook] 收到回调: event={body.get('type', '?')}")

    try:
        # 解析回调数据
        approval_info = FeishuService.parse_approval_webhook(body)
        instance_code = approval_info["instance_code"]

        logger.info(f"[Webhook] 处理审批实例: {instance_code}")

        # TODO: 从 body 里提取：
        #   - 发票图片 URLs
        #   - 申请人信息
        #   - 表单字段（事由等）
        # 这些取决于你在飞书审批里建的表单结构
        #
        # 大致是这样的：
        # form_data = extract_form_data(body)
        # invoice_urls = form_data.get("invoice_images", [])
        # applicant = form_data.get("applicant", {})
        # reason = form_data.get("reason", "")

        # ===== 核心处理链 =====
        start = time.time()

        # 1. OCR 识别
        # ocr_results = await ocr_service.batch_recognize(invoice_urls)

        # 2. 规则检查
        # rule_result = rule_engine.batch_check(ocr_results, event_start_date=...)

        # 3. AI 审核
        # employee_ctx = EmployeeContext(name=..., department=...)
        # ai_report = ai_reviewer.review(ocr_results, rule_result, employee_ctx, reason=reason)

        # 4. 组装报告
        # report = assemble_final_report(instance_code, ..., rule_result, ai_report)

        # 5. 推送飞书
        # await feishu_service.send_review_message(report)

        elapsed = time.time() - start
        logger.info(f"[Webhook] 处理完成: {instance_code}, 耗时 {elapsed:.2f}s")

        return JSONResponse({"code": 0, "msg": "ok", "processing_time": round(elapsed, 2)})

    except Exception as e:
        logger.error(f"[Webhook] 处理异常: {e}", exc_info=True)
        return JSONResponse({"code": -1, "msg": str(e)}, status_code=500)


# ====== 启动 ======

if __name__ == "__main__":
    import uvicorn
    print(f"""
    ========================================
      交通费报销 AI 智能体 v1.0
      http://localhost:{settings.port}
      Debug: http://localhost:{settings.port}/debug/review-test
    ========================================
    """)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=settings.debug,
    )
