"""采集调度轮次回归：暂停落点、页面归属、交付资格与按轮次回查。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.services.crawl_schedule import CrawlScheduleService
from app.services.price_crawler import PriceCrawlerService
from app.seed import SEED_ROWS
from app.store import store


class CrawlRoundTest(unittest.TestCase):
    PLAN_ID = 1
    ROUND = "1-R1"

    def setUp(self) -> None:
        # 服务持有的是 store 单例，重置时直接换掉底层表。
        store._tables = {
            name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self.schedule = CrawlScheduleService()
        self.crawler = PriceCrawlerService()

    # ---- 暂停与在途页冲突 ----

    def test_pause_keeps_signed_page_and_reports_inflight_conflict(self) -> None:
        result = self.schedule.pause_plan(self.PLAN_ID, "值班员暂停北站 S6")
        self.assertTrue(result["ok"])
        self.assertTrue(result["needs_confirmation"])
        self.assertEqual([c["page_no"] for c in result["conflicts"]], ["B"])
        receipt = result["pause_receipt"]
        self.assertEqual([p["page_no"] for p in receipt["signed_pages"]], ["A"])
        self.assertEqual(receipt["signed_pages"][0]["kept_in_round"], self.ROUND)
        self.assertTrue(receipt["next_round_blocked"])

    def test_pause_does_not_lose_stored_samples(self) -> None:
        before = self.schedule.round_detail(self.PLAN_ID, self.ROUND)["sample_check"]
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        paused = self.schedule.round_detail(self.PLAN_ID, self.ROUND)["sample_check"]
        self.assertTrue(before["intact"])
        self.assertTrue(paused["intact"])
        self.assertEqual(paused["sample_count"], before["sample_count"])
        self.assertEqual(paused["stored_pages"], before["stored_pages"])

    def test_signed_receipts_are_archived_on_original_round(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.resume_plan(self.PLAN_ID)
        detail = self.schedule.round_detail(self.PLAN_ID, self.ROUND)
        page_a = next(op for op in detail["page_operations"] if op["page_no"] == "A")
        self.assertEqual(page_a["eligibility"], "收录")
        self.assertEqual(page_a["stored_sample_id"], 4)

    # ---- 暂停后不得调度下一轮 ----

    def test_new_round_is_rejected_while_paused(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        rejected = self.schedule.open_round(self.PLAN_ID)
        self.assertFalse(rejected["ok"])
        self.assertTrue(rejected["paused"])
        self.assertEqual(rejected["paused_round_id"], self.ROUND)
        self.assertIn("已暂停", rejected["message"])

    def test_queue_round_request_reports_paused_plan(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        response = self.schedule.submit_round_request(self.PLAN_ID, "调度队列")
        self.assertFalse(response["ok"])
        self.assertFalse(response["accepted"])
        self.assertTrue(response["paused"])
        self.assertIn("已暂停", response["message"])
        self.assertEqual(response["paused_round_id"], self.ROUND)

    def test_due_plans_skip_paused_plan(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        due = self.schedule.due_plans(now="9999-01-01T00:00:00+00:00")
        self.assertNotIn("北站 S6 采集计划", due["plans"])
        self.assertIn("北站 S6 采集计划", due["paused_plans"])

    # ---- 在途页面续跑与交付资格 ----

    def test_inflight_page_needs_resume_then_held_until_receipts_complete(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.record_receipt(self.PLAN_ID, self.ROUND, "B", note="第一份回执")
        signoff = self.schedule.signoff_detail(self.PLAN_ID, self.ROUND, "B")
        self.assertEqual(signoff["eligibility"]["state"], "需要恢复")
        self.assertEqual(signoff["page"]["received_receipts"], 1)
        self.assertIn("仍有 1 份回执未到", signoff["refresh_hint"])

        resumed = self.schedule.resume_plan(self.PLAN_ID)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["summary"]["暂挂"], 1)
        self.assertEqual(self.crawler.page_eligibility(1, "B")["state"], "暂挂")

        delivered = self.crawler.deliver_page(1, "B")
        self.assertFalse(delivered["results"][0]["delivered"])
        self.assertEqual(delivered["results"][0]["outcome"], "暂挂")

    def test_resume_only_continues_paused_round(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        wrong = self.schedule.resume_plan(self.PLAN_ID, "1-R9")
        self.assertFalse(wrong["ok"])
        right = self.schedule.resume_plan(self.PLAN_ID)
        self.assertTrue(right["ok"])
        self.assertEqual(right["round_id"], self.ROUND)
        self.assertEqual([r["round_id"] for r in self.schedule.list_rounds(self.PLAN_ID)["rounds"]],
                         [self.ROUND])

    def test_crawler_blocks_delivery_of_new_round_while_paused(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        blocked = self.crawler.deliver_page(1)
        summary = blocked["summary"]
        self.assertEqual(summary["需要恢复"], 1)
        self.assertEqual(summary["收录"], 1)

    def test_signoff_requires_matching_round(self) -> None:
        missing = self.schedule.signoff_detail(self.PLAN_ID, "1-R9", "B")
        self.assertFalse(missing["ok"])

    # ---- 旧轮/新轮回执隔离 ----

    def test_completion_receipts_are_looked_up_by_round(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.resume_plan(self.PLAN_ID)
        self.schedule.sign_page(self.PLAN_ID, self.ROUND, "B")
        self.assertTrue(self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)["ok"])
        new_round = self.schedule.open_round(self.PLAN_ID)
        self.assertTrue(new_round["ok"])
        new_id = new_round["round"]["round_id"]
        self.assertNotEqual(new_id, self.ROUND)

        old_receipt = self.schedule.completion_receipt(self.PLAN_ID, self.ROUND)
        self.assertTrue(old_receipt["ok"])
        self.assertEqual(old_receipt["completion_receipt"]["round_id"], self.ROUND)
        self.assertEqual(
            {p["page_no"] for p in old_receipt["completion_receipt"]["pages"]}, {"A", "B"},
        )
        new_receipt = self.schedule.completion_receipt(self.PLAN_ID, new_id)
        self.assertFalse(new_receipt["ok"])
        self.assertIn("尚未回写", new_receipt["message"])

    def test_cannot_complete_round_with_unsigned_pages(self) -> None:
        blocked = self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["pending_pages"], ["B"])
        self.schedule.sign_page(self.PLAN_ID, self.ROUND, "B")
        self.assertTrue(self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)["ok"])

    def test_cannot_complete_paused_round(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        blocked = self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)
        self.assertFalse(blocked["ok"])
        self.schedule.resume_plan(self.PLAN_ID)
        self.schedule.sign_page(self.PLAN_ID, self.ROUND, "B")
        self.assertTrue(self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)["ok"])
        again = self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)
        self.assertFalse(again["ok"])
        self.assertEqual(again["completion_receipt"]["round_id"], self.ROUND)

    # ---- 页面归属回查与操作顺序 ----

    def test_page_location_tracks_round_assignment(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.resume_plan(self.PLAN_ID)
        self.schedule.sign_page(self.PLAN_ID, self.ROUND, "B")
        self.schedule.complete_round(self.PLAN_ID, True, self.ROUND)
        self.schedule.open_round(self.PLAN_ID)
        located = self.schedule.locate_pages(self.PLAN_ID, "A")
        self.assertTrue(located["ok"])
        self.assertEqual([item["round_id"] for item in located["located"]], [self.ROUND])
        self.assertEqual(located["located"][0]["eligibility"], "收录")

    def test_page_operations_follow_receipt_order(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.record_receipt(self.PLAN_ID, self.ROUND, "B", note="B 回执")
        self.schedule.resume_plan(self.PLAN_ID)
        operations = self.schedule.round_detail(self.PLAN_ID, self.ROUND)["page_operations"]
        self.assertEqual([op["page_no"] for op in operations], ["A", "B"])
        self.assertEqual([op["sequence"] for op in operations], [1, 2])
        page_a = operations[0]
        self.assertEqual(page_a["available_actions"], ["查看样本"])

    def test_task_operations_follow_receipt_order(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.record_receipt(self.PLAN_ID, self.ROUND, "B", note="B 第一份回执")
        operations = self.crawler.round_operations(1)["operations"]
        self.assertEqual([op["page_no"] for op in operations], ["A", "B"])
        self.assertEqual(operations[1]["eligibility"], "需要恢复")
        self.assertIn("继续计划", operations[1]["available_actions"])

    def test_generic_action_reports_rejection(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        entry, message = self.schedule.run_action(self.PLAN_ID, "立即执行")
        self.assertIsNone(entry)
        self.assertIn("已暂停", message)

    def test_receipt_events_use_single_sequence_number(self) -> None:
        self.schedule.pause_plan(self.PLAN_ID, "暂停")
        self.schedule.record_receipt(self.PLAN_ID, self.ROUND, "B")
        detail = self.schedule.round_detail(self.PLAN_ID, self.ROUND)
        orders = [event["order"] for event in detail["round"]["events"]]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(len(orders), len(set(orders)))
        round_detail = self.schedule.round_detail(self.PLAN_ID, self.ROUND)
        raw_pages = next(
            r for r in store.rows("crawl_schedule")[0]["rounds"]
            if r["round_id"] == self.ROUND
        )["pages"]
        receipt_orders = [r["order"] for r in raw_pages[1]["receipts"]]
        self.assertTrue(set(receipt_orders).issubset(set(orders)))
        self.assertEqual(round_detail["round"]["pages"][1]["receipts"], "1/2")

    def test_verify_deliveries_feeds_back_to_plan(self) -> None:
        result = self.schedule.verify_deliveries(self.PLAN_ID)
        self.assertTrue(result["ok"])
        self.assertEqual(result["round_id"], self.ROUND)
        self.assertEqual(result["summary"], {"收录": 1, "暂挂": 1, "需要恢复": 0})
        detail = self.schedule.round_detail(self.PLAN_ID, self.ROUND)
        self.assertTrue(detail["delivery_checks"])


if __name__ == "__main__":
    unittest.main()
