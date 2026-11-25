import unittest

from packaging_tool.inventory import cancellation_confirmed, plan_actions, plan_lines
from packaging_tool.model import (
    ActionKind,
    Assessment,
    Carton,
    LineState,
    PrintLine,
)


def carton(carton_id, line="P-1", qty=5):
    return Carton(carton_id, "G20", "H7", 410, "EU", "fr", qty, line, "L120")


def assessment(carton_id, state, edition, line="P-1", qty=5):
    return Assessment(
        carton=carton(carton_id, line, qty), state=state, edition=edition,
        missing=(), evidence=("http://x/signed",), reasons=())


class LinePlanningTests(unittest.TestCase):
    def test_fixture_scene_one_classifies_printed_stickers_by_carton(self):
        # P-4A：C-401(7) 仍用 FR-7，C-402(11) 换 FR-8；已印 8、待印 10
        assessments = [
            assessment("C-401", LineState.READY, "FR-7", "P-4A", 7),
            assessment("C-402", LineState.AWAITING_EDITION, "FR-8", "P-4A", 11),
        ]
        line = PrintLine("P-4A", "FR-7", printed=8, queued=10)
        plan = plan_lines(assessments, [line])[0]
        self.assertEqual(plan.usable, 7)           # 7 张可继续贴低批号箱
        self.assertEqual(plan.pending_supervisor, 1)  # 多出的 1 张等主管确认
        self.assertEqual(plan.quarantine, 0)
        self.assertEqual(plan.replacement_qty, 11)
        self.assertEqual(plan.cancellable_queued, 10)
        self.assertEqual(plan.state, LineState.AWAITING_EDITION)
        self.assertIn("C-402", plan.missing_for)

    def test_reserved_old_stickers_are_quarantined(self):
        assessments = [
            assessment("C-401", LineState.READY, "FR-7", "P-4A", 7),
            assessment("C-402", LineState.AWAITING_EDITION, "FR-8", "P-4A", 11),
        ]
        line = PrintLine("P-4A", "FR-7", printed=8, queued=10)
        plan = plan_lines(assessments, [line], reserved={"P-4A": 3})[0]
        self.assertEqual(plan.quarantine, 3)       # 已实物配到换版箱的旧贴隔离
        self.assertEqual(plan.usable, 5)           # 剩余 5 张可领
        # 待印 10 张足以补齐 C-401 的 7 张缺口，因此不算缺贴箱
        self.assertNotIn("C-401", plan.missing_for)
        self.assertIn("C-402", plan.missing_for)

    def test_ready_carton_short_even_with_queued_is_listed_missing(self):
        # 已印+待印都补不齐时才登记缺贴箱
        assessments = [
            assessment("C-1", LineState.READY, "FR-7", "P-1", qty=12),
        ]
        line = PrintLine("P-1", "FR-7", printed=4, queued=4)
        plan = plan_lines(assessments, [line])[0]
        self.assertEqual(plan.usable, 4)
        self.assertIn("C-1", plan.missing_for)

    def test_shared_line_keeps_valid_print_run_for_other_orders(self):
        # 同一印线另一款箱货仍用旧版：换版不能丢掉其有效印量
        assessments = [
            assessment("C-1", LineState.READY, "FR-7", "P-9", qty=6),
            assessment("C-2", LineState.READY, "FR-7", "P-9", qty=4),
        ]
        line = PrintLine("P-9", "FR-7", printed=6, queued=5)
        plan = plan_lines(assessments, [line])[0]
        self.assertEqual(plan.usable, 6)
        self.assertEqual(plan.cancellable_queued, 1)  # 只需取消多余未印量
        self.assertEqual(plan.state, LineState.READY)

    def test_suspended_line_holds_queued_quantity(self):
        assessments = [
            assessment("C-404", LineState.SUSPENDED, None, "P-4C", qty=5),
        ]
        line = PrintLine("P-4C", "FR-7", printed=0, queued=5)
        plan = plan_lines(assessments, [line])[0]
        self.assertEqual(plan.cancellable_queued, 0)  # 暂停行不代客户取消
        self.assertEqual(plan.held_demand, 5)
        self.assertEqual(plan.state, LineState.SUSPENDED)

    def test_ready_shortfall_uses_queued_before_new_print(self):
        assessments = [assessment("C-1", LineState.READY, "FR-7", "P-1", qty=10)]
        line = PrintLine("P-1", "FR-7", printed=4, queued=4)
        plan = plan_lines(assessments, [line])[0]
        self.assertEqual(plan.usable, 4)
        # 已印+待印=8，还差 2 张；待印量全部保留不可取消
        self.assertEqual(plan.cancellable_queued, 0)


class ActionPlanningTests(unittest.TestCase):
    def _plans(self):
        assessments = [
            assessment("C-401", LineState.READY, "FR-7", "P-4A", 7),
            assessment("C-402", LineState.AWAITING_EDITION, "FR-8", "P-4A", 11),
        ]
        return plan_lines(assessments,
                          [PrintLine("P-4A", "FR-7", printed=8, queued=10)])

    def test_replace_waits_for_actual_cancel_receipt(self):
        plans = self._plans()
        # 没有回执：只生成取消，不允许下替换单
        actions = plan_actions(plans, receipts={})
        self.assertEqual([a.kind for a in actions], [ActionKind.CANCEL])
        cancel = actions[0]
        self.assertEqual(cancel.request_id, "WO-CANCEL-P-4A")

        # 回执被拒收：仍不能替换
        rejected = {"receipt": "PRN-00001", "rows": [
            {"item": "P-4A", "status": "rejected"}]}
        self.assertFalse(cancellation_confirmed(plans[0], rejected))
        actions = plan_actions(plans, {"CANCEL-P-4A": rejected})
        self.assertEqual([a.kind for a in actions], [])

        # 真实取消且队列待印归零：替换单才放行
        accepted = {"receipt": "PRN-00002", "rows": [
            {"item": "P-4A", "status": "accepted"}]}
        updated = plan_lines(
            [assessment("C-401", LineState.READY, "FR-7", "P-4A", 7),
             assessment("C-402", LineState.AWAITING_EDITION, "FR-8", "P-4A", 11)],
            [PrintLine("P-4A", "FR-7", printed=9, queued=0, cancelled=9)])
        self.assertTrue(cancellation_confirmed(updated[0], accepted))
        actions = plan_actions(updated, {"CANCEL-P-4A": accepted})
        self.assertEqual([a.kind for a in actions], [ActionKind.REPLACE])
        replace = actions[0]
        self.assertEqual(replace.details["P-4A"]["qty"], 11)
        self.assertEqual(replace.details["P-4A"]["new_edition"], "FR-8")
        self.assertEqual(replace.details["P-4A"]["based_on_cancel"], "PRN-00002")

    def test_stable_request_ids_are_idempotent(self):
        plans = self._plans()
        first = plan_actions(plans, {})
        second = plan_actions(plans, {})
        self.assertEqual([a.request_id for a in first],
                         [a.request_id for a in second])
        # 已有回执的动作不再生成
        receipts = {"CANCEL-P-4A": {"receipt": "PRN-1", "rows": [
            {"item": "P-4A", "status": "accepted"}]}}
        self.assertEqual(plan_actions(plans, receipts), [])


if __name__ == "__main__":
    unittest.main()
