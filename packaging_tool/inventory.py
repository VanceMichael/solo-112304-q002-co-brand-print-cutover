"""印线印量核对与已印旧贴归类。

规则落点：
- 旧贴按箱核对：能用在低批号箱上的归“可继续领用”，已落在换版货上的
  隔离，没有现行去向的余量等主管确认；共享一版贴的其他订单不丢有效印量。
- 取消只针对印单未印部分，替换单必须以印厂回执里的真实取消结果为前提。
- 缺授权暂停的订单行保留其未印印量，不代客户取消。
"""
from __future__ import annotations

from .model import (
    Action,
    ActionKind,
    Assessment,
    LinePlan,
    LineState,
    PrintLine,
)

_STATE_PRIORITY = (
    LineState.AWAITING_EDITION,
    LineState.PENDING_SUPERVISOR,
    LineState.SUSPENDED,
    LineState.READY,
)


def plan_lines(assessments: list[Assessment], lines: list[PrintLine],
               reserved: dict[str, int] | None = None) -> list[LinePlan]:
    """按印线汇总箱需求，把已印量分成可领/隔离/待确认三类。

    reserved 给出已实物配到换版箱上的旧贴张数（收件登记口径），这些贴
    必须隔离；未登记时默认没有实物预配，隔离量为 0。
    """
    reserved = reserved or {}
    plans: list[LinePlan] = []
    for line in lines:
        on_line = [a for a in assessments if a.carton.print_line == line.line_id]
        ready = [a for a in on_line if a.state == LineState.READY
                 and a.edition == line.edition]
        awaiting = [a for a in on_line if a.state == LineState.AWAITING_EDITION]
        held = [a for a in on_line if a.state in (
            LineState.SUSPENDED, LineState.PENDING_SUPERVISOR)]

        ready_demand = sum(a.carton.qty for a in ready)
        held_demand = sum(a.carton.qty for a in held)
        new_demand = sum(a.carton.qty for a in awaiting)
        new_editions = sorted({a.edition for a in awaiting if a.edition})

        # 已印旧贴：先隔离已落到换版货上的，再按箱分配给仍适用的箱。
        quarantine = min(reserved.get(line.line_id, 0), line.printed)
        remaining = line.printed - quarantine
        usable = min(remaining, ready_demand)
        pending_supervisor = remaining - usable

        # 缺贴箱号：按已印可领 + 待印的实际覆盖能力逐箱核对，
        # 已印/待印足够的箱不列入；换版箱与暂停箱本身就缺贴，始终列入。
        missing_for: list[str] = []
        leftover = usable + line.queued
        for assessment in ready:
            take = min(leftover, assessment.carton.qty)
            if take < assessment.carton.qty:
                missing_for.append(assessment.carton.carton_id)
            leftover -= take
        missing_for.extend(a.carton.carton_id for a in awaiting)
        missing_for.extend(a.carton.carton_id for a in held)

        # 未印部分还要覆盖：已印不够的现行需求，以及暂停行（恢复后仍用此版）。
        unprinted_needed = max(0, ready_demand - usable) + held_demand
        cancellable = min(line.queued, max(0, line.queued - unprinted_needed))

        notes: list[str] = []
        if awaiting:
            notes.append(
                f"箱 {', '.join(a.carton.carton_id for a in awaiting)} 需换版 "
                f"{new_editions}，旧版 {line.edition} 未印部分须先取消")
        if pending_supervisor:
            notes.append(
                f"已印余量 {pending_supervisor} 张无现行箱货去向，等主管确认后再领")
        if held:
            notes.append(
                f"箱 {', '.join(a.carton.carton_id for a in held)} 暂停，"
                "对应未印印量保留不取消")

        state = LineState.READY
        for candidate in _STATE_PRIORITY:
            if any(a.state == candidate for a in on_line):
                state = candidate
                break

        plans.append(LinePlan(
            line=line,
            required_edition=line.edition,
            state=state,
            usable=usable,
            quarantine=quarantine,
            pending_supervisor=pending_supervisor,
            cancellable_queued=cancellable,
            replacement_qty=new_demand,
            missing_for=missing_for,
            notes=notes,
            ready_demand=ready_demand,
            held_demand=held_demand,
            new_edition=new_editions[0] if len(new_editions) == 1 else None,
        ))
    return plans


def cancellation_confirmed(plan: LinePlan, cancel_receipt: dict | None) -> bool:
    """以逐版回执为准：该印线未印部分是否已被印厂确认取消。

    回执行必须是 accepted，并且队列现状能印证待印量已按取消结果变化；
    “暂时查不到回执”不算取消成功。
    """
    if cancel_receipt is None:
        return False
    rows = {row["item"]: row["status"] for row in cancel_receipt.get("rows", [])}
    if rows.get(plan.line.line_id) != "accepted":
        return False
    return plan.line.queued <= 0 or plan.line.cancelled > 0


def plan_actions(plans: list[LinePlan], receipts: dict[str, dict] | None,
                 request_prefix: str = "WO") -> list[Action]:
    """根据当前队列与已有回执生成门户动作；同一印线的请求号保持稳定。"""
    receipts = receipts or {}
    actions: list[Action] = []
    for plan in plans:
        line = plan.line
        cancel_key = f"CANCEL-{line.line_id}"
        cancel_receipt = receipts.get(cancel_key)
        if plan.cancellable_queued > 0 and cancel_receipt is None:
            actions.append(Action(
                kind=ActionKind.CANCEL,
                line_id=line.line_id,
                request_id=f"{request_prefix}-{cancel_key}",
                items=(line.line_id,),
                details={line.line_id: {
                    "edition": line.edition,
                    "cancel_queued": plan.cancellable_queued,
                }},
                note="取消该印单未印部分，替换单以真实取消回执为准",
            ))

        replace_key = f"REPLACE-{line.line_id}"
        if plan.replacement_qty > 0 and receipts.get(replace_key) is None:
            if not cancellation_confirmed(plan, cancel_receipt):
                continue  # 等回执不是取消成功，不能下替换单
            actions.append(Action(
                kind=ActionKind.REPLACE,
                line_id=line.line_id,
                request_id=f"{request_prefix}-{replace_key}",
                items=(line.line_id,),
                details={line.line_id: {
                    "new_edition": plan.new_edition,
                    "qty": plan.replacement_qty,
                    "based_on_cancel": cancel_receipt["receipt"],
                }},
                note="旧单未印部分确认取消后下替换单",
            ))

        print_key = f"PRINT-{line.line_id}"
        gap = plan.ready_demand + plan.held_demand - plan.usable - line.queued
        if gap > 0 and receipts.get(print_key) is None:
            actions.append(Action(
                kind=ActionKind.PRINT,
                line_id=line.line_id,
                request_id=f"{request_prefix}-{print_key}",
                items=(line.line_id,),
                details={line.line_id: {
                    "edition": line.edition,
                    "qty": gap,
                }},
                note="现行版印量不足，补印",
            ))
    return actions
