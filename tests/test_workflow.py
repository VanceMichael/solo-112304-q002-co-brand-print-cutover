import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from packaging_tool.cli import main
from packaging_tool.model import ActionKind, LineState
from packaging_tool.portal import PortalConflict

from tests.helpers import FixtureServer


class EndToEndWorkflowTests(unittest.TestCase):
    def test_full_handoff_across_three_scenes(self):
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as fixture:
            store_path = str(Path(tmp) / "receipts.jsonl")
            service = fixture.service(store_path)

            # 时间点 0：授权齐全的箱就绪，H8 箱暂停，门户暂无待办
            snapshot = service.take_snapshot()
            self.assertEqual(snapshot.scene_index, 0)
            order = service.evaluate(snapshot)
            states = {a.carton.carton_id: a.state for a in order.assessments}
            self.assertEqual(states["C-404"], LineState.SUSPENDED)
            self.assertEqual(states["C-401"], LineState.READY)
            self.assertEqual(order.actions, [])

            # 时间点 1：公告发布。C-402 换 FR-8，取消 P-4A 未印部分；
            # 门户先丢一次回应，工具靠请求号把 accepted 回执查回来。
            fixture.scene(1)
            snapshot = service.take_snapshot()
            order = service.evaluate(snapshot)
            self.assertEqual(
                {a.carton.carton_id: a.edition for a in order.assessments}["C-402"],
                "FR-8")
            plan_4a = next(p for p in order.line_plans if p.line.line_id == "P-4A")
            self.assertEqual(plan_4a.cancellable_queued, 10)
            service.execute_handoff(order)
            cancel = next(a for a in order.actions if a.line_id == "P-4A"
                          and a.kind == ActionKind.CANCEL)
            self.assertIsNotNone(cancel.receipt)
            self.assertEqual(
                {row["item"]: row["status"] for row in cancel.receipt["rows"]},
                {"P-4A": "accepted"})
            # 此时队列仍显示待印 10，替换单被真实取消门槛拦住
            self.assertFalse(any(a.kind == ActionKind.REPLACE for a in order.actions))

            # 时间点 2：印厂公布取消结果，替换单放行；已印 9 张按箱核对
            fixture.scene(2)
            snapshot = service.take_snapshot()
            order = service.evaluate(snapshot)
            plan_4a = next(p for p in order.line_plans if p.line.line_id == "P-4A")
            self.assertEqual(plan_4a.usable, 7)
            self.assertEqual(plan_4a.pending_supervisor, 2)
            self.assertEqual(plan_4a.replacement_qty, 11)
            service.execute_handoff(order)
            replace = next(a for a in order.actions if a.kind == ActionKind.REPLACE)
            self.assertEqual(replace.line_id, "P-4A")
            self.assertEqual(replace.details["P-4A"]["qty"], 11)
            self.assertEqual(replace.details["P-4A"]["new_edition"], "FR-8")
            self.assertEqual(replace.receipt["rows"][0]["status"], "accepted")

            # 暂停的 P-4C 始终没有被取消
            self.assertFalse(any(a.line_id == "P-4C" and a.kind == ActionKind.CANCEL
                                 for a in order.actions))

            report = service.report(order)
            self.assertEqual(report["scene_index"], 2)
            evidence_ids = {item["id"] for item in report["evidence"]}
            self.assertIn("FR-7", evidence_ids)
            self.assertIn("FR-8", evidence_ids)
            self.assertIn("N-FR-8", evidence_ids)
            text = service.render_text(order)
            self.assertIn("C-402", text)
            self.assertIn("可领 7", text)

            # 重跑：全部动作命中本地存根，不产生新门户请求
            before = len(service.portal.store.all())
            service.execute_handoff(service.evaluate(service.take_snapshot()))
            self.assertEqual(len(service.portal.store.all()), before)

    def test_rejected_cancel_blocks_replacement(self):
        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as fixture:
            service = fixture.service(str(Path(tmp) / "receipts.jsonl"))
            fixture.scene(1)
            order = service.evaluate(service.take_snapshot())
            # P-4B 的取消在 fixture 里被拒收
            plan_4b = next(p for p in order.line_plans if p.line.line_id == "P-4B")
            # 场景中 P-4B 无需取消（内销线未换版），改为直接验证拒收回执门槛
            from packaging_tool.inventory import cancellation_confirmed
            rejected = {"receipt": "PRN-X", "rows": [
                {"item": "P-4B", "status": "rejected"}]}
            self.assertFalse(cancellation_confirmed(plan_4b, rejected))

    def test_conflicting_request_number_raises_and_keeps_running(self):
        import json as _json
        from packaging_tool.transport import Response
        from tests.helpers import FakeTransport

        with tempfile.TemporaryDirectory() as tmp, FixtureServer() as fixture:
            service = fixture.service(str(Path(tmp) / "r.jsonl"))
            fixture.scene(1)
            order = service.evaluate(service.take_snapshot())
            # 门户对该请求号返回 409（同号不同内容），工具不得覆盖本地存根
            conflict = Response(
                fixture.origin + "/printer/portal", 409, "application/json",
                _json.dumps({"message": "业务请求号已有不同内容"}))
            service.portal.transport = FakeTransport(
                routes={}, posts={("WO-CANCEL-P-4A", "cancel"): conflict})
            with self.assertRaises(PortalConflict):
                service.execute_handoff(order)
            self.assertIsNone(service.portal.store.get("CANCEL-P-4A"))

    def test_cli_audit_outputs_json_report(self):
        with FixtureServer() as fixture:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main(["--base", fixture.origin, "--format", "json", "audit"])
            self.assertEqual(code, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual({row["carton"] for row in report["cartons"]},
                             {"C-401", "C-402", "C-403", "C-404"})
            self.assertIn("evidence", report)


if __name__ == "__main__":
    unittest.main()
