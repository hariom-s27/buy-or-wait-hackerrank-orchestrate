"""Inclusive daily cash movements and a source trace, without payment decisions."""
from __future__ import annotations

import calendar
import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from code.config import HORIZON_DAYS
from code.domain import fx
from code.domain.models import RequestCase
from code.reconstruct import salary
from code.reconstruct.anomalies import (
    CashContribution, EventAudit, FxConverter, SpecialResolution,
    partition_events, resolve_special_events,
)
from code.reconstruct.streams import ExpenseStream, reconstruct_expense_streams


@dataclass(frozen=True)
class LedgerTrace:
    ledger: dict[date, float]
    contributions: tuple[CashContribution, ...]
    expense_streams: tuple[ExpenseStream, ...]
    salary_streams: tuple[salary.SalaryStream, ...]
    special_resolution: SpecialResolution


class UnresolvedCashEvents(ValueError):
    """An incomplete cash forecast must not masquerade as a complete ledger."""

    def __init__(self, events: tuple[EventAudit, ...]):
        self.events = events
        super().__init__("Unresolved cash events: " + "; ".join(
            f"{event.event_id}: {event.reason}" for event in events
        ))


def occurrences(last_date: date, gap: float, end: date, *, anchor_day: int | None = None) -> list[date]:
    """Expand after last_date through end, retaining the original calendar anchor.

    Gaps from 26 through 32 (including fractional medians) are monthly. Other
    positive gaps use elapsed days; fractional days accumulate from the anchor
    before reducing to calendar dates, avoiding repeated truncation drift.
    """
    if isinstance(gap, bool) or not math.isfinite(gap) or gap < 1:
        raise ValueError("gap must be finite and at least one day")
    day = last_date.day if anchor_day is None else anchor_day
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise ValueError("anchor_day must be an integer from 1 through 31")
    result = []
    step = 1
    while last_date < end:
        if 26 <= gap <= 32:
            year, month = divmod(last_date.year * 12 + last_date.month - 1 + step, 12)
            next_date = date(year, month + 1, min(
                day, calendar.monthrange(year, month + 1)[1],
            ))
        else:
            next_date = last_date + timedelta(days=gap * step)
        if next_date > end:
            break
        result.append(next_date)
        step += 1
    return result


def _salary_contributions(events, request_date, horizon, home_currency, fx_converter):
    # Step 3 deliberately excludes its request date. Retain its current state,
    # then obtain just the boundary day's projection through the same public
    # engine. A changed/terminated state today must not revive yesterday's stream.
    streams = salary.project_salary_streams(
        events, request_date, max(horizon, 32), fx_converter,
        home_currency=home_currency,
    )
    end = request_date + timedelta(days=horizon)
    projected = [(stream, row) for stream in streams for row in stream.occurrences if row.date <= end]
    active = {stream.stream_id: stream for stream in streams}
    boundary = salary.project_salary_streams(
        events, request_date - timedelta(days=1), 1, fx_converter,
        home_currency=home_currency,
    )
    for earlier in boundary:
        current = active.get(earlier.stream_id)
        if current is not None and current.provenance == earlier.provenance:
            projected.extend((current, row) for row in earlier.occurrences if row.date == request_date)
    result = []
    for stream, row in projected:
        result.append(CashContribution(
            date=row.date, amount=row.amount, currency=row.currency,
            category=stream.category, description=stream.description,
            event_id=row.scheduled_event_id or stream.representative_event_id,
            source=stream.stream_id, source_event_ids=row.source_event_ids,
        ))
    return streams, result


def build_ledger_trace(
    user: RequestCase, request_date: date, horizon: int = HORIZON_DAYS,
    changes: Mapping[str, float | None] | None = None, *,
    fx_converter: FxConverter = fx.convert,
) -> LedgerTrace:
    """Build a fresh scenario and trace from a typed RequestCase.

    Opening balance stays on user.profile.current_available_balance. This
    function returns movements, never replays older settled cash, and never
    modifies Events, Streams, or prior scenario dictionaries. Overlay amounts
    are expressed in the expense stream's source currency. Known cash events
    are not edited by an overlay; only inferred future expense occurrences are.
    """
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 0:
        raise ValueError("horizon must be a non-negative integer")
    events = tuple(user.events)
    home_currency = user.profile.home_currency
    if user.request.user_id != user.profile.user_id or any(
        event.user_id != user.profile.user_id for event in events
    ):
        raise ValueError("Ledger case contains mismatched users")
    end = request_date + timedelta(days=horizon)
    partition = partition_events(events)
    expense_streams = reconstruct_expense_streams(events, request_date, fx_converter=fx_converter)
    overlays = dict(changes or {})
    by_representative = {stream.representative_event_id: stream for stream in expense_streams}
    for event_id, amount in overlays.items():
        if event_id not in by_representative:
            raise ValueError(f"Unknown expense-stream representative: {event_id}")
        if amount is not None and (isinstance(amount, bool) or not math.isfinite(amount)
                                   or not 0 <= amount <= by_representative[event_id].level):
            raise ValueError(f"Expense reduction must be between zero and baseline level: {event_id}")

    # Ownership is shared from Step 3's table. Even a suppressed confirmation
    # must not leak back in through the generic scheduled-event rule.
    salary_owned = {event.event_id for event in events if event.category == "salary"
                    and salary.SALARY_DESCRIPTION_CLASSES.get(event.description)
                    == salary.SalaryClass.SCHEDULED_NEXT}
    special = resolve_special_events(
        events, request_date, horizon, home_currency=home_currency,
        fx_converter=fx_converter, salary_event_ids=salary_owned,
    )
    contributions = list(special.contributions)
    unresolved = list(special.unresolved_events)
    for event in partition.ordinary:
        if event.direction == "non_cash":
            continue
        if event.settlement_date is None:
            unresolved.append(EventAudit(event.event_id, "MISSING_DATE"))
            continue
        if not request_date <= event.settlement_date <= end:
            continue
        if event.amount is None:
            unresolved.append(EventAudit(event.event_id, "MISSING_AMOUNT"))
            continue
        if not math.isfinite(event.amount) or event.amount < 0:
            unresolved.append(EventAudit(event.event_id, "INVALID_AMOUNT"))
            continue
        if event.direction not in {"credit", "debit"}:
            unresolved.append(EventAudit(event.event_id, "UNKNOWN_STATE"))
            continue
        amount = fx_converter(event.amount, event.currency, home_currency, event.settlement_date)
        contributions.append(CashContribution(
            date=event.settlement_date, amount=amount if event.direction == "credit" else -amount,
            currency=home_currency, category=event.category, description=event.description,
            event_id=event.event_id, source="ordinary:settled", source_event_ids=(event.event_id,),
        ))
    if unresolved:
        raise UnresolvedCashEvents(tuple(sorted(unresolved, key=lambda row: row.event_id)))

    observed_expense_dates = {(row.category, row.date) for row in contributions
                             if row.source == "ordinary:settled" and row.amount <= 0}
    observed_income_dates = {(row.description, row.date) for row in contributions
                            if row.category == "salary" and row.amount >= 0}
    for stream in expense_streams:
        level = overlays.get(stream.representative_event_id, stream.level)
        if level is None:
            continue
        for on_date in occurrences(stream.last_date, stream.cadence_days, end, anchor_day=stream.anchor_day):
            if on_date < request_date or (stream.category, on_date) in observed_expense_dates:
                continue
            amount = fx_converter(level, stream.currency, home_currency, on_date)
            contributions.append(CashContribution(
                date=on_date, amount=-amount, currency=home_currency, category=stream.category,
                description=stream.description, event_id=stream.representative_event_id,
                source=stream.stream_id, source_event_ids=stream.source_event_ids,
            ))
    salary_streams, salary_rows = _salary_contributions(
        events, request_date, horizon, home_currency, fx_converter,
    )
    contributions.extend(row for row in salary_rows
                         if (row.description, row.date) not in observed_income_dates)

    daily: dict[date, list[float]] = defaultdict(list)
    for row in contributions:
        if not math.isfinite(row.amount):
            raise ValueError(f"Invalid cash amount from {row.event_id}")
        daily[row.date].append(row.amount)
    ledger = {request_date + timedelta(days=offset): math.fsum(
        daily[request_date + timedelta(days=offset)]
    ) for offset in range(horizon + 1)}
    return LedgerTrace(
        ledger, tuple(sorted(contributions, key=lambda row: (row.date, row.source, row.event_id))),
        tuple(expense_streams), tuple(salary_streams), special,
    )


def build_ledger(
    user: RequestCase, request_date: date, horizon: int = HORIZON_DAYS,
    changes: Mapping[str, float | None] | None = None,
) -> dict[date, float]:
    """Net daily home-currency movement over [request_date, request_date+horizon]."""
    return build_ledger_trace(user, request_date, horizon, changes).ledger


def trough(
    ledger: Mapping[date, float], opening_balance: float, from_date: date,
    extra_payments: Mapping[date, float] | None = None,
) -> float:
    """Minimum end-of-day balance from from_date, never an intraday minimum.

    This is only a balance primitive; it does not calculate capacity or choose
    a payment date, amount, method, or plan. Extra payments are positive debits.
    Opening balance precedes the first ledger date; earlier daily movements
    still affect balances when inspecting a later portion of the ledger.
    """
    payments = dict(extra_payments or {})
    if any(day not in ledger or not math.isfinite(amount) or amount < 0
           for day, amount in payments.items()):
        raise ValueError("Extra payments must be finite non-negative amounts on ledger dates")
    balance = opening_balance
    minimum = None
    for on_date in sorted(ledger):
        balance = math.fsum((balance, ledger[on_date], -payments.get(on_date, 0.0)))
        if on_date >= from_date:
            minimum = balance if minimum is None else min(minimum, balance)
    return balance if minimum is None else minimum
