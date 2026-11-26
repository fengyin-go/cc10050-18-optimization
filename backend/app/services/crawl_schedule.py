"""采集调度计划领域服务：按周期调度价格爬取任务的执行，负责计划触发、状态回写与失败重排。

数据链路位置：调度编排层，把爬取任务按周期组织成执行计划，触发结果回写
上次状态，并把执行量同步到采集量指标。

轮次口径：
- 每次调度开启一个执行轮次（round），页面、签收、回执、样本都挂在轮次上；
- 暂停只冻结轮次入口，不迁移页面、不回收样本；
- 暂停期间拒绝新建轮次，续跑只能继续被暂停的旧轮；
- 完成回执按 round_id 归档，旧轮回执不会回传给恢复后的新轮。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.services import crawl_state as cs
from app.services.base import BaseModuleService
from app.store import store


class CrawlScheduleService(BaseModuleService):
    MODULE = "crawl_schedule"
    ENTITY = "调度计划"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["计划名称", "关联任务", "调度周期", "下次执行", "上次状态"]
    REQUIRED_FIELDS = ["计划名称", "关联任务", "调度周期"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "执行失败"]
    ACTION_RULES = {
        "立即执行": "执行中",
        "暂停计划": "待执行",
        "继续计划": "执行中",
        "完成轮次": "已完成",
        "失败回写": "执行失败",
    }
    NEGATIVE_ACTIONS = ["失败回写"]

    # ---- 内部工具 ----

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表只给轮次摘要，完整轮次/页面/回执走 /rounds 子接口。"""
        rows, total = super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size,
        )
        summary = []
        for row in rows:
            view = {key: value for key, value in row.items() if key not in ("rounds", "pause_receipt")}
            current = cs.get_round(row)
            view["round_count"] = len(row.get("rounds") or [])
            view["current_round_status"] = (current or {}).get("status")
            view["current_round_pages"] = len(cs.pages_of(current))
            summary.append(view)
        return summary, total

    @staticmethod
    def _linked_task(plan: dict[str, Any]) -> dict[str, Any] | None:
        task_name = plan.get("关联任务", "")
        return next(
            (t for t in store.rows("price_crawler") if t.get("任务名称") == task_name),
            None,
        )

    @staticmethod
    def _task_site(task: dict[str, Any] | None, plan: dict[str, Any]) -> str:
        if task and task.get("目标站点"):
            return str(task["目标站点"])
        return str(plan.get("目标站点", "") or "")

    def _sync_task_status(self, plan: dict[str, Any], status: str) -> None:
        task = self._linked_task(plan)
        if task is not None:
            task["status"] = status
            task["pending"] = status == "运行中"
            task["round_id"] = plan.get("current_round_id")

    @staticmethod
    def _fresh_completion_receipt(
        rnd: dict[str, Any],
    ) -> dict[str, Any] | None:
        """完成回执里的样本核对按当前样本库重算，避免示例数据与库内不一致。"""
        receipt = rnd.get("completion_receipt")
        if not receipt:
            return None
        return {**receipt, "sample_check": cs.sample_check(rnd)}

    def _round_view(self, plan: dict[str, Any], rnd: dict[str, Any]) -> dict[str, Any]:
        pages = []
        for page in cs.pages_of(rnd):
            received, expected = cs.receipt_progress(page)
            pages.append({
                "page_no": page.get("page_no"),
                "delivery_status": page.get("delivery_status"),
                "signed": bool(page.get("signed_at")),
                "signed_at": page.get("signed_at"),
                "receipts": f"{received}/{expected}",
                "received": received,
                "expected": expected,
                "stored_sample_id": page.get("stored_sample_id"),
                "stored_at": page.get("stored_at"),
                "transfer_started_at": page.get("transfer_started_at"),
                "inflight": cs.page_is_inflight(page),
            })
        return {
            "round_id": rnd.get("round_id"),
            "round_seq": rnd.get("round_seq"),
            "status": rnd.get("status"),
            "opened_at": rnd.get("opened_at"),
            "paused_at": rnd.get("paused_at"),
            "resumed_at": rnd.get("resumed_at"),
            "completed_at": rnd.get("completed_at"),
            "pause_landing": rnd.get("pause_landing"),
            "pages": pages,
            "page_count": len(pages),
            "signed_pages": sum(1 for p in pages if p["signed"]),
            "stored_pages": sum(1 for p in pages if p["stored_sample_id"] is not None),
            "pending_receipts": sum(1 for p in pages if p["received"] < p["expected"]),
            "has_pause_receipt": bool(rnd.get("pause_receipt")),
            "has_completion_receipt": bool(rnd.get("completion_receipt")),
            "events": list(rnd.get("events") or []),
        }

    def _pause_receipt(self, plan: dict[str, Any], rnd: dict[str, Any]) -> dict[str, Any]:
        pages = cs.pages_of(rnd)
        conflicts = [
            {
                "page_no": page.get("page_no"),
                "delivery_status": page.get("delivery_status"),
                "transfer_started_at": page.get("transfer_started_at"),
                "receipts": "%d/%d" % cs.receipt_progress(page),
                "message": f"页面 {page.get('page_no')} 已开始传输，暂停在回执落点前生效",
            }
            for page in pages if cs.page_is_inflight(page)
        ]
        signed = [
            {
                "page_no": page.get("page_no"),
                "signed_at": page.get("signed_at"),
                "stored_sample_id": page.get("stored_sample_id"),
                "kept_in_round": rnd.get("round_id"),
            }
            for page in pages if page.get("signed_at")
        ]
        return {
            "kind": "pause_receipt",
            "plan_id": plan.get("id"),
            "plan_name": plan.get("计划名称", ""),
            "round_id": rnd.get("round_id"),
            "paused_at": rnd.get("paused_at"),
            "pause_landing": rnd.get("pause_landing"),
            "signed_pages": signed,
            "inflight_pages": conflicts,
            "pending_receipts": [
                {
                    "page_no": page.get("page_no"),
                    "receipts": "%d/%d" % cs.receipt_progress(page),
                    "awaiting": max(cs.receipt_progress(page)[1] - cs.receipt_progress(page)[0], 0),
                }
                for page in pages
                if cs.receipt_progress(page)[0] < cs.receipt_progress(page)[1]
            ],
            "sample_check": cs.sample_check(rnd, since=rnd.get("paused_at")),
            "next_round_blocked": True,
        }

    def _round_lookup(
        self,
        entry_id: int,
        round_id: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return None, None, f"计划 {entry_id} 不存在"
        rnd = cs.get_round(plan, round_id) if round_id else cs.get_round(plan)
        if rnd is None:
            target = round_id or "当前轮次"
            return plan, None, f"计划 {entry_id} 的{target}不存在或已归档"
        return plan, rnd, None

    # ---- 轮次生命周期 ----

    def open_round(self, entry_id: int) -> dict[str, Any]:
        """开启新一轮执行；暂停中或旧轮仍在途时拒绝调度。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        if plan.get("paused"):
            paused_round = plan.get("paused_round_id") or ""
            return {
                "ok": False,
                "paused": True,
                "paused_round_id": paused_round,
                "message": (
                    f"计划「{plan.get('计划名称', '')}」已暂停"
                    f"（暂停轮次 {paused_round}），拒绝新建轮次；请先恢复旧轮"
                ),
            }
        active = cs.active_round(plan)
        if active is not None:
            if active.get("status") == "已暂停":
                return {
                    "ok": False,
                    "paused": True,
                    "paused_round_id": active.get("round_id"),
                    "message": f"轮次 {active.get('round_id')} 已暂停，不能新建轮次；请先继续旧轮",
                }
            return {
                "ok": False,
                "active_round_id": active.get("round_id"),
                "message": f"轮次 {active.get('round_id')} 尚未完成，不能重复开轮",
            }
        task = self._linked_task(plan)
        if task is None:
            plan["status"] = "执行失败"
            plan["上次状态"] = "关联任务不存在"
            plan["abnormal"] = True
            return {"ok": False, "message": f"关联任务 {plan.get('关联任务', '')} 不存在"}
        rounds = plan.setdefault("rounds", [])
        seq = max((int(r.get("round_seq", 0)) for r in rounds), default=0) + 1
        round_id = f"{plan.get('id')}-R{seq}"
        now = cs.now_iso()
        rnd = {
            "round_id": round_id,
            "round_seq": seq,
            "plan_id": plan.get("id"),
            "task_name": plan.get("关联任务", ""),
            "site": self._task_site(task, plan),
            "status": "进行中",
            "opened_at": now,
            "paused_at": None,
            "resumed_at": None,
            "completed_at": None,
            "pause_landing": None,
            "pages": [],
            "events": [],
            "pause_receipt": None,
            "completion_receipt": None,
            "delivery_checks": [],
        }
        rounds.append(rnd)
        plan["current_round_id"] = round_id
        plan["target_site"] = rnd["site"]
        plan["status"] = "执行中"
        plan["pending"] = True
        plan["abnormal"] = False
        plan["上次状态"] = "执行中"
        cs.record_round_event(plan, rnd, "开轮", note=f"轮次 {round_id} 开始调度")
        self._sync_task_status(plan, "运行中")
        return {
            "ok": True,
            "message": f"新一轮 {round_id} 已开始",
            "round": self._round_view(plan, rnd),
        }

    def execute_now(self, entry_id: int) -> dict[str, Any]:
        """立即执行计划：在新轮次里联动关联爬取任务进入运行中。"""
        result = self.open_round(entry_id)
        if result.get("ok"):
            result["message"] = "计划已触发执行"
            task = self._linked_task(store.find(self.MODULE, entry_id) or {})
            result["task"] = task.get("任务名称") if task else None
        return result

    def pause_plan(self, entry_id: int, reason: str = "", round_id: str | None = None) -> dict[str, Any]:
        """暂停当前轮次：冻结调度入口，保留已签收页面与在途页面。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        rnd = cs.get_round(plan, round_id or plan.get("current_round_id"))
        if rnd is None:
            return {"ok": False, "message": "当前没有可暂停的轮次"}
        if rnd.get("status") == "已暂停" and plan.get("paused"):
            receipt = rnd.get("pause_receipt") or self._pause_receipt(plan, rnd)
            return {
                "ok": True,
                "message": f"计划已处于暂停状态，轮次 {rnd.get('round_id')} 的暂停落点未变化",
                "pause_receipt": receipt,
                "conflicts": receipt.get("inflight_pages", []),
            }
        if rnd.get("status") == "已完成":
            return {"ok": False, "message": f"轮次 {rnd.get('round_id')} 已完成，不能暂停"}
        now = cs.now_iso()
        event = cs.record_round_event(
            plan, rnd, "暂停", note=reason or "计划工作台暂停计划",
        )
        rnd["status"] = "已暂停"
        rnd["paused_at"] = now
        rnd["pause_landing"] = event["order"]
        plan["status"] = "待执行"
        plan["pending"] = True
        plan["paused"] = True
        plan["paused_at"] = now
        plan["paused_round_id"] = rnd.get("round_id")
        plan["pause_reason"] = reason
        plan["上次状态"] = "已暂停"
        self._sync_task_status(plan, "已暂停")
        receipt = self._pause_receipt(plan, rnd)
        rnd["pause_receipt"] = receipt
        conflicts = receipt["inflight_pages"]
        message = f"计划已暂停，轮次 {rnd.get('round_id')} 共 {len(receipt['signed_pages'])} 个已签收页面保留在原轮次"
        if conflicts:
            message += f"；{len(conflicts)} 个在途页面发生暂停冲突，需值班员确认暂停落点"
        return {
            "ok": True,
            "message": message,
            "paused": True,
            "needs_confirmation": bool(conflicts),
            "conflicts": conflicts,
            "pause_landing": event["order"],
            "pause_receipt": receipt,
        }

    def confirm_pause(self, entry_id: int, round_id: str | None = None) -> dict[str, Any]:
        """值班员确认暂停状态：记录确认人与落点，不改变页面归属。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        rnd = cs.get_round(plan, round_id or plan.get("paused_round_id"))
        if rnd is None or rnd.get("status") != "已暂停":
            return {"ok": False, "message": "计划不在暂停状态，无需确认"}
        now = cs.now_iso()
        rnd["pause_confirmed_at"] = now
        rnd["pause_confirmed_by"] = "值班员"
        plan["pause_confirmed_at"] = now
        event = cs.record_round_event(plan, rnd, "确认暂停", note="值班员确认暂停状态")
        return {
            "ok": True,
            "message": f"已确认轮次 {rnd.get('round_id')} 的暂停状态",
            "round_id": rnd.get("round_id"),
            "pause_landing": rnd.get("pause_landing"),
            "confirmed_event": event["order"],
        }

    def resume_plan(self, entry_id: int, round_id: str | None = None) -> dict[str, Any]:
        """解除暂停：只续跑被暂停的旧轮，不新建轮次、不迁移页面。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        paused_round_id = plan.get("paused_round_id")
        if not plan.get("paused"):
            return {"ok": False, "message": "计划未处于暂停状态，无需续跑"}
        if round_id and paused_round_id and round_id != paused_round_id:
            return {
                "ok": False,
                "message": f"只能继续被暂停的旧轮 {paused_round_id}，不能续跑轮次 {round_id}",
            }
        rnd = cs.get_round(plan, paused_round_id)
        if rnd is None:
            return {"ok": False, "message": f"暂停轮次 {paused_round_id} 不存在"}
        if rnd.get("status") == "已完成":
            return {"ok": False, "message": f"旧轮 {rnd.get('round_id')} 已完成，不能继续，需新建轮次"}
        now = cs.now_iso()
        rnd["status"] = "已续跑"
        rnd["resumed_at"] = now
        plan["paused"] = False
        plan["paused_at"] = None
        plan["status"] = "执行中"
        plan["pending"] = True
        plan["current_round_id"] = rnd.get("round_id")
        plan["上次状态"] = "已续跑"
        self._sync_task_status(plan, "运行中")
        event = cs.record_round_event(plan, rnd, "续跑", note="恢复旧轮继续执行")
        results = cs.round_eligibility(plan, rnd)
        signed = [
            {
                "page_no": page.get("page_no"),
                "signed_at": page.get("signed_at"),
                "stored_sample_id": page.get("stored_sample_id"),
            }
            for page in cs.pages_of(rnd) if page.get("signed_at")
        ]
        resume_receipt = {
            "kind": "resume_receipt",
            "plan_id": plan.get("id"),
            "plan_name": plan.get("计划名称", ""),
            "round_id": rnd.get("round_id"),
            "resumed_at": now,
            "pause_landing": rnd.get("pause_landing"),
            "resumed_event": event["order"],
            "signed_pages": signed,
            "sample_check": cs.sample_check(rnd),
            "summary": cs.summarize_states(results),
        }
        rnd["resume_receipt"] = resume_receipt
        return {
            "ok": True,
            "message": f"已解除暂停，继续旧轮 {rnd.get('round_id')}",
            "round_id": rnd.get("round_id"),
            "resumed_event": event["order"],
            "resume_receipt": resume_receipt,
            "eligibility": results,
            "summary": resume_receipt["summary"],
            "page_operations": cs.ordered_page_operations(plan, rnd),
        }

    def submit_round_request(self, entry_id: int, source: str = "调度队列") -> dict[str, Any]:
        """调度队列请求新轮次：暂停期间明确拒绝并说明暂停轮次。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        if plan.get("paused"):
            paused_round = plan.get("paused_round_id") or cs.active_round(plan)
            stopped = cs.record_round_event(
                plan, cs.get_round(plan, paused_round),
                "拒绝新轮请求",
                note=f"{source}请求新轮次，计划已暂停",
            )
            return {
                "ok": False,
                "accepted": False,
                "paused": True,
                "paused_round_id": paused_round,
                "rejected_event": stopped["order"],
                "message": (
                    f"{source}请求新轮次被拒绝：计划「{plan.get('计划名称', '')}」已暂停"
                    f"（暂停轮次 {paused_round}），需先继续旧轮"
                ),
            }
        result = self.open_round(entry_id)
        result["source"] = source
        result["accepted"] = bool(result.get("ok"))
        return result

    def complete_round(
        self,
        entry_id: int,
        ok: bool = True,
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """按轮次回写完成回执；暂停中的轮次必须先续跑。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        rnd = cs.get_round(plan, round_id or plan.get("current_round_id"))
        if rnd is None:
            return {"ok": False, "message": f"轮次 {round_id or '当前轮次'} 不存在"}
        if rnd.get("status") == "已暂停" or (plan.get("paused") and plan.get("paused_round_id") == rnd.get("round_id")):
            return {
                "ok": False,
                "message": f"轮次 {rnd.get('round_id')} 已暂停，需先继续旧轮再回写完成回执",
            }
        if rnd.get("completion_receipt"):
            existing = self._fresh_completion_receipt(rnd) or rnd["completion_receipt"]
            return {
                "ok": False,
                "message": f"轮次 {rnd.get('round_id')} 已回写完成回执（{existing.get('completed_at')}），不会覆盖或串到其他轮次",
                "completion_receipt": existing,
            }
        if ok:
            pending = [
                page.get("page_no") for page in cs.pages_of(rnd)
                if not page.get("signed_at") or not cs.page_is_stored(page)
            ]
            if pending:
                return {
                    "ok": False,
                    "round_id": rnd.get("round_id"),
                    "pending_pages": pending,
                    "message": (
                        f"轮次 {rnd.get('round_id')} 还有未签收入库页面（{'、'.join(pending)}），"
                        "不能回写成功完成回执"
                    ),
                }
        already_done = rnd.get("status") == "已完成"
        if already_done and not rnd.get("completion_receipt"):
            return {"ok": False, "message": f"轮次 {rnd.get('round_id')} 已完成"}
        period = str(plan.get("调度周期", "60m") or "60m")
        try:
            minutes = int(period.rstrip("m")) if period.endswith("m") else int(period)
        except ValueError:
            minutes = 60
        now = cs.now_iso()
        event = cs.record_round_event(
            plan, rnd, "完成回写", note="成功" if ok else "失败",
        )
        next_run = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(
            timespec="seconds")
        receipt = {
            "kind": "completion_receipt",
            "plan_id": plan.get("id"),
            "plan_name": plan.get("计划名称", ""),
            "round_id": rnd.get("round_id"),
            "round_seq": rnd.get("round_seq"),
            "completed_at": now,
            "ok": ok,
            "outcome": "成功" if ok else "失败",
            "event_order": event["order"],
            "pages": [
                {
                    "page_no": page.get("page_no"),
                    "signed": bool(page.get("signed_at")),
                    "stored_sample_id": page.get("stored_sample_id"),
                    "receipts": "%d/%d" % cs.receipt_progress(page),
                }
                for page in cs.pages_of(rnd)
            ],
            "sample_check": cs.sample_check(rnd),
        }
        rnd["status"] = "已完成"
        rnd["completed_at"] = now
        rnd["outcome"] = "成功" if ok else "失败"
        rnd["completion_receipt"] = receipt
        plan["status"] = "已完成" if ok else "执行失败"
        plan["上次状态"] = "成功" if ok else "失败"
        plan["abnormal"] = not ok
        plan["pending"] = False
        plan["下次执行"] = next_run
        plan["last_completed_round_id"] = rnd.get("round_id")
        self._sync_task_status(plan, "运行中" if ok else "采集失败")
        return {
            "ok": True,
            "message": f"轮次 {rnd.get('round_id')} 执行结果已回写",
            "round_id": rnd.get("round_id"),
            "next_run": next_run,
            "completion_receipt": receipt,
        }

    # ---- 回查 ----

    def locate_pages(self, entry_id: int, page_no: str | None = None) -> dict[str, Any]:
        """按页面回查归属轮次：页面只属于签收时的那个轮次，不随暂停/续跑漂移。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        wanted = str(page_no or "").strip().upper()
        located: list[dict[str, Any]] = []
        for rnd in plan.get("rounds") or []:
            for page in cs.pages_of(rnd):
                if wanted and str(page.get("page_no", "")).upper() != wanted:
                    continue
                eligibility = cs.page_eligibility(plan, rnd, page)
                located.append({
                    "page_no": page.get("page_no"),
                    "round_id": rnd.get("round_id"),
                    "round_seq": rnd.get("round_seq"),
                    "round_status": rnd.get("status"),
                    "round_opened_at": rnd.get("opened_at"),
                    "delivery_status": page.get("delivery_status"),
                    "signed_at": page.get("signed_at"),
                    "stored_sample_id": page.get("stored_sample_id"),
                    "receipts": "%d/%d" % cs.receipt_progress(page),
                    "eligibility": eligibility["state"],
                    "reasons": eligibility["reasons"],
                    "delivery_checks": [
                        {
                            "checked_at": check.get("checked_at"),
                            "outcome": next(
                                (r["outcome"] for r in check.get("results", [])
                                 if r.get("page_no") == page.get("page_no")),
                                None,
                            ),
                        }
                        for check in rnd.get("delivery_checks") or []
                    ],
                })
        return {
            "ok": True,
            "message": (
                f"页面 {wanted} 归属轮次：" + "、".join(item["round_id"] for item in located)
                if located else f"计划 {entry_id} 中没有页面 {wanted or ''}"
            ),
            "plan_id": plan.get("id"),
            "page_no": wanted or None,
            "located": located,
            "rounds": [
                {
                    "round_id": rnd.get("round_id"),
                    "status": rnd.get("status"),
                    "paused_at": rnd.get("paused_at"),
                    "resumed_at": rnd.get("resumed_at"),
                    "completed_at": rnd.get("completed_at"),
                }
                for rnd in plan.get("rounds") or []
            ],
        }

    def list_rounds(self, entry_id: int) -> dict[str, Any]:
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        rounds = [self._round_view(plan, rnd) for rnd in plan.get("rounds") or []]
        return {
            "ok": True,
            "message": "轮次已按开启顺序返回",
            "plan": {
                "id": plan.get("id"),
                "计划名称": plan.get("计划名称"),
                "status": plan.get("status"),
                "paused": bool(plan.get("paused")),
                "paused_round_id": plan.get("paused_round_id"),
                "current_round_id": plan.get("current_round_id"),
                "last_completed_round_id": plan.get("last_completed_round_id"),
            },
            "rounds": rounds,
            "total": len(rounds),
        }

    def round_detail(self, entry_id: int, round_id: str) -> dict[str, Any]:
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        results = cs.round_eligibility(plan, rnd)
        return {
            "ok": True,
            "message": f"轮次 {rnd.get('round_id')} 详情",
            "round": self._round_view(plan, rnd),
            "eligibility": results,
            "summary": cs.summarize_states(results),
            "page_operations": cs.ordered_page_operations(plan, rnd),
            "sample_check": cs.sample_check(rnd),
            "pause_receipt": rnd.get("pause_receipt"),
            "completion_receipt": self._fresh_completion_receipt(rnd),
            "delivery_checks": list(rnd.get("delivery_checks") or []),
        }

    def completion_receipt(self, entry_id: int, round_id: str | None = None) -> dict[str, Any]:
        """按 round_id 回查完成回执，旧轮与恢复后的新轮严格分开。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        if round_id:
            rnd = cs.get_round(plan, round_id)
            if rnd is None:
                return {"ok": False, "message": f"轮次 {round_id} 不存在，无法回查完成回执"}
            receipt = self._fresh_completion_receipt(rnd)
            if receipt is None:
                return {
                    "ok": False,
                    "round_id": round_id,
                    "message": f"轮次 {round_id} 尚未回写完成回执",
                }
            return {"ok": True, "message": f"轮次 {round_id} 的完成回执", "completion_receipt": receipt}
        rounds = [
            rnd for rnd in plan.get("rounds") or [] if rnd.get("completion_receipt")
        ]
        if not rounds:
            return {"ok": False, "message": "该计划还没有完成回执"}
        latest = rounds[-1]
        return {
            "ok": True,
            "message": f"该计划最近一份完成回执来自轮次 {latest.get('round_id')}",
            "completion_receipt": self._fresh_completion_receipt(latest),
            "available_rounds": [r.get("round_id") for r in rounds],
        }

    def pause_receipt(self, entry_id: int, round_id: str | None = None) -> dict[str, Any]:
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        receipt = rnd.get("pause_receipt")
        if receipt is None:
            return {"ok": False, "message": f"轮次 {rnd.get('round_id')} 没有暂停回执"}
        return {"ok": True, "message": f"轮次 {rnd.get('round_id')} 的暂停回执", "pause_receipt": receipt}

    # ---- 页面签收 / 回执 ----

    def signoff_detail(
        self,
        entry_id: int,
        round_id: str,
        page_no: str,
    ) -> dict[str, Any]:
        """页面签收详情：签收、回执、入库样本、交付资格和可做操作。"""
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        page = cs.page_by_no(rnd, page_no)
        if page is None:
            return {"ok": False, "message": f"轮次 {round_id} 中没有页面 {page_no}"}
        eligibility = cs.page_eligibility(plan, rnd, page)
        operations = cs.ordered_page_operations(plan, rnd)
        operation = next((o for o in operations if o["page_no"] == page.get("page_no")), None)
        received, expected = cs.receipt_progress(page)
        return {
            "ok": True,
            "message": f"页面 {page.get('page_no')} 签收详情",
            "plan_id": plan.get("id"),
            "plan_name": plan.get("计划名称"),
            "round_id": rnd.get("round_id"),
            "round_status": rnd.get("status"),
            "paused": cs.plan_is_paused(plan, rnd),
            "page": {
                "page_no": page.get("page_no"),
                "delivery_status": page.get("delivery_status"),
                "transfer_started_at": page.get("transfer_started_at"),
                "signed_at": page.get("signed_at"),
                "signed_by": page.get("signed_by"),
                "stored_sample_id": page.get("stored_sample_id"),
                "stored_at": page.get("stored_at"),
                "expected_receipts": expected,
                "received_receipts": received,
                "receipts": list(page.get("receipts") or []),
                "inflight": cs.page_is_inflight(page),
            },
            "eligibility": eligibility,
            "available_actions": operation["available_actions"] if operation else [],
            "sequence": operation["sequence"] if operation else None,
            "order_basis": operation["order_basis"] if operation else "",
            "pause_landing": rnd.get("pause_landing"),
            "refresh_hint": (
                f"第一份回执已到，仍有 {max(expected - received, 0)} 份回执未到"
                if 0 < received < expected else ""
            ),
        }

    def sign_page(
        self,
        entry_id: int,
        round_id: str,
        page_no: str,
        *,
        receipt_no: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """页面签收并入库；暂停时只登记签收，不迁移轮次。"""
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        page = cs.page_by_no(rnd, page_no)
        if page is None:
            return {"ok": False, "message": f"轮次 {round_id} 中没有页面 {page_no}"}
        received, expected = cs.receipt_progress(page)
        if page.get("signed_at") and cs.page_is_stored(page):
            return {
                "ok": True,
                "message": f"页面 {page.get('page_no')} 已签收并入原轮次 {round_id}",
                "page_no": page.get("page_no"),
                "round_id": round_id,
                "stored_sample_id": page.get("stored_sample_id"),
                "eligibility": cs.page_eligibility(plan, rnd, page),
            }
        now = cs.now_iso()
        next_no = receipt_no or (received + 1)
        order = cs.next_plan_order(plan)
        page.setdefault("receipts", []).append({
            "no": next_no,
            "at": now,
            "kind": "签收",
            "order": order,
            "note": note,
        })
        page["signed_at"] = now
        page["signed_by"] = "值班员"
        page["delivery_status"] = "已签收"
        cs.record_round_event(
            plan, rnd, "页面签收", page_no=page.get("page_no"),
            note=f"第 {next_no} 份回执签收", at=now, order=order,
        )
        sample = cs.store_page_sample(plan, rnd, page, at=now)
        received, expected = cs.receipt_progress(page)
        return {
            "ok": True,
            "message": (
                f"页面 {page.get('page_no')} 已签收并入库样本 {sample.get('id')}，"
                f"仍归属轮次 {round_id}"
            ),
            "page_no": page.get("page_no"),
            "round_id": round_id,
            "stored_sample_id": sample.get("id"),
            "receipts": f"{received}/{expected}",
            "eligibility": cs.page_eligibility(plan, rnd, page),
        }

    def record_receipt(
        self,
        entry_id: int,
        round_id: str,
        page_no: str,
        *,
        note: str = "",
        sign: bool = False,
    ) -> dict[str, Any]:
        """登记一份页面回执；签收回执会触发入库，其余回执只更新等待进度。"""
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        page = cs.page_by_no(rnd, page_no)
        if page is None:
            return {"ok": False, "message": f"轮次 {round_id} 中没有页面 {page_no}"}
        if sign:
            return self.sign_page(entry_id, round_id, page_no, note=note)
        now = cs.now_iso()
        if not page.get("transfer_started_at"):
            page["transfer_started_at"] = now
        order = cs.next_plan_order(plan)
        page.setdefault("receipts", []).append({
            "no": len(page.get("receipts") or []) + 1,
            "at": now,
            "kind": "传输",
            "order": order,
            "note": note,
        })
        page["delivery_status"] = "待回执"
        cs.record_round_event(
            plan, rnd, "回执到达", page_no=page.get("page_no"),
            note=note or "传输回执", at=now, order=order,
        )
        received, expected = cs.receipt_progress(page)
        return {
            "ok": True,
            "message": f"页面 {page.get('page_no')} 回执 {received}/{expected}",
            "page_no": page.get("page_no"),
            "round_id": round_id,
            "receipts": f"{received}/{expected}",
            "awaiting": max(expected - received, 0),
            "eligibility": cs.page_eligibility(plan, rnd, page),
        }

    def verify_deliveries(self, entry_id: int, round_id: str | None = None) -> dict[str, Any]:
        """用采集状态数据逐页校验交付资格，并把结果回传给调度。"""
        plan, rnd, error = self._round_lookup(entry_id, round_id)
        if error:
            return {"ok": False, "message": error}
        results = cs.round_eligibility(plan, rnd)
        summary = cs.summarize_states(results)
        checks = {
            "checked_at": cs.now_iso(),
            "round_id": rnd.get("round_id"),
            "summary": summary,
            "pages": results,
            "source": "crawl_state",
        }
        rnd.setdefault("delivery_checks", []).append(checks)
        cs.record_round_event(
            plan, rnd, "交付校验",
            note=f"收录 {summary[cs.ELIGIBLE]} / 暂挂 {summary[cs.HELD]} / 需要恢复 {summary[cs.NEEDS_RESUME]}",
        )
        return {"ok": True, "message": "交付资格已校验并回传调度", **checks}

    # ---- 兼容动作入口 ----

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "立即执行":
            result = self.open_round(entry_id)
        elif action == "暂停计划":
            result = self.pause_plan(entry_id)
        elif action == "继续计划":
            result = self.resume_plan(entry_id)
        elif action == "完成轮次":
            result = self.complete_round(entry_id, True)
        elif action == "失败回写":
            result = self.complete_round(entry_id, False)
        else:
            return super().run_action(entry_id, action)
        # 业务拒绝时不能回 ok，否则页面会把「已暂停，拒绝新建轮次」当成成功。
        return (plan if result.get("ok") else None), str(result.get("message", ""))

    # ---- 兼容原有扩展方法 ----

    def report_execution(self, entry_id: int) -> dict[str, Any]:
        """把计划执行量上报采集量指标。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None:
            return {"ok": False, "message": "计划不存在"}
        task = self._linked_task(plan)
        site = self._task_site(task, plan)
        rnd = cs.get_round(plan)
        stored = sum(1 for page in cs.pages_of(rnd) if cs.page_is_stored(page))
        metrics = store.rows("collect_metrics")
        row = next((m for m in metrics if m.get("站点名称") == site), None)
        if row is None:
            mid = max((int(m.get("id", 0)) for m in metrics), default=0) + 1
            metrics.append({
                "id": mid,
                "统计时间": cs.now_iso(),
                "站点名称": site,
                "抓取量": str(max(stored, 1)),
                "解析量": "0",
                "产出量": "0",
                "status": "正常",
                "pending": True,
                "abnormal": False,
            })
        else:
            row["抓取量"] = str(int(row.get("抓取量", "0") or 0) + max(stored, 1))
        return {"ok": True, "message": "计划执行量已上报", "site": site, "stored_pages": stored}

    def reschedule_failed(self, entry_id: int) -> dict[str, Any]:
        """失败计划重新排队并隔离失败上下文。"""
        plan = store.find(self.MODULE, entry_id)
        if plan is None or plan.get("status") != "执行失败":
            return {"ok": False, "message": "计划不存在或未失败"}
        rows = store.rows("failure_quarantine")
        fid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": fid,
            "样本指纹": f"schedule-{entry_id}",
            "失败阶段": "调度",
            "失败原因": f"计划 {plan.get('计划名称', '')} 执行失败",
            "隔离时间": cs.now_iso(),
            "重试次数": "1",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        plan["status"] = "待执行"
        plan["abnormal"] = False
        plan["pending"] = True
        return {"ok": True, "message": "失败计划已重排", "quarantine_id": fid}

    def due_plans(self, now: str | None = None) -> dict[str, Any]:
        """列出到达下次执行时间的待执行计划；暂停中的计划不参与调度。"""
        current = now or cs.now_iso()
        due = [
            p for p in store.rows(self.MODULE)
            if p.get("status") == "待执行"
            and not p.get("paused")
            and str(p.get("下次执行", "") or "9999") <= current
        ]
        paused = [p for p in store.rows(self.MODULE) if p.get("paused")]
        return {
            "ok": True,
            "message": "到期计划已筛选，暂停计划不参与调度",
            "due_count": len(due),
            "plans": [p.get("计划名称", "") for p in due],
            "paused_count": len(paused),
            "paused_plans": [p.get("计划名称", "") for p in paused],
        }
