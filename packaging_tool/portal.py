"""原有印单门户的交接客户端。

约束（与来源约定对齐）：
- 同一业务请求号原样重交必须拿回同一张回执；参数变化会被门户拒为 409，
  此时不覆盖本地记录，交主管处理。
- 提交后短暂查不到（404）不等于取消成功：先用业务请求号查询逐版回执，
  只有确认门户没有存根时才用相同参数重交。
- 回执统一含 request/receipt/operation/rows/details，HTML 界面里包在 <pre> 中。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from html import unescape
from pathlib import Path

from .model import Action


class PortalConflict(RuntimeError):
    """同号不同内容（HTTP 409），需要人工核对。"""


class PortalUnavailable(RuntimeError):
    """重试后仍无法取得门户回应。"""


class ReceiptStore:
    """按 (动作键, 请求号) 保存回执，支持跨重跑的 JSONL 持久化。"""

    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else None
        self._receipts: dict[str, dict] = {}
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    record = json.loads(line)
                    self._receipts[record["key"]] = record["receipt"]

    def get(self, key: str) -> dict | None:
        return self._receipts.get(key)

    def put(self, key: str, receipt: dict) -> None:
        self._receipts[key] = receipt
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"key": key, "receipt": receipt}, ensure_ascii=False) + "\n")

    def all(self) -> dict[str, dict]:
        return dict(self._receipts)


def _parse_body(response) -> dict:
    text = response.text
    if "html" in response.content_type and "<pre>" in text:
        text = unescape(text.split("<pre>", 1)[1].split("</pre>", 1)[0])
    return json.loads(text)


class PortalClient:
    def __init__(self, transport, portal_url: str, store: ReceiptStore,
                 poll_attempts: int = 4, poll_interval: float = 0.25):
        self.transport = transport
        self.portal_url = portal_url
        self.store = store
        self.poll_attempts = poll_attempts
        self.poll_interval = poll_interval

    def submit(self, action: Action) -> dict:
        """提交一个门户动作并返回回执，全过程幂等。"""
        key = self.key_for(action)
        cached = self.store.get(key)
        if cached is not None:
            return cached
        payload = {
            "request": action.request_id,
            "operation": action.kind.value,
            "items": list(action.items),
            "details": action.details,
        }
        response = self.transport.post(self.portal_url, payload)
        if response.status == 409:
            raise PortalConflict(_parse_body(response).get("message", "请求号内容冲突"))
        receipt = self._handle_response(response, payload, key)
        action.receipt = receipt
        return receipt

    def _handle_response(self, response, payload: dict, key: str) -> dict:
        if response.status == 200:
            receipt = _parse_body(response)
            self.store.put(key, receipt)
            return receipt
        if response.status in (404, 503):
            return self._recover_after_lost(payload, key, response.status)
        raise PortalUnavailable(f"门户返回 HTTP {response.status}: {response.text[:200]}")

    def _recover_after_lost(self, payload: dict, key: str, first_status: int) -> dict:
        # 第一步：按业务请求号查逐版回执，查不到不视为取消成功。
        for _ in range(self.poll_attempts):
            queried = self._query(payload["request"])
            if queried is not None:
                self.store.put(key, queried)
                return queried
            self._wait()
        # 第二步：确认门户无存根后，用完全相同的参数原样重交。
        retried = self.transport.post(self.portal_url, payload)
        if retried.status == 409:
            # 门户其实已收到原请求：再查一次把原回执取回。
            queried = self._query(payload["request"])
            if queried is not None:
                self.store.put(key, queried)
                return queried
            raise PortalConflict(_parse_body(retried).get("message", "请求号内容冲突"))
        if retried.status != 200:
            raise PortalUnavailable(
                f"重试后仍无回应（首次 {first_status}，重试 {retried.status}）")
        receipt = _parse_body(retried)
        self.store.put(key, receipt)
        return receipt

    def _query(self, request_id: str) -> dict | None:
        from urllib.parse import quote
        url = f"{self.portal_url}?request={quote(request_id)}"
        response = self.transport.get(url)
        if response.status == 404:
            return None
        if response.status != 200:
            raise PortalUnavailable(f"回执查询返回 HTTP {response.status}")
        return _parse_body(response)

    def _wait(self) -> None:
        import time
        time.sleep(self.poll_interval)

    @staticmethod
    def key_for(action: Action) -> str:
        return f"{action.kind.value.upper()}-{action.line_id}"

    @staticmethod
    def serialize(action: Action) -> dict:
        data = asdict(action)
        data["kind"] = action.kind.value
        return data
