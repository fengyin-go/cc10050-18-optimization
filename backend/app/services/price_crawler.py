"""价格爬取任务领域服务：按关键词与抓取深度调度商品价格页面的抓取，负责任务启停与重试。

数据链路位置：采集执行层，把站点目录、频控规则和代理线路组合成可运行的爬取任务，
产出的页面样本写入样本库供解析层消费。

与调度计划共用 `crawl_state` 的轮次口径：每笔页面交付都先用采集状态数据校验
是否仍具资格，再回传 收录 / 暂挂 / 需要恢复 给调度。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services import crawl_state as cs
from app.services.base import BaseModuleService
from app.store import store


class PriceCrawlerService(BaseModuleService):
    MODULE = "price_crawler"
    ENTITY = "爬取任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务名称", "目标站点", "商品关键词", "抓取深度", "采集间隔"]
    REQUIRED_FIELDS = ["任务名称", "目标站点", "商品关键词"]
    STATUS_ORDER = ["草稿", "运行中", "已暂停", "采集失败"]
    ACTION_RULES = {"启动任务": "运行中", "暂停任务": "已暂停", "重试采集": "采集失败"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：爬取调度与样本产出 ----

    def _plan_for(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        plans = cs.plans_for_task(str(entry.get("任务名称", "")))
        return plans[0] if plans else None

    def _resolve_page(
        self,
        entry: dict[str, Any],
        round_id: str | None,
        page_no: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
        plan = self._plan_for(entry)
        if plan is None:
            return None, None, f"任务 {entry.get('任务名称', '')} 未关联调度计划，无法按轮次交付"
        rnd = cs.get_round(plan, round_id) if round_id else cs.get_round(plan)
        if rnd is None:
            return plan, None, f"任务 {entry.get('任务名称', '')} 没有可用的执行轮次"
        if page_no is None:
            return plan, rnd, None
        page = cs.page_by_no(rnd, page_no)
        if page is None:
            return plan, rnd, f"轮次 {rnd.get('round_id')} 中没有页面 {page_no}"
        return plan, rnd, None

    def start_task(self, entry_id: int) -> dict[str, Any]:
        """启动爬取任务，校验站点有效性与频控状态后进入运行中。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        domain = entry.get("目标站点", "")
        site = next(
            (s for s in store.rows("shop_site") if s.get("站点域名") == domain), None
        )
        if site is None or site.get("status") != "已验证":
            return {"ok": False, "message": f"目标站点 {domain} 未验证或已封禁"}
        limit = next(
            (r for r in store.rows("rate_limit") if r.get("目标站点") == domain
             and r.get("status") == "已生效"), None
        )
        if limit is None:
            return {"ok": False, "message": "目标站点缺少生效频控规则，拒绝启动"}
        plan = self._plan_for(entry)
        if plan is not None and plan.get("paused"):
            return {
                "ok": False,
                "message": f"计划已暂停（轮次 {plan.get('paused_round_id')}），不能单独启动任务",
            }
        entry["status"] = "运行中"
        entry["pending"] = True
        return {
            "ok": True,
            "message": "任务已启动",
            "concurrency": limit.get("并发上限", "3"),
            "interval": entry.get("采集间隔", "60"),
        }

    def pause_task(self, entry_id: int, reason: str) -> dict[str, Any]:
        """暂停运行中的任务并记录原因。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "运行中":
            return {"ok": False, "message": "任务不存在或不在运行中"}
        entry["status"] = "已暂停"
        entry["pending"] = True
        return {"ok": True, "message": f"任务已暂停: {reason}"}

    def retry_failed(self, entry_id: int) -> dict[str, Any]:
        """重试采集失败的任务，隔离失败样本后重新进入运行。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "采集失败":
            return {"ok": False, "message": "任务不存在或非失败状态"}
        qrows = store.rows("failure_quarantine")
        qid = max((int(r.get("id", 0)) for r in qrows), default=0) + 1
        qrows.append({
            "id": qid,
            "样本指纹": f"task-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "失败阶段": "采集",
            "失败原因": f"任务 {entry_id} 重试前快照",
            "隔离时间": datetime.now(timezone.utc).isoformat(),
            "重试次数": "1",
            "status": "待处理",
            "pending": True,
            "abnormal": False,
        })
        entry["status"] = "运行中"
        entry["pending"] = True
        return {"ok": True, "message": "失败样本已隔离，任务重新运行", "quarantine_id": qid}

    def produce_samples(self, entry_id: int, pages: list[str]) -> dict[str, Any]:
        """把抓取到的页面写入样本库，形成任务→样本的数据链路。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "运行中":
            return {"ok": False, "message": "任务不在运行中，不能产出样本"}
        plan = self._plan_for(entry)
        if plan is not None and plan.get("paused"):
            return {"ok": False, "message": "计划已暂停，不能产出新样本"}
        rows = store.rows("sample_store")
        base_id = max((int(r.get("id", 0)) for r in rows), default=0)
        batch = f"task-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}"
        rnd = cs.get_round(plan) if plan else None
        round_id = rnd.get("round_id") if rnd else ""
        for offset, page in enumerate(pages, start=1):
            rows.append({
                "id": base_id + offset,
                "样本批次": batch,
                "站点名称": entry.get("目标站点", ""),
                "原始HTML摘要": page[:80],
                "抓取时间": datetime.now(timezone.utc).isoformat(),
                "样本指纹": f"fp-{batch}-{offset}",
                "status": "已保存",
                "pending": True,
                "abnormal": False,
                "round_id": round_id,
                "plan_id": plan.get("id") if plan else None,
            })
        return {"ok": True, "message": "样本已写入样本库", "batch": batch, "count": len(pages)}

    def deliver_page(
        self,
        entry_id: int,
        page_no: str | None = None,
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """用采集状态数据校验每笔页面交付，并回传结果给调度。

        round_id 缺省时取任务关联计划的当前轮次；未指定 page_no 时逐页校验。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        plan, rnd, error = self._resolve_page(entry, round_id, page_no)
        if error:
            return {"ok": False, "message": error}
        assert plan is not None and rnd is not None
        pages = [cs.page_by_no(rnd, page_no)] if page_no else cs.pages_of(rnd)
        results: list[dict[str, Any]] = []
        for page in pages:
            eligibility = cs.page_eligibility(plan, rnd, page)
            outcome = eligibility["state"]
            delivered = outcome == cs.ELIGIBLE and cs.page_is_stored(page)
            if outcome == cs.ELIGIBLE:
                handoff = "已交付下游" if delivered else "可交付，等待交接"
            elif outcome == cs.HELD:
                handoff = "暂挂，等待回执齐全后交接"
            else:
                handoff = "需要续跑旧轮后才能交付"
            results.append({
                **eligibility,
                "task": entry.get("任务名称", ""),
                "outcome": outcome,
                "delivered": delivered,
                "handoff": handoff,
            })
        summary = cs.summarize_states(results)
        check = {
            "checked_at": cs.now_iso(),
            "task": entry.get("任务名称", ""),
            "plan_id": plan.get("id"),
            "round_id": rnd.get("round_id"),
            "source": "crawl_state",
            "results": results,
            "summary": summary,
        }
        rnd.setdefault("delivery_checks", []).append(check)
        cs.record_round_event(
            plan, rnd, "任务交付校验",
            page_no=page_no,
            note=f"收录 {summary[cs.ELIGIBLE]} / 暂挂 {summary[cs.HELD]} / 需要恢复 {summary[cs.NEEDS_RESUME]}",
        )
        return {
            "ok": True,
            "message": "页面交付资格已校验并回传调度",
            **check,
            # 旧轮可做的操作按实际接收顺序返回，供任务详情直接展示。
            "page_operations": cs.ordered_page_operations(plan, rnd),
        }

    def round_operations(
        self,
        entry_id: int,
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """回到原申请时，按实际接收顺序给出该轮每页可做的操作。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        plan, rnd, error = self._resolve_page(entry, round_id, None)
        if error:
            return {"ok": False, "message": error}
        assert plan is not None and rnd is not None
        operations = cs.ordered_page_operations(plan, rnd)
        return {
            "ok": True,
            "message": f"轮次 {rnd.get('round_id')} 的操作已按实际接收顺序返回",
            "task": entry.get("任务名称", ""),
            "round_id": rnd.get("round_id"),
            "round_status": rnd.get("status"),
            "operations": operations,
            "summary": cs.summarize_states(cs.round_eligibility(plan, rnd)),
        }

    def page_eligibility(
        self,
        entry_id: int,
        page_no: str,
        round_id: str | None = None,
    ) -> dict[str, Any]:
        """单页交付资格查询，供调度与前端随时核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        plan, rnd, error = self._resolve_page(entry, round_id, page_no)
        if error:
            return {"ok": False, "message": error}
        assert plan is not None and rnd is not None
        page = cs.page_by_no(rnd, page_no)
        assert page is not None
        eligibility = cs.page_eligibility(plan, rnd, page)
        return {
            "ok": True,
            "message": f"页面 {page_no} 当前交付资格：{eligibility['state']}",
            "task": entry.get("任务名称", ""),
            "round_id": rnd.get("round_id"),
            **eligibility,
        }

    def bind_proxy_route(self, entry_id: int) -> dict[str, Any]:
        """为任务选择可用代理线路并记录绑定。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "任务不存在"}
        region = ""
        site = next(
            (s for s in store.rows("shop_site")
             if s.get("站点域名") == entry.get("目标站点", "")), None
        )
        if site:
            region = str(site.get("采集区域", ""))
        candidates = [
            r for r in store.rows("proxy_route")
            if r.get("status") == "可用" and (not region or region in str(r.get("线路区域", "")))
        ]
        if not candidates:
            return {"ok": False, "message": "没有可用代理线路"}
        best = max(candidates, key=lambda r: float(r.get("可用性评分", "0") or 0))
        return {
            "ok": True,
            "message": "代理线路已绑定",
            "route_id": best.get("id"),
            "region": best.get("线路区域", ""),
        }

    def report_metrics(self, entry_id: int) -> dict[str, Any]:
        """向采集量指标上报本任务的抓取量。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "任务不存在"}
        domain = entry.get("目标站点", "")
        rows = store.rows("collect_metrics")
        row = next((r for r in rows if r.get("站点名称") == domain), None)
        now = datetime.now(timezone.utc).isoformat()
        if row is None:
            mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": mid,
                "统计时间": now,
                "站点名称": domain,
                "抓取量": "1",
                "解析量": "0",
                "产出量": "0",
                "status": "正常",
                "pending": True,
                "abnormal": False,
            })
        else:
            row["抓取量"] = str(int(row.get("抓取量", "0") or 0) + 1)
            row["统计时间"] = now
        return {"ok": True, "message": "抓取量已上报", "site": domain}
