"""采集轮次共享状态：轮次、页面交付、回执与交付资格的唯一口径。

调度计划（crawl_schedule）与价格爬取任务（price_crawler）都从这里读取轮次状态，
避免两边各写一套判断导致暂停/续跑后的口径漂移。

约定：
- 轮次是页面归属的最小单位，页面一旦归属某个 round_id，暂停、续跑都不迁移；
- 回执、完成回执、样本都按 round_id 归档，不允许跨轮混用；
- 交付资格只有三种：收录、暂挂、需要恢复。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.store import store

# 轮次仍占用调度入口的状态：进行中、已续跑、已暂停。
RUNNING_ROUND_STATUSES = ("进行中", "已续跑")
PAUSED_ROUND_STATUSES = ("已暂停",)
ACTIVE_ROUND_STATUSES = RUNNING_ROUND_STATUSES + PAUSED_ROUND_STATUSES
# 传输已经开始但尚未入库的页面状态。
INFLIGHT_PAGE_STATUSES = ("传输中", "待回执", "已签收")

ELIGIBLE = "收录"
HELD = "暂挂"
NEEDS_RESUME = "需要恢复"


def now_iso() -> str:
    """统一的时间戳口径，精确到秒，便于按回执顺序回查。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- 计划 / 轮次 / 页面定位 ----


def plans_for_task(task_name: str) -> list[dict[str, Any]]:
    return [
        plan for plan in store.rows("crawl_schedule")
        if plan.get("关联任务") == task_name
    ]


def get_round(plan: dict[str, Any] | None, round_id: str | None = None) -> dict[str, Any] | None:
    """按 round_id 精确取轮次；不传时取当前轮次。绝不跨轮兜底。"""
    if not plan:
        return None
    rounds = plan.get("rounds") or []
    if round_id:
        return next((r for r in rounds if r.get("round_id") == round_id), None)
    current = plan.get("current_round_id")
    if current:
        found = next((r for r in rounds if r.get("round_id") == current), None)
        if found is not None:
            return found
    return rounds[-1] if rounds else None


def active_round(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """当前仍占用调度入口的轮次（进行中/已续跑/已暂停）。"""
    if not plan:
        return None
    current = get_round(plan, plan.get("current_round_id"))
    if current is not None and current.get("status") in ACTIVE_ROUND_STATUSES:
        return current
    for rnd in reversed(plan.get("rounds") or []):
        if rnd.get("status") in ACTIVE_ROUND_STATUSES:
            return rnd
    return None


def latest_round(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    rounds = (plan or {}).get("rounds") or []
    return rounds[-1] if rounds else None


def pages_of(rnd: dict[str, Any] | None) -> list[dict[str, Any]]:
    return (rnd or {}).get("pages") or []


def page_by_no(rnd: dict[str, Any] | None, page_no: str) -> dict[str, Any] | None:
    wanted = str(page_no or "").strip().upper()
    return next(
        (p for p in pages_of(rnd) if str(p.get("page_no", "")).upper() == wanted),
        None,
    )


def next_plan_order(plan: dict[str, Any]) -> int:
    plan["event_seq"] = int(plan.get("event_seq", 0) or 0) + 1
    return int(plan["event_seq"])


def record_round_event(
    plan: dict[str, Any],
    rnd: dict[str, Any] | None,
    kind: str,
    *,
    page_no: str | None = None,
    note: str = "",
    at: str | None = None,
    order: int | None = None,
) -> dict[str, Any]:
    """把签收/回执/暂停/续跑等事件写入轮次事件流。

    调用方已经为回执分配过序号时传 order，避免一笔回执占用两个事件序号。
    """
    event = {
        "order": order if order is not None else next_plan_order(plan),
        "kind": kind,
        "page_no": page_no,
        "at": at or now_iso(),
        "note": note,
    }
    if rnd is not None:
        rnd.setdefault("events", []).append(event)
    return event


# ---- 页面交付状态 ----


def page_is_stored(page: dict[str, Any]) -> bool:
    sample_id = page.get("stored_sample_id")
    if sample_id in (None, ""):
        return False
    try:
        sample = store.find("sample_store", int(sample_id))
    except (TypeError, ValueError):
        return False
    return sample is not None


def receipt_progress(page: dict[str, Any]) -> tuple[int, int]:
    expected = int(page.get("expected_receipts", 1) or 1)
    received = len(page.get("receipts") or [])
    return received, expected


def page_is_inflight(page: dict[str, Any]) -> bool:
    """在途：传输已开始、尚未入库、且仍在等回执。"""
    return (
        not page_is_stored(page)
        and bool(page.get("transfer_started_at") or page.get("receipts"))
        and page.get("delivery_status") in INFLIGHT_PAGE_STATUSES
    )


def plan_is_paused(plan: dict[str, Any] | None, rnd: dict[str, Any] | None = None) -> bool:
    if (plan or {}).get("status") == "已暂停":
        return True
    return (rnd or {}).get("status") == "已暂停"


def page_eligibility(
    plan: dict[str, Any],
    rnd: dict[str, Any],
    page: dict[str, Any],
) -> dict[str, Any]:
    """校验一笔页面交付是否仍具资格，返回 收录 / 暂挂 / 需要恢复。"""
    received, expected = receipt_progress(page)
    stored = page_is_stored(page)
    paused = plan_is_paused(plan, rnd)
    reasons: list[str] = []
    if stored:
        state = ELIGIBLE
        reasons.append(f"页面已签收并入库（样本 {page.get('stored_sample_id')}），保留在原轮次 {rnd.get('round_id')}")
        if paused:
            reasons.append("计划已暂停，但已入库页面不回收、不迁移")
    elif paused:
        if received >= expected:
            state = HELD
            reasons.append(f"回执已齐（{received}/{expected}），但轮次已暂停，页面暂挂")
            reasons.append("续跑旧轮后才允许交付下游")
        else:
            state = NEEDS_RESUME
            reasons.append("轮次已暂停，在途页面必须续跑旧轮后才能继续")
            reasons.append(f"回执未齐（{received}/{expected}），页面仍在等待后续回执")
    elif rnd.get("status") in ("进行中", "已续跑"):
        if received >= expected:
            state = ELIGIBLE
            reasons.append(f"回执已齐（{received}/{expected}），轮次进行中，交付资格有效")
        else:
            state = HELD
            reasons.append(f"仍在等待后续回执（{received}/{expected}）")
            reasons.append("页面处于传输中，暂不能交付下游")
    else:
        state = HELD
        reasons.append(f"轮次状态为 {rnd.get('status')}，页面暂挂待人工处理")
        if received < expected:
            reasons.append(f"回执未齐（{received}/{expected}）")
    return {
        "state": state,
        "eligible": state == ELIGIBLE,
        "round_id": rnd.get("round_id"),
        "round_status": rnd.get("status"),
        "page_no": page.get("page_no"),
        "delivery_status": page.get("delivery_status"),
        "stored": stored,
        "stored_sample_id": page.get("stored_sample_id"),
        "receipts": f"{received}/{expected}",
        "received": received,
        "expected": expected,
        "signed": bool(page.get("signed_at")),
        "reasons": reasons,
        "checked_at": now_iso(),
    }


def round_eligibility(plan: dict[str, Any], rnd: dict[str, Any]) -> list[dict[str, Any]]:
    return [page_eligibility(plan, rnd, page) for page in pages_of(rnd)]


def summarize_states(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        ELIGIBLE: sum(1 for item in results if item.get("state") == ELIGIBLE),
        HELD: sum(1 for item in results if item.get("state") == HELD),
        NEEDS_RESUME: sum(1 for item in results if item.get("state") == NEEDS_RESUME),
    }


# ---- 样本与核对 ----


def store_page_sample(
    plan: dict[str, Any],
    rnd: dict[str, Any],
    page: dict[str, Any],
    *,
    at: str | None = None,
) -> dict[str, Any]:
    """把已签收页面写入样本库；重复调用是幂等的，不覆盖旧样本。"""
    if page_is_stored(page):
        return store.find("sample_store", int(page["stored_sample_id"])) or {}
    rows = store.rows("sample_store")
    sample_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    stored_at = at or now_iso()
    sample = {
        "id": sample_id,
        "status": "已保存",
        "pending": True,
        "abnormal": False,
        "样本批次": f"{rnd.get('round_id')}-{page.get('page_no')}",
        "站点名称": plan.get("目标站点") or plan.get("关联任务", ""),
        "原始HTML摘要": f"{rnd.get('round_id')} {page.get('page_no')} 页页面样本",
        "抓取时间": stored_at,
        "样本指纹": f"fp-{rnd.get('round_id')}-{page.get('page_no')}",
        "round_id": rnd.get("round_id"),
        "plan_id": plan.get("id"),
        "page_no": page.get("page_no"),
    }
    rows.append(sample)
    page["stored_sample_id"] = sample_id
    page["stored_at"] = stored_at
    page["delivery_status"] = "已入库"
    page["pending"] = True
    return sample


def samples_for_round(rnd: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not rnd:
        return []
    round_id = rnd.get("round_id")
    return [
        row for row in store.rows("sample_store")
        if row.get("round_id") == round_id
    ]


def sample_check(rnd: dict[str, Any] | None, *, since: str | None = None) -> dict[str, Any]:
    """核对轮次样本是否仍在库，暂停不应让已入库页面消失。"""
    pages = pages_of(rnd)
    stored_pages = []
    missing = []
    for page in pages:
        if page_is_stored(page):
            stored_pages.append({
                "page_no": page.get("page_no"),
                "sample_id": page.get("stored_sample_id"),
                "stored_at": page.get("stored_at"),
            })
        elif page.get("stored_sample_id") not in (None, ""):
            missing.append({
                "page_no": page.get("page_no"),
                "sample_id": page.get("stored_sample_id"),
            })
    samples = samples_for_round(rnd)
    after_pause = [
        sample for sample in samples
        if since and str(sample.get("抓取时间", "")) >= since
    ]
    return {
        "round_id": (rnd or {}).get("round_id"),
        "stored_pages": stored_pages,
        "missing_samples": missing,
        "sample_count": len(samples),
        "samples_after_pause": [
            {"sample_id": s.get("id"), "page_no": s.get("page_no"), "stored_at": s.get("抓取时间")}
            for s in after_pause
        ],
        "intact": not missing,
    }


# ---- 旧轮可做的操作（按实际接收顺序） ----


def first_receipt_order(page: dict[str, Any]) -> int | None:
    orders = [
        int(receipt.get("order", 0))
        for receipt in page.get("receipts") or []
        if receipt.get("order") is not None
    ]
    return min(orders) if orders else None


def page_available_actions(eligibility: dict[str, Any], page: dict[str, Any], rnd: dict[str, Any]) -> list[str]:
    state = eligibility.get("state")
    if rnd.get("status") == "已完成":
        return ["查看完成回执"]
    if state == ELIGIBLE:
        return ["查看样本"] if page_is_stored(page) else ["交付页面"]
    if state == HELD:
        if eligibility.get("received", 0) < eligibility.get("expected", 1):
            return ["登记回执"]
        return ["继续计划"] if plan_is_paused(None, rnd) else ["交付页面"]
    # 需要恢复
    return ["继续计划", "登记回执"]


def ordered_page_operations(
    plan: dict[str, Any],
    rnd: dict[str, Any],
) -> list[dict[str, Any]]:
    """按实际接收顺序给出页面操作；没有回执的页面排在已回执页面之后。"""
    entries: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
    for index, page in enumerate(pages_of(rnd)):
        received, expected = receipt_progress(page)
        eligibility = page_eligibility(plan, rnd, page)
        first = first_receipt_order(page)
        if first is not None:
            key = (0, first, index)
            basis = f"回执顺序 {first}"
        else:
            transfer_order = int(page.get("transfer_order", 0) or 0)
            key = (1, transfer_order or 10_000 + index, index)
            basis = "尚无回执，按传输开始顺序" if transfer_order else "尚无回执，按页面清单顺序"
        entries.append((key, {
            "page_no": page.get("page_no"),
            "page_status": page.get("delivery_status"),
            "receipt_order": first,
            "receipts": f"{received}/{expected}",
            "received": received,
            "expected": expected,
            "signed": bool(page.get("signed_at")),
            "stored_sample_id": page.get("stored_sample_id"),
            "eligibility": eligibility["state"],
            "eligible": eligibility["eligible"],
            "reasons": eligibility["reasons"],
            "available_actions": page_available_actions(eligibility, page, rnd),
            "order_basis": basis,
        }))
    entries.sort(key=lambda item: item[0])
    operations = []
    for sequence, (_, item) in enumerate(entries, start=1):
        item["sequence"] = sequence
        operations.append(item)
    return operations
