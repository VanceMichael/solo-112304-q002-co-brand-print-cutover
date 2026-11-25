"""命令行入口：核对、报告与门户交接。

示例：
  python3 -m packaging_tool.cli --base http://127.0.0.1:8765 audit
  python3 -m packaging_tool.cli --base http://127.0.0.1:8765 handoff \
      --receipt-store receipts.jsonl --format json
"""
from __future__ import annotations

import argparse
import json
import sys

from .portal import PortalClient, PortalConflict, PortalUnavailable, ReceiptStore
from .service import WorkOrderService
from .sources import SourceReader
from .transport import HttpTransport, TransportError


def build_service(args) -> WorkOrderService:
    transport = HttpTransport(timeout=args.timeout)
    reader = SourceReader(transport)
    base = args.base.rstrip("/")
    endpoints = {
        "catalog": f"{base}/brand/catalog",
        "notices": f"{base}/brand/notices",
        "cartons": f"{base}/factory/cartons",
        "queue": f"{base}/printer/queue",
        "scene": f"{base}/__scene",
    }
    portal = None
    if args.command == "handoff":
        portal = PortalClient(
            transport, f"{base}/printer/portal",
            ReceiptStore(args.receipt_store),
            poll_attempts=args.poll_attempts, poll_interval=args.poll_interval,
        )
    reserved = json.loads(args.reserved) if args.reserved else None
    return WorkOrderService(reader, endpoints, portal, reserved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨厂包贴换版核对与门户交接工具")
    parser.add_argument("--base", default="http://127.0.0.1:8765",
                        help="本地来源服务根地址")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--reserved",
                        help='已实物预配到换版箱的旧贴张数 JSON，如 {"P-4A": 3}')
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit", help="只核对来源并输出工单报告，不做门户交接")
    handoff = sub.add_parser("handoff", help="核对后在门户执行取消/替换/补印")
    handoff.add_argument("--receipt-store", default="receipts.jsonl",
                         help="回执持久化文件，保证重跑幂等")
    handoff.add_argument("--poll-attempts", type=int, default=4)
    handoff.add_argument("--poll-interval", type=float, default=0.25)
    args = parser.parse_args(argv)

    service = build_service(args)
    try:
        snapshot = service.take_snapshot()
        order = service.evaluate(snapshot)
        if args.command == "handoff":
            service.execute_handoff(order)
    except (TransportError, PortalUnavailable) as exc:
        print(f"交接中断：{exc}", file=sys.stderr)
        return 2
    except PortalConflict as exc:
        print(f"门户请求号冲突，需人工核对：{exc}", file=sys.stderr)
        return 3

    if args.format == "json":
        print(json.dumps(service.report(order), ensure_ascii=False, indent=2))
    else:
        print(service.render_text(order))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
