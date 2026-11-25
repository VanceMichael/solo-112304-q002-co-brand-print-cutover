"""授权注册表与单箱核对。

授权严格按款式、区域、语言隔离；整款声明与组件级声明分开存放。
变更公告沿用旧版字段时，必须现场重新拉取旧版签署件，确认同一适用对象
（同款式/区域/语言/组件）上的旧声明仍可核对，否则该箱转主管确认。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import Assessment, Authorization, Carton, LineState, Notice
from .sources import SourceReader
from .transport import TransportError


@dataclass
class _CarryResolution:
    ok: bool
    reasons: list[str]
    evidence: list[str]


class AuthorizationRegistry:
    def __init__(self, authorizations: list[Authorization]):
        self._all = list(authorizations)
        self._index: dict[tuple[str, str, str], list[Authorization]] = {}
        for auth in authorizations:
            self._index.setdefault((auth.style, auth.region, auth.lang), []).append(auth)

    def for_scope(self, style: str, region: str, lang: str) -> list[Authorization]:
        # 不做任何跨区域/跨语言回退：缺一份语种授权就只能停对应行。
        return list(self._index.get((style, region, lang), []))

    def edition(self, edition_id: str) -> Authorization | None:
        return next((auth for auth in self._all if auth.edition_id == edition_id), None)


def resolve_carries(notice: Notice, new_auth: Authorization,
                    reader: SourceReader, registry: AuthorizationRegistry) -> _CarryResolution:
    """核对公告与新签署件指向的旧版字段是否仍能在旧签署件上核对。"""
    reasons: list[str] = []
    evidence: list[str] = [new_auth.source_url]
    xml_carries = {carry.field_name: carry for carry in new_auth.carries}
    notice_carries = {carry.field_name: carry for carry in notice.carry}
    if set(xml_carries) != set(notice_carries):
        reasons.append(
            f"公告沿用字段 {sorted(notice_carries)} 与签署件 {sorted(xml_carries)} 不一致")
        return _CarryResolution(False, reasons, evidence)
    for field_name, carry in notice_carries.items():
        try:
            previous = reader.fetch_authorization(
                _signed_url_for(notice, registry, carry.source_edition))
        except (TransportError, ValueError) as exc:
            reasons.append(f"沿用字段 {field_name} 的旧版 {carry.source_edition} 无法核对：{exc}")
            continue
        evidence.append(previous.source_url)
        if (previous.style, previous.region, previous.lang) != (
                new_auth.style, new_auth.region, new_auth.lang):
            reasons.append(f"旧版 {carry.source_edition} 适用对象与新版不一致，禁止扩用")
            continue
        target_component = carry.component or notice.component
        claim = previous.claim_for(field_name, target_component)
        if claim is None:
            reasons.append(
                f"旧版 {carry.source_edition} 上找不到 {target_component} 的 {field_name} 声明")
            continue
        reasons.append(
            f"字段 {field_name} 沿用旧版 {carry.source_edition}：{claim.value}（已重新核对）")
    ok = all("无法核对" not in reason and "不一致" not in reason and "找不到" not in reason
             for reason in reasons)
    return _CarryResolution(ok, reasons, evidence)


def _signed_url_for(notice: Notice, registry: AuthorizationRegistry,
                    edition_id: str) -> str:
    """找到旧版签署件 URL：优先注册表，其次按同目录命名规则推断。"""
    auth = registry.edition(edition_id)
    if auth is not None:
        return auth.source_url
    base = notice.signed_url.rsplit("/", 1)[0]
    return f"{base}/{edition_id}.xml"


# 订单没有显式列字段要求时，只要签署件覆盖该组件（或整款）至少一条声明，
# 授权即成立；逐字段缺口只在订单显式要求或公告沿用时才拦截。
DEFAULT_REQUIRED_FIELDS: tuple[str, ...] = ()


def assess_carton(carton: Carton, registry: AuthorizationRegistry,
                  notices: list[Notice], reader: SourceReader) -> Assessment:
    scoped = registry.for_scope(carton.style, carton.region, carton.lang)
    evidence = [auth.source_url for auth in scoped]
    reasons: list[str] = []

    if not scoped:
        return Assessment(
            carton=carton, state=LineState.SUSPENDED, edition=None,
            missing=("该语种销售授权",), evidence=tuple(evidence),
            reasons=(f"缺少 {carton.region}/{carton.lang} 的 {carton.style} 签署授权",),
        )

    active_notice = next((n for n in notices if n.applies_to(carton)), None)
    if active_notice is not None:
        new_auth = registry.edition(active_notice.new_edition)
        if new_auth is None:
            return Assessment(
                carton=carton, state=LineState.PENDING_SUPERVISOR,
                edition=active_notice.new_edition,
                missing=("新版签署件",), evidence=tuple(evidence + [active_notice.source_url]),
                reasons=(f"公告 {active_notice.notice_id} 指向的 {active_notice.new_edition} 未发布",),
            )
        resolution = resolve_carries(active_notice, new_auth, reader, registry)
        reasons.extend(resolution.reasons)
        evidence.extend(url for url in resolution.evidence if url not in evidence)
        if not resolution.ok:
            return Assessment(
                carton=carton, state=LineState.PENDING_SUPERVISOR,
                edition=new_auth.edition_id, missing=("沿用字段可核对性",),
                evidence=tuple(evidence), reasons=tuple(reasons),
            )
        edition = new_auth
        reasons.insert(0, (
            f"组件 {carton.component} 批号 {carton.component_lot} 达到公告 "
            f"{active_notice.notice_id} 起点 {active_notice.from_lot}，启用 {edition.edition_id}"))
    else:
        edition = _latest_for(carton, scoped, notices)
        reasons.append(
            f"组件批号 {carton.component_lot} 未达到任何换版起点，沿用现行 {edition.edition_id}")

    missing = _missing_claims(carton, edition, reasons)
    if missing:
        return Assessment(
            carton=carton, state=LineState.SUSPENDED, edition=edition.edition_id,
            missing=tuple(missing), evidence=tuple(evidence), reasons=tuple(reasons),
        )
    # 公告适用即代表旧贴在该批号失效：授权虽已核对到新版，但箱货在领到
    # 新版贴之前仍是“等换版”，不能直接判可贴。
    state = LineState.AWAITING_EDITION if active_notice is not None else LineState.READY
    return Assessment(
        carton=carton, state=state, edition=edition.edition_id,
        missing=(), evidence=tuple(evidence), reasons=tuple(reasons),
    )


def _latest_for(carton: Carton, scoped: list[Authorization],
                notices: list[Notice]) -> Authorization:
    """无公告适用时的现行版本：排除被同对象公告取代的新版，取剩余最新。

    低批号在途货仍用旧贴，不能因为站点上已经挂出更新的签署件就自动换版；
    换版只能由达到起点批号的公告触发。
    """
    superseded = {
        notice.new_edition for notice in notices
        if notice.region == carton.region and notice.component == carton.component
        and (notice.style is None or notice.style == carton.style)
        and carton.component_lot < notice.from_lot
    }
    candidates = [auth for auth in scoped if auth.edition_id not in superseded]
    pool = candidates or scoped
    # 业务时间以来源当前版本为准；同范围多份时取发行日最新，再按 ID 兜底。
    return sorted(pool, key=lambda auth: (auth.issued, auth.edition_id))[-1]


def _missing_claims(carton: Carton, edition: Authorization,
                    reasons: list[str]) -> list[str]:
    required = getattr(carton, "required_fields", None) or DEFAULT_REQUIRED_FIELDS
    missing: list[str] = []
    for field_name in required:
        claim = edition.claim_for(field_name, carton.component,
                                  carton.variant_code)
        if claim is None:
            missing.append(field_name)
            reasons.append(
                f"{edition.edition_id} 缺少订单要求的 {carton.component} "
                f"{field_name} 声明")
            continue
        target = "组件级" if claim.component == carton.component else "整款级"
        reasons.append(f"{field_name}={claim.value}（{target}声明，来源 {edition.source_url}）")
    if not edition.has_component_claim(carton.component):
        # 箱货带的是特定可拆组件：整款级声明不能代替该组件自身的签署声明。
        missing.append(f"{carton.component} 适用声明")
        reasons.append(
            f"{edition.edition_id} 没有 {carton.component} 组件的任何声明，"
            "整款级声明不能扩用到该组件")
    return missing
