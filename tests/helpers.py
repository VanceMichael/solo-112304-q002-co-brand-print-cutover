"""测试辅助：脚本化的假传输层与基于真实 fixture 的服务装配。"""
from __future__ import annotations

import json
from threading import Thread
from urllib.parse import urlsplit

from source_fixture import Exchange

from packaging_tool.portal import PortalClient, ReceiptStore
from packaging_tool.service import WorkOrderService
from packaging_tool.sources import SourceReader
from packaging_tool.transport import Response


class FakeTransport:
    """按 URL/方法返回预置回应，并记录全部请求。"""

    def __init__(self, routes: dict, posts: dict | None = None):
        # routes: {url: Response 或 (status, dict)}；posts 同结构，按 (method, path) 覆盖
        self.routes = routes
        self.posts = posts or {}
        self.requests: list[tuple[str, str, dict | None]] = []

    def get(self, url: str) -> Response:
        self.requests.append(("GET", url, None))
        return self._materialize(self.routes[url], url)

    def post(self, url: str, payload: dict) -> Response:
        path = urlsplit(url).path
        self.requests.append(("POST", path, payload))
        key = (payload.get("request"), payload.get("operation"))
        if key in self.posts:
            return self._materialize(self.posts[key], url)
        if url in self.posts:
            return self._materialize(self.posts[url], url)
        return self._materialize(self.routes.get(url, (404, {"message": "未配置"})), url)

    @staticmethod
    def _materialize(prepared, url: str) -> Response:
        if isinstance(prepared, Response):
            return prepared
        status, body = prepared
        if isinstance(body, str):
            return Response(url, status, "text/html", body)
        return Response(url, status, "application/json",
                        json.dumps(body, ensure_ascii=False))


class FixtureServer:
    """在随机回环端口启动真实 source_fixture，供端到端测试使用。"""

    def __init__(self):
        self.server = Exchange(("127.0.0.1", 0))
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.thread = Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def scene(self, index: int) -> None:
        import urllib.request
        request = urllib.request.Request(
            f"{self.origin}/__scene",
            data=json.dumps({"index": index}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200

    def endpoints(self) -> dict:
        return {
            "catalog": f"{self.origin}/brand/catalog",
            "notices": f"{self.origin}/brand/notices",
            "cartons": f"{self.origin}/factory/cartons",
            "queue": f"{self.origin}/printer/queue",
            "scene": f"{self.origin}/__scene",
        }

    def service(self, receipt_path: str | None = None, poll_interval: float = 0.01,
                poll_attempts: int = 3) -> WorkOrderService:
        from packaging_tool.transport import HttpTransport
        transport = HttpTransport(timeout=3)
        reader = SourceReader(transport)
        portal = PortalClient(
            transport, f"{self.origin}/printer/portal",
            ReceiptStore(receipt_path),
            poll_attempts=poll_attempts, poll_interval=poll_interval)
        return WorkOrderService(reader, self.endpoints(), portal)
