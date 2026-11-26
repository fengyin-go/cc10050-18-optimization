"""采集调度与轮次动作接口：把暂停、续跑、签收、回执、交付校验暴露成可调用的 POST 端点。

通用资源路由只支持按钮式动作；轮次相关的操作需要 round_id / page_no 等参数，
因此单独开一组子路由，业务判断仍全部留在 `app/services` 里。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.crawl_schedule import CrawlScheduleService
from app.services.price_crawler import PriceCrawlerService
from app.store import store

router = APIRouter(prefix="/api", tags=["采集调度"])


def _schedule() -> CrawlScheduleService:
    return CrawlScheduleService()


def _crawler() -> PriceCrawlerService:
    return PriceCrawlerService()


def _require_plan(plan_id: int) -> dict[str, Any]:
    plan = store.find("crawl_schedule", plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"调度计划 {plan_id} 不存在")
    return plan


class PausePayload(BaseModel):
    reason: str = ""
    round_id: str | None = None


class RoundPayload(BaseModel):
    round_id: str | None = None


class SignoffPayload(BaseModel):
    round_id: str
    page_no: str
    note: str = ""
    receipt_no: int | None = None


class ReceiptPayload(BaseModel):
    round_id: str
    page_no: str
    note: str = ""
    sign: bool = False


class DeliverPayload(BaseModel):
    task_id: int | None = None
    round_id: str | None = None
    page_no: str | None = None


class VerifyPayload(BaseModel):
    task_id: int = Field(..., description="价格爬取任务 id")
    round_id: str | None = None
    page_no: str | None = None


# ---- 轮次视图 ----


@router.get("/crawl_schedule/{plan_id}/rounds")
def list_rounds(plan_id: int) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().list_rounds(plan_id)


@router.get("/crawl_schedule/{plan_id}/rounds/{round_id}")
def round_detail(plan_id: int, round_id: str) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().round_detail(plan_id, round_id)


@router.get("/crawl_schedule/{plan_id}/page-locations")
def page_locations(plan_id: int, page_no: str | None = None) -> dict[str, Any]:
    """按页面回查归属轮次与每次校验结论。"""
    _require_plan(plan_id)
    return _schedule().locate_pages(plan_id, page_no)


@router.get("/crawl_schedule/{plan_id}/rounds/{round_id}/pages/{page_no}")
def signoff_detail(plan_id: int, round_id: str, page_no: str) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().signoff_detail(plan_id, round_id, page_no)


@router.get("/crawl_schedule/{plan_id}/pause-receipt")
def pause_receipt(plan_id: int, round_id: str | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().pause_receipt(plan_id, round_id)


@router.get("/crawl_schedule/{plan_id}/completion-receipt")
def completion_receipt(plan_id: int, round_id: str | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().completion_receipt(plan_id, round_id)


# ---- 轮次动作 ----


@router.post("/crawl_schedule/{plan_id}/rounds")
def open_round(plan_id: int) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().open_round(plan_id)


@router.post("/crawl_schedule/{plan_id}/round-request")
def round_request(plan_id: int, source: str = "调度队列") -> dict[str, Any]:
    """调度队列请求下一轮：计划暂停时返回明确拒绝说明。"""
    _require_plan(plan_id)
    return _schedule().submit_round_request(plan_id, source)


@router.post("/crawl_schedule/{plan_id}/pause")
def pause_plan(plan_id: int, payload: PausePayload | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    body = payload or PausePayload()
    return _schedule().pause_plan(plan_id, body.reason, body.round_id)


@router.post("/crawl_schedule/{plan_id}/confirm-pause")
def confirm_pause(plan_id: int, payload: RoundPayload | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    body = payload or RoundPayload()
    return _schedule().confirm_pause(plan_id, body.round_id)


@router.post("/crawl_schedule/{plan_id}/resume")
def resume_plan(plan_id: int, payload: RoundPayload | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    body = payload or RoundPayload()
    return _schedule().resume_plan(plan_id, body.round_id)


@router.post("/crawl_schedule/{plan_id}/complete")
def complete_round(plan_id: int, payload: RoundPayload | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    body = payload or RoundPayload()
    return _schedule().complete_round(plan_id, True, body.round_id)


@router.post("/crawl_schedule/{plan_id}/signoff")
def signoff(plan_id: int, payload: SignoffPayload) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().sign_page(
        plan_id, payload.round_id, payload.page_no,
        receipt_no=payload.receipt_no, note=payload.note,
    )


@router.post("/crawl_schedule/{plan_id}/receipt")
def receipt(plan_id: int, payload: ReceiptPayload) -> dict[str, Any]:
    _require_plan(plan_id)
    return _schedule().record_receipt(
        plan_id, payload.round_id, payload.page_no,
        note=payload.note, sign=payload.sign,
    )


@router.post("/crawl_schedule/{plan_id}/verify-deliveries")
def verify_deliveries(plan_id: int, payload: RoundPayload | None = None) -> dict[str, Any]:
    _require_plan(plan_id)
    body = payload or RoundPayload()
    return _schedule().verify_deliveries(plan_id, body.round_id)


# ---- 爬取任务交付资格 ----


@router.get("/price_crawler/{task_id}/operations")
def task_operations(task_id: int, round_id: str | None = None) -> dict[str, Any]:
    """回到原申请时，按实际接收顺序回查该轮的页面操作。"""
    return _crawler().round_operations(task_id, round_id)


@router.post("/price_crawler/{task_id}/deliver-page")
def deliver_page(task_id: int, payload: DeliverPayload | None = None) -> dict[str, Any]:
    body = payload or DeliverPayload()
    return _crawler().deliver_page(task_id, body.page_no, body.round_id)


@router.post("/price_crawler/{task_id}/verify-delivery")
def verify_delivery(task_id: int, payload: VerifyPayload) -> dict[str, Any]:
    """单页交付资格查询：必须带 page_no，返回该页是否有资格交付。"""
    if not payload.page_no:
        return _crawler().deliver_page(task_id, payload.page_no, payload.round_id)
    return _crawler().page_eligibility(task_id, payload.page_no, payload.round_id)
