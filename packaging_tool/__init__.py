"""跨厂包贴换版合作端工具。

跟着印单核对客户网页实际发布的授权文字与签署件，评估每箱货需要的
贴纸版本，并在原有印单门户完成取消、替换和加印交接。
"""
from __future__ import annotations

from .model import (
    Action,
    ActionKind,
    Authorization,
    Carton,
    Claim,
    LineState,
    Notice,
    PrintLine,
    ProductPage,
    StickerBucket,
    WorkOrder,
)
from .service import Snapshot, WorkOrderService

__all__ = [
    "Action",
    "ActionKind",
    "Authorization",
    "Carton",
    "Claim",
    "LineState",
    "Notice",
    "PrintLine",
    "ProductPage",
    "Snapshot",
    "StickerBucket",
    "WorkOrder",
    "WorkOrderService",
]
