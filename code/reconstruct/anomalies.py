"""Auditable cash-state resolution. Links alone never imply cash duplication."""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from code.domain import fx
from code.domain.models import Event

logger = logging.getLogger(__name__)
FxConverter = Callable[[float, str, str, date], float]
SPECIAL_EVENT_TYPES = frozenset({
    "refund", "investment_purchase", "investment_sale", "investment_valuation",
})

ANOMALY_RULES = {
    "PENDING_DEBIT": "Reserve debit on its settlement date.",
    "PENDING_CREDIT": "Drop unconfirmed credit, including pending refunds.",
    "SCHEDULED": "Count the confirmed cash flow on its settlement date.",
    "CANCELLED": "Drop cancelled event.",
    "FAILED": "Drop failed attempt; do not invent a retry.",
    "FAILED_WITH_RETRY": "Drop failed attempt; the linked scheduled retry counts once.",
    "SCHEDULED_RETRY": "Count the scheduled retry, not its failed predecessor.",
    "NON_CASH": "Drop unrealized/non-cash value, regardless of other fields.",
    "SETTLED": "Count settled cash only within the forecast window.",
    "AUTHORIZATION_REPLACED": "Settled purchase replaces its linked authorization.",
    "DUPLICATE_CHARGE": "Count one linked card obligation, preferring settled cash.",
    "REVERSAL": "Keep charge and settled reversal at their respective cash dates.",
    "AWAITING_REFUND": "Keep the purchase debit; pending refund is not cash.",
    "REIMBURSEMENT": "Keep both settled cash flows; reimbursement is not recurring salary.",
    "SALARY_PROJECTION": "Salary module owns this confirmation, including suppression.",
    "OUTSIDE_WINDOW": "Already historical or beyond the forecast end; no cash replay.",
    "MISSING_DATE": "Unresolved cash flow: no settlement date supplied.",
    "MISSING_AMOUNT": "Unresolved cash flow: blank amount is never zero.",
    "INVALID_AMOUNT": "Unresolved cash flow: amount must be finite and non-negative.",
    "UNKNOWN_STATE": "Unresolved status/direction; no invented cash interpretation.",
}

STATUS_RULES = {
    ("pending", "debit"): "PENDING_DEBIT",
    ("pending", "credit"): "PENDING_CREDIT",
    ("scheduled", "debit"): "SCHEDULED",
    ("scheduled", "credit"): "SCHEDULED",
    ("settled", "debit"): "SETTLED",
    ("settled", "credit"): "SETTLED",
}
DROP_RULES = frozenset({"NON_CASH", "CANCELLED", "FAILED", "PENDING_CREDIT"})
LINKED_PAIR_RULES = {
    ("Card authorization", "Settled card purchase"): "AUTHORIZATION_REPLACED",
    ("Original card charge", "Possible duplicate card charge"): "DUPLICATE_CHARGE",
    ("Possible duplicate card charge", "Possible duplicate card charge"): "DUPLICATE_CHARGE",
    ("Card charge later reversed", "Settled card charge reversal"): "REVERSAL",
    ("Purchase awaiting refund", "Pending merchant refund"): "AWAITING_REFUND",
    ("Reimbursable work expense", "Employer expense reimbursement"): "REIMBURSEMENT",
}


@dataclass(frozen=True)
class EventPartition:
    ordinary: tuple[Event, ...]
    special: tuple[Event, ...]


@dataclass(frozen=True)
class EventAudit:
    event_id: str
    reason: str
    linked_event_id: str | None = None
    related_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CashContribution:
    date: date
    amount: float
    currency: str
    category: str
    description: str
    event_id: str
    source: str
    source_event_ids: tuple[str, ...]
    linked_event_id: str | None = None


@dataclass(frozen=True)
class SpecialResolution:
    contributions: tuple[CashContribution, ...]
    dropped_events: tuple[EventAudit, ...]
    unresolved_events: tuple[EventAudit, ...]


def partition_events(user_events: Iterable[Event]) -> EventPartition:
    events = sorted(user_events, key=lambda event: event.event_id)
    if len({event.user_id for event in events}) > 1:
        raise ValueError("Event partition requires one user's events")
    if len({event.event_id for event in events}) != len(events):
        raise ValueError("Duplicate event IDs supplied to event partition")
    referenced = {event.linked_event_id for event in events if event.linked_event_id}
    ordinary, special = [], []
    for event in events:
        is_special = (event.status != "settled" or event.linked_event_id is not None
                      or event.event_id in referenced or event.event_type in SPECIAL_EVENT_TYPES)
        (special if is_special else ordinary).append(event)
    return EventPartition(tuple(ordinary), tuple(special))


def _cash_rule(event: Event) -> str:
    if event.status == "unrealized" or event.direction == "non_cash":
        return "NON_CASH"
    if event.status in {"cancelled", "failed"}:
        return event.status.upper()
    return STATUS_RULES.get((event.status, event.direction), "UNKNOWN_STATE")


def _linked_rules(events: tuple[Event, ...]):
    by_id = {event.event_id: event for event in events}
    related: dict[str, set[str]] = defaultdict(set)
    equivalent: dict[str, set[str]] = defaultdict(set)
    rules: dict[str, str] = {}
    for child in events:
        parent = by_id.get(child.linked_event_id)
        if parent is None:
            if child.linked_event_id:
                logger.warning("Event %s references absent event %s", child.event_id, child.linked_event_id)
            continue
        related[parent.event_id].add(child.event_id)
        related[child.event_id].add(parent.event_id)
        rule = LINKED_PAIR_RULES.get((parent.description, child.description))
        if rule in {"AUTHORIZATION_REPLACED", "DUPLICATE_CHARGE"}:
            if parent.direction == child.direction == "debit":
                equivalent[parent.event_id].add(child.event_id)
                equivalent[child.event_id].add(parent.event_id)
        elif rule:
            rules[parent.event_id] = rules[child.event_id] = rule
        if (parent.status == "failed" and parent.direction == child.direction == "debit"
                and child.status == "scheduled" and parent.category == child.category):
            rules[parent.event_id] = "FAILED_WITH_RETRY"
            rules[child.event_id] = "SCHEDULED_RETRY"

    suppressed: dict[str, str] = {}
    visited = set()
    for start in sorted(equivalent):
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current not in component:
                component.add(current)
                pending.extend(equivalent[current] - component)
        visited.update(component)
        candidates = [by_id[eid] for eid in component
                      if _cash_rule(by_id[eid]) in {"SETTLED", "SCHEDULED", "PENDING_DEBIT"}]
        if not candidates:
            continue
        owner = max(candidates, key=lambda event: (
            event.status == "settled",
            {"Settled card purchase": 3, "Original card charge": 2,
             "Possible duplicate card charge": 1}.get(event.description, 0),
            event.event_date, event.settlement_date or date.min, event.event_id,
        ))
        for eid in sorted(component - {owner.event_id}):
            # A pending authorization is only superseded by actual settlement.
            if by_id[eid].description == "Card authorization" and owner.status != "settled":
                continue
            suppressed[eid] = ("AUTHORIZATION_REPLACED" if by_id[eid].description == "Card authorization"
                               else "DUPLICATE_CHARGE")
            related[owner.event_id].update(component - {owner.event_id})
    return related, rules, suppressed


def resolve_special_events(
    user_events: Iterable[Event], request_date: date, horizon: int = 90, *,
    home_currency: str, fx_converter: FxConverter = fx.convert,
    salary_event_ids: Iterable[str] = (),
) -> SpecialResolution:
    """Resolve special rows, with exactly one disposition for each input row.

    Salary-owned confirmations are explicitly deferred by the ledger consumer.
    Suppression is resolved BEFORE clipping dates, so a historical original
    prevents a pending duplicate from becoming a new future debit. Reversals
    and reimbursements retain their actual dates, including across the boundary.
    """
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 0:
        raise ValueError("horizon must be a non-negative integer")
    end = request_date + timedelta(days=horizon)
    events = partition_events(user_events).special
    related, linked_rules, suppressed = _linked_rules(events)
    salary_owned = set(salary_event_ids)
    contributions, dropped, unresolved = [], [], []
    for event in events:
        related_ids = tuple(sorted(related[event.event_id]))
        rule = _cash_rule(event)
        if rule == "FAILED" and linked_rules.get(event.event_id) == "FAILED_WITH_RETRY":
            rule = "FAILED_WITH_RETRY"
        if rule in DROP_RULES or rule == "FAILED_WITH_RETRY":
            pass
        elif event.event_id in suppressed:
            rule = suppressed[event.event_id]
        elif event.event_id in salary_owned:
            rule = "SALARY_PROJECTION"
        elif rule == "UNKNOWN_STATE":
            pass
        elif event.settlement_date is None:
            rule = "MISSING_DATE"
        elif not request_date <= event.settlement_date <= end:
            rule = "OUTSIDE_WINDOW"
        elif event.amount is None:
            rule = "MISSING_AMOUNT"
        elif not math.isfinite(event.amount) or event.amount < 0:
            rule = "INVALID_AMOUNT"
        else:
            rule = linked_rules.get(event.event_id, rule)
            amount = fx_converter(event.amount, event.currency, home_currency, event.settlement_date)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"Invalid FX result for {event.event_id}")
            contributions.append(CashContribution(
                date=event.settlement_date, amount=amount if event.direction == "credit" else -amount,
                currency=home_currency, category=event.category, description=event.description,
                event_id=event.event_id, source=f"special:{rule}",
                source_event_ids=tuple(sorted((event.event_id,) + related_ids)),
                linked_event_id=event.linked_event_id,
            ))
            continue
        audit = EventAudit(event.event_id, rule, event.linked_event_id, related_ids)
        if rule in {"UNKNOWN_STATE", "MISSING_DATE", "MISSING_AMOUNT", "INVALID_AMOUNT"}:
            unresolved.append(audit)
            logger.warning("Unresolved event %s: %s", event.event_id, ANOMALY_RULES[rule])
        else:
            dropped.append(audit)
            logger.debug("Excluded event %s: %s", event.event_id, ANOMALY_RULES[rule])
    return SpecialResolution(
        tuple(sorted(contributions, key=lambda row: (row.date, row.event_id))),
        tuple(dropped), tuple(unresolved),
    )
