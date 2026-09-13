"""Expense recurrence from ordinary settled debit history, grouped by category."""
from __future__ import annotations

import calendar
import logging
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from statistics import fmean, median

from code.domain import fx
from code.domain.models import Event, Stream
from code.reconstruct.anomalies import FxConverter, partition_events

logger = logging.getLogger(__name__)


@dataclass
class ExpenseStream(Stream):
    cadence_days: float
    source_event_ids: tuple[str, ...]
    amount_event_ids: tuple[str, ...]
    anchor_day: int
    calendar_day_event_id: str


def _calendar_anchor(history: list[Event]) -> Event:
    """Retain an unclamped monthly day only while recent observations agree."""
    anchor = history[-1]
    for earlier in reversed(history[:-1]):
        recent_date = anchor.settlement_date
        earlier_date = earlier.settlement_date
        if earlier_date.day == min(recent_date.day, calendar.monthrange(
            earlier_date.year, earlier_date.month,
        )[1]):
            continue
        if recent_date.day != min(earlier_date.day, calendar.monthrange(
            recent_date.year, recent_date.month,
        )[1]):
            break
        anchor = earlier
    return anchor


def reconstruct_expense_streams(
    user_events: Iterable[Event], request_date: date, *,
    fx_converter: FxConverter = fx.convert,
) -> list[ExpenseStream]:
    """Use settlement dates for recurrence; at least two observations are needed.

    The newest row supplies identity and change-related metadata. Blank observed
    amounts are logged and excluded from the mean, never filled with zero. If a
    category contains multiple currencies, normalize observations to the newest
    row's currency at their cash dates before averaging. Projection converts that
    source-currency level at each future occurrence date.
    """
    groups: dict[str, list[Event]] = defaultdict(list)
    for event in partition_events(user_events).ordinary:
        if event.direction != "debit":
            continue
        if event.settlement_date is None:
            logger.warning("Expense %s lacks a settlement date; not recurrence evidence", event.event_id)
        elif event.settlement_date <= request_date:
            groups[event.category].append(event)
    streams = []
    for category, history in sorted(groups.items()):
        history.sort(key=lambda event: (event.settlement_date, event.event_date, event.event_id))
        if len(history) < 2:
            logger.info("Expense %s has only one historical occurrence; no recurrence", category)
            continue
        gap = median((right.settlement_date - left.settlement_date).days
                     for left, right in zip(history, history[1:]))
        if gap < 1:
            logger.warning("Expense %s has no positive daily cadence; no invented recurrence", category)
            continue
        newest = history[-1]
        amounts, amount_ids = [], []
        for event in history:
            if event.amount is None:
                logger.warning("Expense %s has a blank historical amount; excluded from mean", event.event_id)
                continue
            if not math.isfinite(event.amount) or event.amount < 0:
                raise ValueError(f"Invalid expense amount at {event.event_id}")
            amount = fx_converter(event.amount, event.currency, newest.currency, event.settlement_date)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"Invalid expense FX result at {event.event_id}")
            amounts.append(amount)
            amount_ids.append(event.event_id)
        if not amounts:
            raise ValueError(f"Unresolved recurring expense level: {category}")
        anchor = _calendar_anchor(history) if 26 <= gap <= 32 else newest
        streams.append(ExpenseStream(
            stream_id=f"expense:{category}", user_id=newest.user_id, category=category,
            description=newest.description, direction="debit", currency=newest.currency,
            level=fmean(amounts), cadence_days=gap, last_date=newest.settlement_date,
            representative_event_id=newest.event_id, flexibility=newest.flexibility,
            minimum_allowed_amount=newest.minimum_allowed_amount,
            source_event_ids=tuple(sorted(event.event_id for event in history)),
            amount_event_ids=tuple(sorted(amount_ids)),
            anchor_day=anchor.settlement_date.day, calendar_day_event_id=anchor.event_id,
        ))
    return streams
