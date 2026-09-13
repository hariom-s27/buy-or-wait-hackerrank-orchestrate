"""
Evidence application to reconstructed financial state (Step 10).

Applies surviving EvidenceDelta records to a RequestCase with the spec's precedence:
(1) explicit cancellation / settlement / amendment,
(2) newer record from the same source,
(3) settled over forecast,
(4) financially safer interpretation.

CRITICAL INVARIANT:
Application is strictly IDEMPOTENT. Applying the same delta twice produces
an identical state to applying it once. Provenance is recorded on every
modified line (see ``get_provenance``).
"""
from __future__ import annotations

import calendar
import datetime
import logging
from collections import defaultdict
from dataclasses import replace
from typing import Dict, Iterable, List, Optional, Set, Tuple

from code.config import HORIZON_DAYS
from code.domain.models import Event, RequestCase
from code.evidence.schema import EvidenceDelta, EvidenceIntent
from code.forecast.ledger import occurrences
from code.reconstruct.streams import reconstruct_expense_streams

logger = logging.getLogger(__name__)

# Intents that represent an explicit cancellation, settlement, or amendment of
# a specific fact (precedence rule 1) — these win over a merely newer but less
# definitive record when both target the same entity.
CANCELLATION_OR_SETTLEMENT_INTENTS = frozenset({
    EvidenceIntent.INCOME_ENDED.value,
    EvidenceIntent.SELF_TRANSFER_DUPLICATE.value,
    EvidenceIntent.DISPUTE_OPEN.value,
    EvidenceIntent.REFUND_PENDING.value,
    EvidenceIntent.UNREALIZED_VALUATION.value,
    EvidenceIntent.SALE_SETTLED.value,
    EvidenceIntent.PRIZE_CREDITED.value,
})


def resolve_precedence(deltas: Iterable[EvidenceDelta]) -> List[EvidenceDelta]:
    """Order and de-conflict deltas per the spec's stated precedence.

    When multiple surviving deltas name the SAME (target_type, target):
      1. an explicit cancellation/settlement/amendment wins over a plain amendment
      2. among remaining candidates, the newer record (by the source message's
         ``sent_at``) wins
      3. ties fall back to the financially safer (smaller-amount) interpretation
      4. remaining ties keep the original (first-seen) ordering, deterministically

    Deltas naming distinct targets never conflict and are all kept. Original
    relative ordering of surviving/non-conflicting deltas is preserved.
    """
    deltas = list(deltas)
    by_target: Dict[Tuple[str, str], List[Tuple[int, EvidenceDelta]]] = defaultdict(list)
    for idx, d in enumerate(deltas):
        by_target[(d.target_type, d.target)].append((idx, d))

    winners: List[Tuple[int, EvidenceDelta]] = []
    for _key, items in by_target.items():
        if len(items) == 1:
            winners.append(items[0])
            continue
        cancel_items = [it for it in items if it[1].intent in CANCELLATION_OR_SETTLEMENT_INTENTS]
        pool = cancel_items or items

        def sort_key(item: Tuple[int, EvidenceDelta]):
            idx, d = item
            sent = d.sent_at or ""
            safer = -(d.amount if d.amount is not None else 0.0)  # smaller amount sorts last (wins)
            return (sent, safer, idx)

        pool.sort(key=sort_key)
        winners.append(pool[-1])

    winners.sort(key=lambda item: item[0])
    return [d for _, d in winners]


def get_provenance(case: RequestCase) -> Dict[str, List[str]]:
    """Return {event_id: [source_id, ...]} for every event this case's evidence touched."""
    return dict(getattr(case, "_evidence_provenance", {}))


def _record_provenance(case: RequestCase, event_id: str, source_id: str) -> None:
    prov: Dict[str, List[str]] = getattr(case, "_evidence_provenance", None)
    if prov is None:
        prov = {}
    existing = prov.setdefault(event_id, [])
    if source_id not in existing:
        existing.append(source_id)
    setattr(case, "_evidence_provenance", prov)


def _find_salary_anchor_date(events: List[Event], request_date: datetime.date) -> datetime.date:
    """Find the next expected salary date on or after request_date."""
    salary_events = [e for e in events if e.category == "salary" and e.direction == "credit" and e.settlement_date]
    day = salary_events[-1].settlement_date.day if salary_events else 15
    year = request_date.year
    month = request_date.month
    max_day = calendar.monthrange(year, month)[1]
    cand = datetime.date(year, month, min(day, max_day))
    if cand < request_date:
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        max_day = calendar.monthrange(year, month)[1]
        cand = datetime.date(year, month, min(day, max_day))
    return cand


def _find_latest_salary_amount(events: List[Event], fallback_currency: str) -> Tuple[float, str]:
    """Find the latest settled core salary amount and currency."""
    salary_events = [e for e in events if e.category == "salary" and e.direction == "credit" and e.amount is not None and e.amount > 0]
    if salary_events:
        latest = max(salary_events, key=lambda e: (e.settlement_date or e.event_date, e.event_id))
        return latest.amount, latest.currency  # type: ignore[return-value]
    return 0.0, fallback_currency


def apply_deltas(case: RequestCase, deltas: Iterable[EvidenceDelta]) -> RequestCase:
    """Apply surviving EvidenceDeltas to a RequestCase idempotently.

    Modifies the case's events list in-place and returns the case. Tracks
    applied delta source_ids to guarantee strict idempotence: replaying the
    exact same delta (by source_id) a second time is a no-op.
    """
    applied_sources: Set[str] = getattr(case, "_applied_delta_sources", set())
    events: List[Event] = list(case.events)
    events_by_id: Dict[str, Event] = {e.event_id: e for e in events}
    req_date = case.request.request_date
    home_curr = case.profile.home_currency

    for delta in resolve_precedence(deltas):
        if delta.source_id in applied_sources:
            # Already applied; enforce strict idempotence
            continue

        intent = delta.intent

        # 1. SALARY_SET_AMOUNT
        if intent == EvidenceIntent.SALARY_SET_AMOUNT.value:
            if delta.amount is not None and delta.amount > 0:
                target_date = delta.effective_date or _find_salary_anchor_date(events, req_date)
                target_curr = delta.currency or home_curr
                delta_ev_id = f"delta_salary_{delta.source_id}"
                # Remove prior instance of this specific delta event if present
                events = [e for e in events if e.event_id != delta_ev_id]
                new_ev = Event(
                    event_id=delta_ev_id,
                    user_id=delta.user_id,
                    event_type="income",
                    description="Next confirmed salary",
                    category="salary",
                    direction="credit",
                    amount=delta.amount,
                    currency=target_curr,
                    event_date=target_date,
                    settlement_date=target_date,
                    status="scheduled",
                    linked_event_id=None,
                    flexibility=None,
                    minimum_allowed_amount=None,
                )
                events.append(new_ev)
                events_by_id[delta_ev_id] = new_ev
                _record_provenance(case, delta_ev_id, delta.source_id)
                logger.info("Applied %s: set next salary to %s %s on %s", delta.source_id, delta.amount, target_curr, target_date)

        # 2. SALARY_SET_DATE
        elif intent == EvidenceIntent.SALARY_SET_DATE.value:
            target_date = delta.effective_date
            if target_date:
                amt, curr = _find_latest_salary_amount(events, home_curr)
                if delta.amount is not None:
                    amt = delta.amount
                if delta.currency:
                    curr = delta.currency
                delta_ev_id = f"delta_salary_date_{delta.source_id}"
                events = [e for e in events if e.event_id != delta_ev_id]
                new_ev = Event(
                    event_id=delta_ev_id,
                    user_id=delta.user_id,
                    event_type="income",
                    description="Next confirmed salary",
                    category="salary",
                    direction="credit",
                    amount=amt,
                    currency=curr,
                    event_date=target_date,
                    settlement_date=target_date,
                    status="scheduled",
                    linked_event_id=None,
                    flexibility=None,
                    minimum_allowed_amount=None,
                )
                events.append(new_ev)
                events_by_id[delta_ev_id] = new_ev
                _record_provenance(case, delta_ev_id, delta.source_id)
                logger.info("Applied %s: updated next salary date to %s (amount %s %s)", delta.source_id, target_date, amt, curr)

        # 3. SALARY_RESUME
        elif intent == EvidenceIntent.SALARY_RESUME.value:
            target_date = delta.effective_date or _find_salary_anchor_date(events, req_date)
            target_curr = delta.currency or home_curr
            amt = delta.amount if delta.amount is not None else _find_latest_salary_amount(events, home_curr)[0]
            delta_ev_id = f"delta_salary_resume_{delta.source_id}"
            events = [e for e in events if e.event_id != delta_ev_id]
            new_ev = Event(
                event_id=delta_ev_id,
                user_id=delta.user_id,
                event_type="income",
                description="Next confirmed salary",
                category="salary",
                direction="credit",
                amount=amt,
                currency=target_curr,
                event_date=target_date,
                settlement_date=target_date,
                status="scheduled",
                linked_event_id=None,
                flexibility=None,
                minimum_allowed_amount=None,
            )
            events.append(new_ev)
            events_by_id[delta_ev_id] = new_ev
            _record_provenance(case, delta_ev_id, delta.source_id)
            logger.info("Applied %s: resumed regular salary of %s %s on %s", delta.source_id, amt, target_curr, target_date)

        # 4. INCOME_ENDED
        elif intent == EvidenceIntent.INCOME_ENDED.value:
            target_date = delta.effective_date or req_date
            delta_ev_id = f"delta_salary_term_{delta.source_id}"
            events = [e for e in events if e.event_id != delta_ev_id]
            new_ev = Event(
                event_id=delta_ev_id,
                user_id=delta.user_id,
                event_type="income",
                description="Final employer payroll",
                category="salary",
                direction="credit",
                amount=0.0,
                currency=home_curr,
                event_date=target_date,
                settlement_date=target_date,
                status="settled",
                linked_event_id=None,
                flexibility=None,
                minimum_allowed_amount=None,
            )
            events.append(new_ev)
            events_by_id[delta_ev_id] = new_ev
            _record_provenance(case, delta_ev_id, delta.source_id)
            logger.info("Applied %s: terminated salary stream as of %s", delta.source_id, target_date)

        # 5. RENT_INCREASE_PCT — future rent only. The amendment ("rent
        # increases by X% from the next rent payment") must never rewrite a
        # historical settled event's amount or provenance: it only affects
        # occurrences that have not happened yet. Since the ledger derives
        # each future rent occurrence from the stream's historical mean
        # (code/reconstruct/streams.py, unchanged here), the only way to
        # raise a FUTURE occurrence without touching history is to add new,
        # future-dated, already-"settled" ordinary events at the increased
        # amount: build_ledger_trace's own "ordinary:settled" suppression
        # rule (it never double-counts a stream occurrence on a date an
        # ordinary settled event already covers) then makes each such date
        # use our increased amount instead of the stream's unmodified mean,
        # with no change to code/reconstruct/* or code/forecast/* required.
        elif intent == EvidenceIntent.RENT_INCREASE_PCT.value:
            pct = delta.percent if delta.percent is not None else 12.0
            mult = 1.0 + (pct / 100.0)
            matching_streams = [s for s in reconstruct_expense_streams(events, req_date)
                                 if s.category == delta.target]
            if not matching_streams:
                # No detectable recurring stream for this category (e.g. fewer
                # than two historical occurrences) -- there is no "next rent
                # payment" to anchor an increase to. Per spec: do not rewrite
                # history, do not guess a future date; simply no-op.
                logger.info("Applied %s: no detectable %r stream; rent increase has no future occurrence to apply to",
                            delta.source_id, delta.target)
            else:
                stream = matching_streams[0]
                new_amount = round(stream.level * mult, 2)
                new_minimum = (round(stream.minimum_allowed_amount * mult, 2)
                               if stream.minimum_allowed_amount is not None else None)
                anchor_event = events_by_id.get(stream.representative_event_id)
                source_event_type = anchor_event.event_type if anchor_event is not None else "expense"
                end = req_date + datetime.timedelta(days=HORIZON_DAYS)
                future_dates = [d for d in occurrences(stream.last_date, stream.cadence_days, end,
                                                        anchor_day=stream.anchor_day)
                                 if d >= req_date]
                for on_date in future_dates:
                    delta_ev_id = f"delta_rent_increase_{delta.source_id}_{on_date.isoformat()}"
                    events = [e for e in events if e.event_id != delta_ev_id]
                    new_ev = Event(
                        event_id=delta_ev_id,
                        user_id=delta.user_id,
                        event_type=source_event_type,
                        description=f"{stream.description} (lease +{pct:g}%)",
                        category=stream.category,
                        direction="debit",
                        amount=new_amount,
                        currency=stream.currency,
                        event_date=on_date,
                        settlement_date=on_date,
                        # "settled" (never linked, never a special event_type)
                        # makes this an ORDINARY event so it is counted directly
                        # on its own date and suppresses the stream's own
                        # unmodified-mean projection for that same date --
                        # see code/forecast/ledger.py's observed_expense_dates.
                        status="settled",
                        linked_event_id=None,
                        flexibility=stream.flexibility,
                        minimum_allowed_amount=new_minimum,
                    )
                    events.append(new_ev)
                    events_by_id[delta_ev_id] = new_ev
                    _record_provenance(case, delta_ev_id, delta.source_id)
                logger.info("Applied %s: future %r occurrences increased by %s%% (%d date(s), history unchanged)",
                            delta.source_id, delta.target, pct, len(future_dates))

        # 6. REFUND_PENDING / DISPUTE_OPEN / UNREALIZED_VALUATION / INCOME_UNCONFIRMED
        elif intent in {
            EvidenceIntent.REFUND_PENDING.value,
            EvidenceIntent.DISPUTE_OPEN.value,
            EvidenceIntent.UNREALIZED_VALUATION.value,
            EvidenceIntent.INCOME_UNCONFIRMED.value,
        }:
            target_id = delta.target
            if target_id in events_by_id:
                target_ev = events_by_id[target_id]
                # If target was scheduled or credit, ensure status is 'pending' or 'unrealized'
                new_status = "unrealized" if intent == EvidenceIntent.UNREALIZED_VALUATION.value else "pending"
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description=target_ev.description,
                    category=target_ev.category,
                    direction=target_ev.direction,
                    amount=target_ev.amount,
                    currency=target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=target_ev.settlement_date,
                    status=new_status,
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)
                logger.info("Applied %s: marked %s as %s", delta.source_id, target_id, new_status)

        # 7. SALE_SETTLED / PRIZE_CREDITED
        elif intent in {EvidenceIntent.SALE_SETTLED.value, EvidenceIntent.PRIZE_CREDITED.value}:
            target_id = delta.target
            if target_id in events_by_id:
                target_ev = events_by_id[target_id]
                amt = delta.amount if delta.amount is not None else target_ev.amount
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description=target_ev.description,
                    category=target_ev.category,
                    direction=target_ev.direction,
                    amount=amt,
                    currency=delta.currency or target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=target_ev.settlement_date or target_ev.event_date,
                    status="settled",
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)
                logger.info("Applied %s: settled event %s with amount %s", delta.source_id, target_id, amt)

        # 8. FAILED_DEBIT_RETRY
        elif intent == EvidenceIntent.FAILED_DEBIT_RETRY.value:
            target_id = delta.target
            if target_id in events_by_id:
                target_ev = events_by_id[target_id]
                # The failed debit itself is ignored; the scheduled retry remains
                # (reserved once — never doubled by re-marking an already-pending row).
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description=target_ev.description,
                    category=target_ev.category,
                    direction="debit",
                    amount=target_ev.amount,
                    currency=target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=target_ev.settlement_date or target_ev.event_date,
                    status="pending",
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)
                logger.info("Applied %s: reserved retry for debit %s as pending", delta.source_id, target_id)

        # 9. TWO_CARD_MINIMUMS — keep both obligations separate: no state change,
        # this intent confirms the existing debits should NOT be merged/netted.
        elif intent == EvidenceIntent.TWO_CARD_MINIMUMS.value:
            pass

        # 10. FX_SETTLEMENT — use the established dated FX logic (code/domain/fx.py).
        # If the message confirms a settlement date, align the event's settlement
        # date to it so fx.convert() resolves the correct day's rate; otherwise no-op.
        elif intent == EvidenceIntent.FX_SETTLEMENT.value:
            target_id = delta.target
            if target_id in events_by_id and delta.effective_date is not None:
                target_ev = events_by_id[target_id]
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description=target_ev.description,
                    category=target_ev.category,
                    direction=target_ev.direction,
                    amount=target_ev.amount,
                    currency=target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=delta.effective_date,
                    status=target_ev.status,
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)
                logger.info("Applied %s: confirmed FX settlement date for %s as %s", delta.source_id, target_id, delta.effective_date)

        # 11. SELF_TRANSFER_DUPLICATE — remove the duplicated transfer effect by
        # cancelling it; existing anomaly resolution (cancelled -> DROP) then
        # excludes it from the ledger, exactly like an explicit cancellation.
        elif intent == EvidenceIntent.SELF_TRANSFER_DUPLICATE.value:
            target_id = delta.target
            if target_id in events_by_id:
                target_ev = events_by_id[target_id]
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description=target_ev.description,
                    category=target_ev.category,
                    direction=target_ev.direction,
                    amount=target_ev.amount,
                    currency=target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=target_ev.settlement_date,
                    status="cancelled",
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)
                logger.info("Applied %s: cancelled duplicated self-transfer %s", delta.source_id, target_id)

        # 12. REIMBURSEMENT_NOT_SALARY
        elif intent == EvidenceIntent.REIMBURSEMENT_NOT_SALARY.value:
            target_id = delta.target
            if target_id in events_by_id:
                target_ev = events_by_id[target_id]
                new_ev = Event(
                    event_id=target_ev.event_id,
                    user_id=target_ev.user_id,
                    event_type=target_ev.event_type,
                    description="Employer expense reimbursement",
                    category="other_income",
                    direction=target_ev.direction,
                    amount=target_ev.amount,
                    currency=target_ev.currency,
                    event_date=target_ev.event_date,
                    settlement_date=target_ev.settlement_date,
                    status=target_ev.status,
                    linked_event_id=target_ev.linked_event_id,
                    flexibility=target_ev.flexibility,
                    minimum_allowed_amount=target_ev.minimum_allowed_amount,
                )
                events = [e if e.event_id != target_id else new_ev for e in events]
                events_by_id[target_id] = new_ev
                _record_provenance(case, target_id, delta.source_id)

        # 13. ONE_TIME_ARREARS / INCOME_CONFIRMED_ONE_OFF
        elif intent in {EvidenceIntent.ONE_TIME_ARREARS.value, EvidenceIntent.INCOME_CONFIRMED_ONE_OFF.value}:
            if delta.amount is not None and delta.amount > 0:
                target_date = delta.effective_date or _find_salary_anchor_date(events, req_date)
                target_curr = delta.currency or home_curr
                delta_ev_id = f"delta_oneoff_{delta.source_id}"
                events = [e for e in events if e.event_id != delta_ev_id]
                desc = "Promotion arrears payment" if intent == EvidenceIntent.ONE_TIME_ARREARS.value else "Invoice payment"
                cat = "salary" if intent == EvidenceIntent.ONE_TIME_ARREARS.value else "other_income"
                new_ev = Event(
                    event_id=delta_ev_id,
                    user_id=delta.user_id,
                    event_type="income",
                    description=desc,
                    category=cat,
                    direction="credit",
                    amount=delta.amount,
                    currency=target_curr,
                    event_date=target_date,
                    settlement_date=target_date,
                    status="scheduled",
                    linked_event_id=None,
                    flexibility=None,
                    minimum_allowed_amount=None,
                )
                events.append(new_ev)
                events_by_id[delta_ev_id] = new_ev
                _record_provenance(case, delta_ev_id, delta.source_id)

        # NO_OP and anything else: no financial modification.

        # Record delta as applied
        applied_sources.add(delta.source_id)

    case.events = events
    setattr(case, "_applied_delta_sources", applied_sources)
    return case
