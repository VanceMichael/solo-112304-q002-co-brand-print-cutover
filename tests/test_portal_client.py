import json
import tempfile
import unittest
from pathlib import Path

from packaging_tool.model import Action, ActionKind
from packaging_tool.portal import (
    PortalClient,
    PortalConflict,
    PortalUnavailable,
    ReceiptStore,
)
from packaging_tool.transport import Response

from tests.helpers import FakeTransport


def make_action(request_id="WO-CANCEL-P-4A", line_id="P-4A"):
    return Action(
        kind=ActionKind.CANCEL, line_id=line_id, request_id=request_id,
        items=(line_id,),
        details={line_id: {"edition": "FR-7", "cancel_queued": 10}})


def json_response(status, body):
    return Response("http://x/printer/portal", status, "application/json",
                    json.dumps(body, ensure_ascii=False))


class PortalClientTests(unittest.TestCase):
    def test_success_stores_receipt_and_caches(self):
        receipt = {"request": "WO-CANCEL-P-4A", "receipt": "PRN-00001",
                   "operation": "cancel",
                   "rows": [{"item": "P-4A", "status": "accepted"}], "details": {}}
        transport = FakeTransport(
            {"http://x/printer/portal": json_response(200, receipt)})
        client = PortalClient(transport, "http://x/printer/portal",
                              ReceiptStore(), poll_interval=0)
        result = client.submit(make_action())
        self.assertEqual(result["receipt"], "PRN-00001")
        # 第二次直接走本地存根，不再发请求
        again = client.submit(make_action())
        self.assertEqual(again, result)
        self.assertEqual(len([r for r in transport.requests if r[0] == "POST"]), 1)

    def test_lost_response_recovered_by_receipt_query(self):
        # 首次提交 503；查询第一次 404、第二次拿到回执（不重交）
        query_url = "http://x/printer/portal?request=WO-CANCEL-P-4A"
        receipt = {"request": "WO-CANCEL-P-4A", "receipt": "PRN-00009",
                   "operation": "cancel",
                   "rows": [{"item": "P-4A", "status": "accepted"}], "details": {}}
        transport = FakeTransport(
            routes={
                query_url: json_response(200, receipt),
            },
            posts={
                ("WO-CANCEL-P-4A", "cancel"):
                    json_response(503, {"message": "暂未取得合作端回应"}),
            })
        # 让第一次查询 404，第二次命中
        original_get = transport.get
        calls = {"get": 0}

        def flaky_get(url):
            calls["get"] += 1
            if calls["get"] == 1:
                return json_response(404, {"message": "当前查不到该请求"})
            return original_get(url)

        transport.get = flaky_get
        client = PortalClient(transport, "http://x/printer/portal",
                              ReceiptStore(), poll_attempts=3, poll_interval=0)
        result = client.submit(make_action())
        self.assertEqual(result["receipt"], "PRN-00009")
        self.assertEqual(calls["get"], 2)   # 先查 404，再查命中
        # 没有第二次 POST：等回执没有被误判成取消
        self.assertEqual(sum(1 for method, _, _ in transport.requests
                             if method == "POST"), 1)

    def test_persistent_404_then_identical_resubmit(self):
        receipt = {"request": "WO-CANCEL-P-4A", "receipt": "PRN-00010",
                   "operation": "cancel",
                   "rows": [{"item": "P-4A", "status": "accepted"}], "details": {}}

        class ResubmitTransport(FakeTransport):
            def __init__(self):
                super().__init__(routes={}, posts={})
                self.post_payloads = []

            def get(self, url):
                return json_response(404, {"message": "当前查不到该请求"})

            def post(self, url, payload):
                self.post_payloads.append(payload)
                if len(self.post_payloads) == 1:
                    return json_response(503, {"message": "暂未取得合作端回应"})
                return json_response(200, receipt)

        transport = ResubmitTransport()
        client = PortalClient(transport, "http://x/printer/portal",
                              ReceiptStore(), poll_attempts=2, poll_interval=0)
        result = client.submit(make_action())
        self.assertEqual(result["receipt"], "PRN-00010")
        # 两次 POST 的请求号、动作、条目与明细完全一致（原样重交）
        self.assertEqual(len(transport.post_payloads), 2)
        self.assertEqual(transport.post_payloads[0], transport.post_payloads[1])
        self.assertIsNotNone(client.store.get("CANCEL-P-4A"))

    def test_conflict_does_not_overwrite_store(self):
        transport = FakeTransport(
            routes={"http://x/printer/portal": json_response(
                409, {"message": "业务请求号已有不同内容"})},
            posts={("WO-CANCEL-P-4A", "cancel"): json_response(
                409, {"message": "业务请求号已有不同内容"})})
        client = PortalClient(transport, "http://x/printer/portal",
                              ReceiptStore(), poll_interval=0)
        with self.assertRaises(PortalConflict):
            client.submit(make_action())
        self.assertIsNone(client.store.get("CANCEL-P-4A"))

    def test_persistent_failure_raises(self):
        transport = FakeTransport(
            routes={"http://x/printer/portal?request=WO-CANCEL-P-4A":
                    json_response(404, {"message": "当前查不到该请求"})},
            posts={("WO-CANCEL-P-4A", "cancel"): json_response(
                503, {"message": "暂未取得合作端回应"})})
        client = PortalClient(transport, "http://x/printer/portal",
                              ReceiptStore(), poll_attempts=2, poll_interval=0)
        with self.assertRaises(PortalUnavailable):
            client.submit(make_action())

    def test_receipt_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            store = ReceiptStore(str(path))
            store.put("CANCEL-P-4A", {"receipt": "PRN-1"})
            reloaded = ReceiptStore(str(path))
            self.assertEqual(reloaded.get("CANCEL-P-4A"), {"receipt": "PRN-1"})


if __name__ == "__main__":
    unittest.main()
