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
import asyncio

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
# 避免重复添加文件日志sink（main.py会被导入两次：__main__ 和 main:app）
import sys as _sys
if not getattr(_sys, "_file_sink_added", False):
    logger.add("logs/app.log", rotation="10 MB", retention="7 days")
    _sys._file_sink_added = True


# ====== 初始化服务实例（全局复用连接池等）======
ocr_service = OcrService()
rule_engine = RuleEngine()
ai_reviewer = AiReviewer()
feishu_service = FeishuService()

# ====== 去重：防止飞书重复推送导致群机器人发多次消息 ======
# 飞书审批事件可能推送多次（如PENDING状态变更），用实例code去重，5分钟自动过期
_processed_instances: dict = {}  # {instance_code: timestamp}
# 后台任务追踪：防止同一个实例被重复创建后台任务
_pending_tasks: dict = {}  # {instance_code: asyncio.Task}


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
        "report": report.model_dump(mode="json"),
        "ai_detail": ai_report.model_dump(mode="json"),
        "rule_detail": rule_result.model_dump(mode="json"),
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
            "ocr_results": [r.model_dump(mode="json") for r in ocr_results],
            "rule_detail": rule_result.model_dump(mode="json"),
            "ai_detail": ai_report.model_dump(mode="json"),
            "report": report.model_dump(mode="json"),
        }

    except Exception as e:
        logger.error(f"[Debug-OCR] 测试失败: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/debug/subscribe-approval")
async def debug_subscribe_approval(request: Request):
    """
    订阅审批事件。飞书要求调用此API一次才能接收审批事件回调。
    
    请求体: {"approval_code": "58EAD7E5-FADD-465F-8680-124833F13E2A"}
    """
    body = await request.json()
    approval_code = body.get("approval_code", "")

    if not approval_code:
        return JSONResponse({"error": "请提供 approval_code"}, status_code=400)

    try:
        result = await feishu_service.subscribe_approval(approval_code)
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Debug] 订阅审批事件失败: {e}")
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

    # 只处理审批实例事件（包括创建和状态变更）
    if event_type and not any(kw in event_type.lower() for kw in ["create", "approval_instance"]):
        logger.info(f"[Webhook] 非审批实例事件({event_type})，跳过")
        return JSONResponse({"code": 0, "msg": "ignored", "event_type": event_type})

    # ===== 去重 + 后台异步：立即返回200避免飞书超时重试 =====
    now = time.time()
    if instance_code and instance_code in _pending_tasks:
        elapsed = now - _processed_instances.get(instance_code, now)
        if elapsed < 300:
            logger.info(f"[Webhook] 重复事件({instance_code})，后台任务已存在({elapsed:.0f}s前)，跳过")
            return JSONResponse({"code": 0, "msg": "duplicate", "instance_code": instance_code})

    if instance_code:
        _processed_instances[instance_code] = now

    # 启动后台异步任务处理审核流程
    if instance_code:
        invoice_tokens = approval_info.get("invoice_file_tokens", [])
        reason = approval_info.get("reason", "")
        date_str = approval_info.get("event_start_date_str", "")
        
        task = asyncio.create_task(
            _process_approval_async(
                instance_code, invoice_tokens, reason, date_str,
                feishu_service, ocr_service, rule_engine, ai_reviewer
            )
        )
        _pending_tasks[instance_code] = task
        logger.info(f"[Webhook] 后台任务已启动: {instance_code}")

    # 立即返回200，避免飞书超时重试
    return JSONResponse({
        "code": 0,
        "msg": "accepted",
        "instance_code": instance_code,
    })


async def _process_approval_async(
    instance_code: str,
    invoice_tokens: list,
    reason: str,
    date_str: str,
    feishu_svc,
    ocr_svc,
    rule_eng,
    ai_svc,
):
    """后台异步处理审核全流程。"""
    start = time.time()
    logger.info(f"[Async] === 开始处理审批实例: {instance_code} ===")
    try:
        image_data_list = []

        # ===== 事件体不含表单数据时，通过API兜底获取 =====
        if not invoice_tokens:
            logger.info(f"[Async] 事件体无表单数据，通过API获取审批实例 {instance_code}")
            detail = await feishu_svc.get_instance_detail(instance_code)
            form_data = FeishuService.extract_form_from_instance(detail)
            image_urls = form_data.get("image_urls", [])
            reason = form_data.get("reason") or reason
            date_str = form_data.get("event_start_date_str") or date_str

            logger.info(
                f"[Async] API兜底: {len(image_urls)} 张图片, "
                f"事由: {reason}, 日期: {date_str}"
            )

            if not image_urls:
                logger.warning("[Async] 未找到发票图片，跳过")
                return

            image_data_list = await feishu_svc.download_images_from_urls(image_urls)
        else:
            image_data_list = await feishu_svc.download_multiple_images(invoice_tokens)

        # ===== 核心处理链 =====
        valid_images = [img for img in image_data_list if img is not None]
        logger.info(f"[Async] 图片下载完成: {len(valid_images)}/{len(image_data_list)}")

        if not valid_images:
            raise Exception("所有发票图片下载失败")

        # 2. OCR 识别
        ocr_results = await ocr_svc.batch_recognize_bytes(valid_images)
        logger.info(f"[Async] OCR 完成: {sum(1 for r in ocr_results if r.confidence > 0)}/{len(ocr_results)}")

        # 3. 规则检查
        event_start_date = None
        if date_str:
            try:
                event_start_date = date.fromisoformat(date_str[:10])
            except ValueError:
                pass

        rule_result = rule_eng.batch_check(ocr_results, event_start_date=event_start_date)
        logger.info(f"[Async] 规则检查: {rule_result.overall_status}")

        # 4. AI 审核
        employee = EmployeeContext(name="飞书用户", department="未知部门")
        ai_report = ai_svc.review(ocr_results, rule_result, employee, reason=reason)
        logger.info(f"[Async] AI 审核: {ai_report.action.value}")

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
            await feishu_svc.send_review_message(report)
            logger.info(f"[Async] 审核结果已推送到飞书群")

        elapsed = time.time() - start
        logger.info(f"[Async] 处理完成: {instance_code}, 耗时 {elapsed:.2f}s")

    except Exception as e:
        logger.error(f"[Async] 处理异常: {instance_code}: {e}", exc_info=True)
    finally:
        _pending_tasks.pop(instance_code, None)


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
