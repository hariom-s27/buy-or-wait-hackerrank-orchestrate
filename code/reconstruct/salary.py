"""Description-based salary state, without expense or payment-decision logic.

``SalaryStream`` extends the Step 2 ``Stream`` without changing that model.
Its ``level`` and ``currency`` describe the confirmed SOURCE salary. Consumers
use ``occurrences`` for dated HOME-currency amounts, not a constant converted
level: exchange rates can differ between occurrences. ``last_date`` is the
recurrence anchor and can be a future authoritative scheduled salary.

Both public functions use the future window (request_date, request_date +
horizon], excluding settled/current income already represented in the balance.
Only settled core history and explicitly scheduled next salary establish state.
Initialize code.domain.fx with the supplied rates before projecting FX income.
"""
from __future__ import annotations

import calendar
import logging
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import Enum

from code.domain import fx
from code.domain.models import Event, Stream

logger = logging.getLogger(__name__)
FxConverter = Callable[[float, str, str, date], float]


class SalaryClass(str, Enum):
    CORE_RECURRING = "CORE_RECURRING"
    TERMINAL = "TERMINAL"
    ONE_OFF = "ONE_OFF"
    IRREGULAR = "IRREGULAR"
    SCHEDULED_NEXT = "SCHEDULED_NEXT"


SALARY_DESCRIPTION_CLASSES = {
    "Payroll credit": SalaryClass.CORE_RECURRING,
    "Base salary": SalaryClass.CORE_RECURRING,
    "Primary household salary": SalaryClass.CORE_RECURRING,
    "International employer payroll": SalaryClass.CORE_RECURRING,
    "Second household income": SalaryClass.CORE_RECURRING,
    "First-job payroll": SalaryClass.CORE_RECURRING,
    "New employer payroll": SalaryClass.CORE_RECURRING,
    "Previous employer payroll": SalaryClass.CORE_RECURRING,
    "Payroll before leave": SalaryClass.CORE_RECURRING,
    "Payroll after returning from leave": SalaryClass.CORE_RECURRING,
    "Temporary assignment pay": SalaryClass.CORE_RECURRING,
    "Peak-season wages": SalaryClass.CORE_RECURRING,
    "Seasonal contract payment": SalaryClass.CORE_RECURRING,
    "Final employer payroll": SalaryClass.TERMINAL,
    "Prorated first salary": SalaryClass.ONE_OFF,
    "Promotion arrears payment": SalaryClass.ONE_OFF,
    "Quarterly performance bonus": SalaryClass.ONE_OFF,
    "Prize proceeds": SalaryClass.ONE_OFF,
    "Investment sale proceeds": SalaryClass.ONE_OFF,
    "Employer expense reimbursement": SalaryClass.ONE_OFF,
    "Performance commission": SalaryClass.ONE_OFF,
    "Monthly sales commission": SalaryClass.ONE_OFF,
    "Account commission payment": SalaryClass.ONE_OFF,
    "Delivery platform payout": SalaryClass.IRREGULAR,
    "Driver platform payout": SalaryClass.IRREGULAR,
    "Weekly app earnings": SalaryClass.IRREGULAR,
    "Task marketplace payout": SalaryClass.IRREGULAR,
    "Website project payment": SalaryClass.IRREGULAR,
    "Consulting invoice payment": SalaryClass.IRREGULAR,
    "Freelance milestone payment": SalaryClass.IRREGULAR,
    "Client retainer payment": SalaryClass.IRREGULAR,
    "Design contract payment": SalaryClass.IRREGULAR,
    "Content contract payment": SalaryClass.IRREGULAR,
    "Application project payment": SalaryClass.IRREGULAR,
    "Independent work payment": SalaryClass.IRREGULAR,
    "Next confirmed salary": SalaryClass.SCHEDULED_NEXT,
}

TRANSITION_FAMILIES = (
    frozenset({"Previous employer payroll", "New employer payroll", "First-job payroll"}),
    frozenset({"Payroll before leave", "Payroll after returning from leave"}),
)
SECOND_HOUSEHOLD = "Second household income"


@dataclass(frozen=True)
class SalaryProvenance:
    identity_event_ids: tuple[str, ...]
    level_event_id: str
    anchor_event_id: str
    calendar_day_event_id: str
    transition_event_ids: tuple[str, ...] = ()
    terminal_event_ids: tuple[str, ...] = ()
    scheduled_next_event_ids: tuple[str, ...] = ()

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(
            self.identity_event_ids
            + (self.level_event_id, self.anchor_event_id, self.calendar_day_event_id)
            + self.transition_event_ids
            + self.terminal_event_ids
            + self.scheduled_next_event_ids
        )))


@dataclass(frozen=True)
class SalaryOccurrence:
    date: date
    amount: float
    currency: str
    source_event_ids: tuple[str, ...]
    scheduled_event_id: str | None = None


@dataclass
class SalaryStream(Stream):
    """A calendar-monthly Stream with auditable, dated income occurrences.

    ``cadence_days=30`` is the existing model's monthly compatibility marker,
    NEVER a timedelta. ``anchor_day`` preserves an unclamped day such as 31.
    ``includes_anchor`` distinguishes an upcoming confirmed paycheck from
    settled history. Re-project from source events when changing the horizon.
    """

    anchor_day: int
    includes_anchor: bool
    provenance: SalaryProvenance
    occurrences: tuple[SalaryOccurrence, ...] = ()


def _event_key(event: Event) -> tuple[date, date, str]:
    # A missing settlement date never becomes an invented cash date.
    assert event.settlement_date is not None
    return event.settlement_date, event.event_date, event.event_id


def _ids(events: Iterable[Event]) -> tuple[str, ...]:
    return tuple(sorted({event.event_id for event in events}))


def _calendar_day_source(history: list[Event]) -> Event:
    """Recover Jan 31 from Jan 31 / Feb 28 history without undoing date changes."""
    source = history[-1]
    for earlier in reversed(history[:-1]):
        earlier_date = _event_key(earlier)[0]
        source_date = _event_key(source)[0]
        if earlier_date.day == min(source_date.day, calendar.monthrange(
            earlier_date.year, earlier_date.month
        )[1]):
            continue
        if source_date.day == min(earlier_date.day, calendar.monthrange(
            source_date.year, source_date.month
        )[1]):
            source = earlier
        else:
            break
    return source


def _next_confirmation(rows: list[Event], request_date: date) -> Event | None:
    upcoming = [row for row in rows if row.status == "scheduled"]
    if upcoming:
        next_date = min(_event_key(row)[0] for row in upcoming)
        same_date = [row for row in upcoming if row.settlement_date == next_date]
        chosen = max(same_date, key=_event_key)
    else:
        chosen = max(rows, key=_event_key) if rows else None
    if len(rows) > 1:
        logger.warning(
            "Multiple next-salary confirmations %s; using %s for the next occurrence",
            _ids(rows), chosen.event_id,
        )
    return chosen


def _salary_state(user_events: Iterable[Event], request_date: date) -> list[SalaryStream]:
    events = list(user_events)
    if len({event.user_id for event in events}) > 1:
        raise ValueError("Salary projection requires events for exactly one user")
    groups: dict[str, list[Event]] = defaultdict(list)
    terminal_rows: list[Event] = []
    next_rows: list[Event] = []
    for event in events:
        if event.category != "salary":
            continue
        kind = SALARY_DESCRIPTION_CLASSES.get(event.description)
        if kind is None:
            logger.warning(
                "Unknown salary description %r (%s, %s): non-projectable",
                event.description, event.user_id, event.event_id,
            )
            continue
        if event.direction != "credit" or kind in {SalaryClass.ONE_OFF, SalaryClass.IRREGULAR}:
            continue
        if event.settlement_date is None:
            logger.warning("Salary %s has no settlement date: non-projectable", event.event_id)
            continue
        historical = event.status == "settled" and event.settlement_date <= request_date
        scheduled = (kind == SalaryClass.SCHEDULED_NEXT and event.status == "scheduled"
                     and event.settlement_date >= request_date)
        if not historical and not scheduled:
            continue
        if kind == SalaryClass.CORE_RECURRING:
            groups[event.description].append(event)
        elif kind == SalaryClass.TERMINAL:
            terminal_rows.append(event)
        elif kind == SalaryClass.SCHEDULED_NEXT:
            next_rows.append(event)

    for history in groups.values():
        history.sort(key=_event_key)
    active = set(groups)
    transitions: dict[str, tuple[str, ...]] = {}
    for family in TRANSITION_FAMILIES:
        members = active & family
        if not members:
            continue
        current = max(members, key=lambda name: _event_key(groups[name][-1]))
        family_ids = _ids(event for name in members for event in groups[name])
        transitions[current] = family_ids
        for previous in sorted(members - {current}):
            logger.info(
                "Salary transition: %s stops; %s is current; source events=%s",
                previous, current, family_ids,
            )
        active -= members - {current}

    # Link an explicit termination to its source description when supplied.
    # Otherwise it terminates the most recently paid main employment at that
    # time, never an independent second household income.
    terminals: dict[str, list[Event]] = defaultdict(list)
    core_by_id = {event.event_id: event for rows in groups.values() for event in rows}
    for terminal in sorted(terminal_rows, key=_event_key):
        linked = core_by_id.get(terminal.linked_event_id)
        candidates = [event for rows in groups.values() for event in rows
                      if event.description != SECOND_HOUSEHOLD
                      and _event_key(event)[0] <= _event_key(terminal)[0]]
        target = linked or (max(candidates, key=_event_key) if candidates else None)
        if target is not None:
            terminals[target.description].append(terminal)
        logger.info(
            "Final employer payroll %s terminates %s; source events=%s",
            terminal.event_id, target.description if target else "main employment",
            (target.event_id, terminal.event_id) if target else (terminal.event_id,),
        )

    next_event = _next_confirmation(next_rows, request_date)
    main = active - {SECOND_HOUSEHOLD}
    linked_next = core_by_id.get(next_event.linked_event_id) if next_event else None
    if linked_next and linked_next.description in main:
        next_target = linked_next.description
    else:
        next_target = max(main, key=lambda name: _event_key(groups[name][-1])) if main else None
    if next_event and next_target is None:
        if terminal_rows:
            logger.warning("Next salary %s cannot revive terminated employment %s",
                           next_event.event_id, _ids(terminal_rows))
            next_event = None
        else:
            next_target = next_event.description
            groups[next_target] = [next_event]
            active.add(next_target)

    streams: list[SalaryStream] = []
    for description in sorted(active):
        history = groups[description]
        latest = history[-1]
        relevant_terminals = terminals.get(description, [])
        if relevant_terminals and _event_key(latest)[0] <= max(
            _event_key(row)[0] for row in relevant_terminals
        ):
            logger.info("Salary %s is terminated: history=%s terminal=%s",
                        description, _ids(history), _ids(relevant_terminals))
            continue
        override = next_event if description == next_target else None
        if override and _event_key(override)[0] < _event_key(latest)[0]:
            override = None  # A newer settled recurring level supersedes old confirmation.
        level_event = override or latest
        if (level_event.amount is None or not math.isfinite(level_event.amount)
                or level_event.amount <= 0):
            logger.warning("Salary %s has unresolved/non-positive level %r at %s: non-projectable",
                           description, level_event.amount, level_event.event_id)
            continue
        anchor = _event_key(level_event)[0]
        day_source = override or _calendar_day_source(history)
        provenance = SalaryProvenance(
            identity_event_ids=_ids(history),
            level_event_id=level_event.event_id,
            anchor_event_id=level_event.event_id,
            calendar_day_event_id=day_source.event_id,
            transition_event_ids=transitions.get(description, ()),
            terminal_event_ids=_ids(relevant_terminals),
            scheduled_next_event_ids=_ids(next_rows) if override else (),
        )
        streams.append(SalaryStream(
            stream_id=f"salary:{description}", user_id=latest.user_id,
            category="salary", description=description, direction="credit",
            currency=level_event.currency, level=level_event.amount, cadence_days=30,
            last_date=anchor, representative_event_id=level_event.event_id,
            flexibility=latest.flexibility, minimum_allowed_amount=latest.minimum_allowed_amount,
            anchor_day=_event_key(day_source)[0].day,
            includes_anchor=bool(override and override.status == "scheduled"),
            provenance=provenance,
        ))
    return streams


def _future_dates(stream: SalaryStream, request_date: date, horizon: int) -> list[date]:
    end = request_date + timedelta(days=horizon)
    anchor = stream.last_date
    month = max(0 if stream.includes_anchor else 1,
                (request_date.year - anchor.year) * 12 + request_date.month - anchor.month)
    result = []
    while True:
        year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + month, 12)
        day = min(stream.anchor_day, calendar.monthrange(year, month_index + 1)[1])
        occurrence = date(year, month_index + 1, day)
        if occurrence > end:
            return result
        if occurrence > request_date:
            result.append(occurrence)
        month += 1


def _validate_horizon(horizon: int) -> None:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 0:
        raise ValueError("horizon must be a non-negative integer number of days")


def project_salary_streams(
    user_events: Iterable[Event],
    request_date: date,
    horizon: int = 90,
    fx_converter: FxConverter = fx.convert,
    *,
    home_currency: str,
) -> list[SalaryStream]:
    """Return Stream-compatible salaries with future home-currency occurrences.

    ``home_currency`` must come from the user's Profile, never inferred from an
    Event's transaction currency. Source level/currency stay on each stream;
    every occurrence is converted separately using its actual settlement date.
    No DataFrames, implicit dataset loads, external APIs, or shared event mutation.
    """
    _validate_horizon(horizon)
    if not home_currency:
        raise ValueError("home_currency must be supplied from the user's profile")
    projected = []
    for stream in _salary_state(user_events, request_date):
        occurrences = []
        for on_date in _future_dates(stream, request_date, horizon):
            amount = fx_converter(stream.level, stream.currency, home_currency, on_date)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError(f"Invalid converted salary for {stream.stream_id} on {on_date}")
            occurrences.append(SalaryOccurrence(
                date=on_date, amount=amount, currency=home_currency,
                source_event_ids=stream.provenance.source_event_ids,
                scheduled_event_id=(stream.provenance.anchor_event_id
                                    if stream.includes_anchor and on_date == stream.last_date
                                    else None),
            ))
        if occurrences:
            projected.append(replace(stream, occurrences=tuple(occurrences)))
    return projected


def income_dates(
    user_events: Iterable[Event], request_date: date, horizon: int = 90,
) -> list[date]:
    """Sorted unique future salary dates using the same state and calendar rules.

    Dates need no currency conversion. This does not decide payment timing.
    """
    _validate_horizon(horizon)
    return sorted({on_date for stream in _salary_state(user_events, request_date)
                   for on_date in _future_dates(stream, request_date, horizon)})
