"""跨厂包贴换版的编排服务。

一次完整流程：
1. 抓客户目录与全部签署件、变更公告、工厂箱货、印厂队列（保留来源证据）；
2. 按箱核对授权，决定可贴/换版/暂停/待确认；
3. 按印线核对已印、待印、取消与剩余有效张数，归类旧贴并生成门户动作；
4. 在门户执行交接，动作与逐版回执一起写回工单，供主管追溯。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .authorization import AuthorizationRegistry, assess_carton
from .inventory import plan_actions, plan_lines
from .model import (
    Assessment,
    Authorization,
    Carton,
    LinePlan,
    Notice,
    PrintLine,
    ProductPage,
    WorkOrder,
)
from .portal import PortalClient
from .sources import SourceReader


@dataclass
class Snapshot:
    """某一时点从各来源拿到的完整业务快照。"""

    scene_index: int
    authorizations: list[Authorization]
    pages: list[ProductPage]
    unsigned_sources: list[str]
    notices: list[Notice]
    cartons: list[Carton]
    lines: list[PrintLine]
    source_urls: dict = field(default_factory=dict)


class WorkOrderService:
    def __init__(self, reader: SourceReader, endpoints: dict,
                 portal: PortalClient | None = None,
                 reserved: dict[str, int] | None = None):
        self.reader = reader
        self.endpoints = dict(endpoints)
        self.portal = portal
        self.reserved = reserved or {}

    def take_snapshot(self) -> Snapshot:
        catalog = self.endpoints["catalog"]
        notices_url = self.endpoints["notices"]
        cartons_url = self.endpoints["cartons"]
        queue_url = self.endpoints["queue"]
        authorizations, pages, unsigned = self.reader.discover(catalog)
        notices = self.reader.load_notices(notices_url)
        for notice in notices:  # 公告新挂的签署件也要纳入注册表
            if not any(auth.edition_id == notice.new_edition for auth in authorizations):
                authorizations.append(self.reader.fetch_authorization(notice.signed_url))
        return Snapshot(
            scene_index=self._scene_index(),
            authorizations=authorizations,
            pages=pages,
            unsigned_sources=unsigned,
            notices=notices,
            cartons=self.reader.load_cartons(cartons_url),
            lines=self.reader.load_print_lines(queue_url),
            source_urls={
                "catalog": catalog, "notices": notices_url,
                "cartons": cartons_url, "queue": queue_url,
            },
        )

    def evaluate(self, snapshot: Snapshot) -> WorkOrder:
        registry = AuthorizationRegistry(snapshot.authorizations)
        self.reader.registry = registry  # 供沿用字段回查旧版签署件 URL
        assessments = [
            assess_carton(carton, registry, snapshot.notices, self.reader)
            for carton in snapshot.cartons
        ]
        plans = plan_lines(assessments, snapshot.lines, self.reserved)
        order = WorkOrder(
            cartons=snapshot.cartons,
            lines=snapshot.lines,
            assessments=assessments,
            line_plans=plans,
            scene_index=snapshot.scene_index,
            evidence=self._evidence(snapshot, assessments),
        )
        if self.portal is not None:
            order.actions = plan_actions(plans, self.portal.store.all())
        return order

    def execute_handoff(self, order: WorkOrder) -> WorkOrder:
        """在门户执行待办动作；已存回执的动作不会重复提交。"""
        if self.portal is None:
            raise RuntimeError("未配置门户客户端，无法交接")
        self._sync_planned_actions(order)
        for action in order.actions:
            if action.receipt is None:
                action.receipt = self.portal.submit(action)
        # 替换单可能在本轮取消回执到位后才满足条件，再规划并提交一轮。
        self._sync_planned_actions(order)
        for action in order.actions:
            if action.receipt is None:
                action.receipt = self.portal.submit(action)
        return order

    def _sync_planned_actions(self, order: WorkOrder) -> None:
        """按当前回执规划动作，并按稳定请求号合并进工单，避免重复条目。"""
        planned = plan_actions(order.line_plans, self.portal.store.all())
        known = {action.request_id: action for action in order.actions}
        for action in planned:
            current = known.get(action.request_id)
            if current is None:
                order.actions.append(action)
                known[action.request_id] = action
            else:
                current.items = action.items
                current.details = action.details
            stored = self.portal.store.get(PortalClient.key_for(action))
            if stored is not None:
                known[action.request_id].receipt = stored

    def report(self, order: WorkOrder) -> dict:
        return {
            "scene_index": order.scene_index,
            "cartons": [self._carton_row(a) for a in order.assessments],
            "print_lines": [self._line_row(p) for p in order.line_plans],
            "actions": [PortalClient.serialize(action) for action in order.actions],
            "evidence": order.evidence,
        }

    def render_text(self, order: WorkOrder) -> str:
        lines = [f"包贴换版工单（时间点 {order.scene_index}）", "=" * 48, ""]
        lines.append("【箱货核对】")
        for row in self.report(order)["cartons"]:
            lines.append(
                f"- {row['carton']} | {row['region']}/{row['lang']} | "
                f"{row['style']}+{row['component']} 批号{row['component_lot']} | "
                f"需 {row['qty']} 张 | {row['state']} | 版本 {row['edition'] or '-'}")
            if row["missing"]:
                lines.append(f"    缺：{', '.join(row['missing'])}")
            for reason in row["reasons"]:
                lines.append(f"    · {reason}")
        lines.append("")
        lines.append("【印线与旧贴归类】")
        for row in self.report(order)["print_lines"]:
            lines.append(
                f"- {row['line']}（{row['edition']}）状态 {row['state']}："
                f"可领 {row['usable']} / 隔离 {row['quarantine']} / "
                f"待确认 {row['pending_supervisor']} / 可取消未印 {row['cancellable_queued']}"
                f" / 替换需求 {row['replacement_qty']}")
            for note in row["notes"]:
                lines.append(f"    · {note}")
            if row["missing_for"]:
                lines.append(f"    缺贴箱号：{', '.join(row['missing_for'])}")
        lines.append("")
        lines.append("【门户动作】")
        if not order.actions:
            lines.append("- 暂无待办")
        for action in order.actions:
            status = action.receipt["receipt"] if action.receipt else "待提交"
            lines.append(
                f"- {action.kind.value} {action.line_id} 请求号 {action.request_id} -> {status}")
            lines.append(f"    {action.note}")
        lines.append("")
        lines.append("【证据链】")
        for item in order.evidence:
            lines.append(f"- {item['kind']} {item['id']} {item['url']}")
        return "\n".join(lines)

    def _scene_index(self) -> int:
        try:
            response = self.reader.transport.get(
                self.endpoints["scene"])
            if response.status == 200:
                return int(response.json()["index"])
        except Exception:
            pass
        return 0

    def _evidence(self, snapshot: Snapshot,
                  assessments: list[Assessment]) -> list[dict]:
        evidence: list[dict] = []
        for auth in snapshot.authorizations:
            evidence.append({
                "kind": "signed_authorization", "id": auth.edition_id,
                "url": auth.source_url, "style": auth.style,
                "region": auth.region, "lang": auth.lang, "issued": auth.issued,
            })
        for page in snapshot.pages:
            evidence.append({
                "kind": "product_page",
                "id": page.url.rsplit("/", 1)[-1] or page.url,
                "url": page.url,
                "style": page.style, "region": page.region, "lang": page.lang,
                "signed_links": list(page.signed_links),
                "expanded_detail_links": list(page.expanded_detail_links),
            })
        for url in snapshot.unsigned_sources:
            evidence.append({
                "kind": "unsigned_source",
                "id": url.rsplit("/", 1)[-1], "url": url})
        for notice in snapshot.notices:
            evidence.append({
                "kind": "change_notice", "id": notice.notice_id,
                "url": notice.source_url, "new_edition": notice.new_edition,
                "signed_url": notice.signed_url, "from_lot": notice.from_lot,
            })
        for assessment in assessments:
            evidence.append({
                "kind": "assessment_trace",
                "id": assessment.carton.carton_id,
                "url": " | ".join(assessment.evidence),
                "state": assessment.state.value,
            })
        return evidence

    @staticmethod
    def _carton_row(assessment: Assessment) -> dict:
        carton = assessment.carton
        return {
            "carton": carton.carton_id,
            "style": carton.style,
            "component": carton.component,
            "component_lot": carton.component_lot,
            "region": carton.region,
            "lang": carton.lang,
            "qty": carton.qty,
            "print_line": carton.print_line,
            "format": carton.format,
            "state": assessment.state.value,
            "edition": assessment.edition,
            "missing": list(assessment.missing),
            "evidence": list(assessment.evidence),
            "reasons": list(assessment.reasons),
        }

    @staticmethod
    def _line_row(plan: LinePlan) -> dict:
        line = plan.line
        return {
            "line": line.line_id,
            "edition": line.edition,
            "printed": line.printed,
            "queued": line.queued,
            "cancelled": line.cancelled,
            "state": plan.state.value,
            "usable": plan.usable,
            "quarantine": plan.quarantine,
            "pending_supervisor": plan.pending_supervisor,
            "cancellable_queued": plan.cancellable_queued,
            "replacement_qty": plan.replacement_qty,
            "new_edition": plan.new_edition,
            "missing_for": list(plan.missing_for),
            "notes": list(plan.notes),
        }
