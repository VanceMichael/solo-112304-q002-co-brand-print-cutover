"""领域模型：授权、变更公告、箱货、印线与工单。

所有对象都保留来源 URL、发行日、区域、语言、适用款式/组件等证据，
任何一条授权都不能脱离这些上下文扩到另一款货。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class LineState(str, Enum):
    """工单行（一箱货或一条印线）所处状态。"""

    READY = "ready"                    # 现行签署件覆盖，可以贴/可领
    AWAITING_EDITION = "awaiting_edition"  # 新版生效，等替换贴
    SUSPENDED = "suspended"            # 缺授权或声明，只停对应订单行
    PENDING_SUPERVISOR = "pending_supervisor"  # 证据不足，等主管确认


class StickerBucket(str, Enum):
    """已印旧贴在新版发布后的归类。"""

    USABLE = "usable"                  # 可继续领用
    QUARANTINE = "quarantine"          # 隔离：旧贴落在新版生效的货上
    PENDING_SUPERVISOR = "pending_supervisor"  # 等主管确认（无适用去向的余量）


class ActionKind(str, Enum):
    """印单门户允许的业务动作。"""

    PRINT = "print"
    CANCEL = "cancel"
    REPLACE = "replace"


@dataclass(frozen=True)
class Claim:
    """签署件里的一条声明。"""

    field_name: str
    value: str
    component: Optional[str] = None  # None 表示对整款声明
    variant: Optional[str] = None    # 衣标变体码（如来源单独声明）


@dataclass(frozen=True)
class Carry:
    """新版沿用旧版字段的指向。"""

    field_name: str
    source_edition: str
    component: Optional[str] = None


@dataclass(frozen=True)
class Authorization:
    """一份签署授权（只能按自身的款式、区域、语言边界使用）。"""

    edition_id: str
    style: str
    region: str
    lang: str
    issued: str
    source_url: str
    claims: tuple[Claim, ...]
    carries: tuple[Carry, ...] = ()

    def claim_for(self, field_name: str, component: Optional[str],
                  variant: Optional[str] = None) -> Optional[Claim]:
        """取精确适用对象的声明，优先级：组件+衣标变体 > 组件级 > 整款级。

        绝不跨对象借用：给了变体码就只接受同变体或更宽层级，不会拿别的
        变体码声明顶替。
        """
        variant_level = None
        component_level = None
        style_level = None
        for claim in self.claims:
            if claim.field_name != field_name:
                continue
            if claim.component == component and claim.variant == variant:
                variant_level = claim
            elif claim.component == component and claim.variant is None:
                component_level = claim
            elif claim.component is None and component is not None:
                style_level = claim
        return variant_level or component_level or style_level

    def has_component_claim(self, component: str) -> bool:
        return any(claim.component == component for claim in self.claims)


@dataclass(frozen=True)
class ProductPage:
    """客户目录里的单品页（可能跳到共用说明页或可打印版本）。"""

    url: str
    style: Optional[str]
    region: Optional[str]
    lang: Optional[str]
    signed_links: tuple[str, ...]
    outbound_links: tuple[str, ...]
    expanded_detail_links: tuple[str, ...]  # details/summary 展开区里的链接


@dataclass(frozen=True)
class Notice:
    """变更公告：从指定组件批号起换版，并声明哪些字段沿用旧版。"""

    notice_id: str
    style: Optional[str]
    component: str
    from_lot: int
    region: str
    new_edition: str
    signed_url: str
    source_url: str
    carry: tuple[Carry, ...]

    def applies_to(self, carton: "Carton") -> bool:
        if carton.region != self.region or carton.component != self.component:
            return False
        if self.style is not None and carton.style != self.style:
            return False
        return carton.component_lot >= self.from_lot


@dataclass(frozen=True)
class Carton:
    """工厂箱货记录：真实组件领用批号、语种、需要张数与允许版型。"""

    carton_id: str
    style: str
    component: str
    component_lot: int
    region: str
    lang: str
    qty: int
    print_line: str
    format: str
    required_fields: tuple[str, ...] = ()  # 订单显式要求核对的声明字段
    variant_code: Optional[str] = None     # 衣标变体码（如来源单独声明）


@dataclass(frozen=True)
class PrintLine:
    """印厂队列里的一个版型（逐版回执的对账单位）。"""

    line_id: str
    edition: str
    printed: int
    queued: int
    cancelled: int = 0


@dataclass
class Action:
    """准备在门户提交或已提交的一个业务动作。"""

    kind: ActionKind
    line_id: str
    request_id: str
    items: tuple[str, ...]
    details: dict = field(default_factory=dict)
    receipt: Optional[dict] = None
    note: str = ""

    @property
    def done(self) -> bool:
        return self.receipt is not None


@dataclass
class WorkOrder:
    """主管视角的工单：箱货、印线、动作与证据快照。"""

    cartons: list[Carton]
    lines: list[PrintLine]
    assessments: list["Assessment"] = field(default_factory=list)
    line_plans: list["LinePlan"] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    scene_index: int = 0


@dataclass(frozen=True)
class Assessment:
    """单箱货的核对结论。"""

    carton: Carton
    state: LineState
    edition: Optional[str]
    missing: tuple[str, ...]
    evidence: tuple[str, ...]          # 支撑结论的网页/签署件 URL
    reasons: tuple[str, ...]


@dataclass
class LinePlan:
    """一条印线在当前时点的处置方案与旧贴归类。"""

    line: PrintLine
    required_edition: Optional[str]
    state: LineState
    usable: int
    quarantine: int
    pending_supervisor: int
    cancellable_queued: int
    replacement_qty: int
    missing_for: list[str]            # 还缺贴的箱号
    notes: list[str]
    ready_demand: int = 0             # 仍用本版的就绪箱需求量
    held_demand: int = 0              # 暂停/待确认箱的保留需求量
    new_edition: Optional[str] = None # 换版箱需要的新版（每线至多一个）
