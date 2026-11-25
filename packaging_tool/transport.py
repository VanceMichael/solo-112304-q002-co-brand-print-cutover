"""HTTP 传输层。

只用标准库，便于在隔离环境运行；非 2xx 状态不抛异常，原样返回状态码，
门户的 404（暂时查不到）、409（同号内容冲突）、503（回执丢失）由上层处理。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TransportError(RuntimeError):
    """来源不可达等传输问题（区别于业务状态码）。"""


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    content_type: str
    text: str

    def json(self) -> object:
        return json.loads(self.text)


class HttpTransport:
    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def get(self, url: str) -> Response:
        return self._open(Request(url, method="GET"), url)

    def post(self, url: str, payload: dict) -> Response:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=data, method="POST",
                          headers={"Content-Type": "application/json"})
        return self._open(request, url)

    def _open(self, request: Request, url: str) -> Response:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return Response(
                    url=url,
                    status=response.status,
                    content_type=response.headers.get_content_type(),
                    text=response.read().decode("utf-8"),
                )
        except HTTPError as exc:  # 404/409/503 等都是可处理的业务回应
            return Response(
                url=url,
                status=exc.code,
                content_type=exc.headers.get_content_type() if exc.headers else "",
                text=exc.read().decode("utf-8", errors="replace"),
            )
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"来源无法访问 {url}: {exc}") from exc
