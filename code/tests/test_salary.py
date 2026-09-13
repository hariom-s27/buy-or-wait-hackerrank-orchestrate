"""Synthetic salary-state tests; no dataset rows, requests, or external APIs."""
from __future__ import annotations

import copy
import logging
from dataclasses import replace
from datetime import date
from itertools import permutations

import pytest

from code.domain import fx
from code.domain.models import Event, ExchangeRate, Stream
from code.reconstruct.salary import (
    SALARY_DESCRIPTION_CLASSES,
    SalaryClass,
    income_dates,
    project_salary_streams,
)


def d(value: str) -> date:
    return date.fromisoformat(value)


def event(event_id, description, on_date, amount=1000.0, **changes):
    on_date = d(on_date)
    return replace(Event(
        event_id=event_id, user_id="synthetic-household", event_type="income",
        description=description, category="salary", direction="credit",
        amount=amount, currency="USD", event_date=on_date, settlement_date=on_date,
        status="settled", linked_event_id=None, flexibility="fixed",
        minimum_allowed_amount=None,
    ), **changes)


def project(rows, request="2025-02-01", horizon=90, **kwargs):
    kwargs.setdefault("home_currency", "USD")
    return project_salary_streams(rows, d(request), horizon, **kwargs)


def dates(stream):
    return [occurrence.date for occurrence in stream.occurrences]


def test_two_independent_monthly_streams():
    rows = [
        event("primary-dec", "Primary household salary", "2024-12-15", 3000),
        event("second-dec", "Second household income", "2024-12-20", 1800),
        event("primary-jan", "Primary household salary", "2025-01-15", 3000),
        event("second-jan", "Second household income", "2025-01-20", 1700),
    ]
    primary, second = project(rows)
    assert isinstance(primary, Stream) and isinstance(second, Stream)
    assert primary.stream_id == "salary:Primary household salary"
    assert second.stream_id == "salary:Second household income"
    assert dates(primary) == [d("2025-02-15"), d("2025-03-15"), d("2025-04-15")]
    assert dates(second) == [d("2025-02-20"), d("2025-03-20"), d("2025-04-20")]
    assert (primary.level, second.level) == (3000, 1700)
    assert primary.provenance.identity_event_ids == ("primary-dec", "primary-jan")
    assert second.provenance.identity_event_ids == ("second-dec", "second-jan")
    assert all(stream.cadence_days == 30 for stream in (primary, second))


@pytest.mark.parametrize("previous,current", [
    ("Previous employer payroll", "New employer payroll"),
    ("First-job payroll", "New employer payroll"),
    ("Payroll before leave", "Payroll after returning from leave"),
])
def test_only_current_transition_stream_projects(previous, current):
    rows = [event("old-a", previous, "2024-09-15", 900),
            event("old-b", previous, "2024-10-15", 900),
            event("current", current, "2025-01-15", 1400)]
    stream, = project(rows)
    assert stream.description == current
    assert stream.level == 1400
    assert stream.last_date == d("2025-01-15")
    assert dates(stream) == [d("2025-02-15"), d("2025-03-15"), d("2025-04-15")]
    assert stream.provenance.identity_event_ids == ("current",)
    assert set(stream.provenance.transition_event_ids) == {"old-a", "old-b", "current"}
    assert stream.cadence_days == 30


def test_transition_is_chronological_not_a_description_priority():
    rows = [event("new-early", "New employer payroll", "2024-12-15", 3000),
            event("previous-later", "Previous employer payroll", "2025-01-15", 1000)]
    stream, = project(rows)
    assert stream.description == "Previous employer payroll"
    assert stream.level == 1000


def test_final_employer_payroll_stops_future_income(caplog):
    rows = [event("regular", "Payroll credit", "2024-12-15"),
            event("final", "Final employer payroll", "2025-01-15")]
    with caplog.at_level(logging.INFO):
        assert project(rows) == []
    assert income_dates(rows, d("2025-02-01")) == []
    assert "regular" in caplog.text and "final" in caplog.text
    assert "terminated" in caplog.text


def test_final_payroll_preserves_independent_second_household_income():
    rows = [event("primary", "Primary household salary", "2024-12-15", 3000),
            event("second", "Second household income", "2025-01-20", 1200),
            event("final", "Final employer payroll", "2025-01-15", 3000)]
    stream, = project(rows)
    assert stream.description == "Second household income"
    assert dates(stream) == [d("2025-02-20"), d("2025-03-20"), d("2025-04-20")]


def test_old_termination_does_not_kill_confirmed_reemployment():
    rows = [event("old", "Previous employer payroll", "2024-10-15"),
            event("final", "Final employer payroll", "2024-11-15"),
            event("new", "New employer payroll", "2025-01-15", 2000)]
    stream, = project(rows)
    assert stream.description == "New employer payroll"
    assert all(occurrence.amount == 2000 for occurrence in stream.occurrences)


def test_restarted_description_carries_prior_terminal_provenance():
    rows = [event("old", "Payroll credit", "2024-10-15"),
            event("final", "Final employer payroll", "2024-11-15"),
            event("resumed", "Payroll credit", "2025-01-15", 2000)]
    stream, = project(rows)
    assert stream.provenance.terminal_event_ids == ("final",)
    assert stream.level == 2000


def test_next_confirmed_replaces_same_date_and_continues_monthly():
    rows = [event("normal", "Payroll credit", "2025-01-15", 1000),
            event("next", "Next confirmed salary", "2025-02-15", 1600, status="scheduled")]
    stream, = project(rows)
    assert dates(stream) == [d("2025-02-15"), d("2025-03-15"), d("2025-04-15")]
    assert [occurrence.amount for occurrence in stream.occurrences] == [1600, 1600, 1600]
    assert sum(o.amount for o in stream.occurrences if o.date.month == 2) == 1600
    assert stream.last_date == d("2025-02-15")
    assert stream.provenance.identity_event_ids == ("normal",)
    assert stream.provenance.level_event_id == "next"
    assert stream.provenance.anchor_event_id == "next"
    assert stream.provenance.scheduled_next_event_ids == ("next",)
    assert [o.scheduled_event_id for o in stream.occurrences] == ["next", None, None]


@pytest.mark.parametrize("next_date", ["2025-02-10", "2025-02-22", "2025-03-22"])
def test_next_confirmed_replaces_timing_even_when_payday_moves(next_date):
    rows = [event("normal", "Payroll credit", "2025-01-15"),
            event("next", "Next confirmed salary", next_date, 1700, status="scheduled")]
    stream, = project(rows)
    assert dates(stream)[0] == d(next_date)
    assert all(on_date.day == d(next_date).day for on_date in dates(stream))
    assert d("2025-02-15") not in dates(stream)


def test_next_confirmed_does_not_override_second_household_income():
    rows = [event("primary", "Primary household salary", "2025-01-15", 3000),
            event("second", "Second household income", "2025-01-20", 1500),
            event("next", "Next confirmed salary", "2025-02-15", 3500, status="scheduled")]
    primary, second = project(rows)
    assert primary.level == 3500
    assert second.level == 1500
    assert second.provenance.scheduled_next_event_ids == ()
    assert dates(second)[0] == d("2025-02-20")


def test_confirmation_without_core_history_establishes_salary():
    rows = [event("partial", "Prorated first salary", "2025-01-15", 400),
            event("next", "Next confirmed salary", "2025-02-15", 1700, status="scheduled")]
    stream, = project(rows)
    assert stream.level == 1700
    assert stream.provenance.source_event_ids == ("next",)
    assert [o.amount for o in stream.occurrences] == [1700, 1700, 1700]


def test_next_confirmation_cannot_revive_terminated_salary():
    rows = [event("normal", "Payroll credit", "2024-12-15"),
            event("final", "Final employer payroll", "2025-01-15"),
            event("next", "Next confirmed salary", "2025-02-15", status="scheduled")]
    assert project(rows) == []
    assert project(rows[1:]) == []
    assert income_dates(rows, d("2025-02-01")) == []


def test_prorated_and_bonus_never_establish_recurring_level():
    rows = [event("prorated", "Prorated first salary", "2024-11-15", 250),
            event("older", "Payroll credit", "2024-12-15", 1000),
            event("raise", "Payroll credit", "2025-01-15", 2000),
            event("arrears", "Promotion arrears payment", "2025-01-20", 6000),
            event("commission", "Monthly sales commission", "2025-01-23", 9000),
            event("late-prorated", "Prorated first salary", "2025-01-25", 100)]
    stream, = project(rows)
    assert stream.level == 2000
    assert stream.provenance.level_event_id == "raise"
    assert set(stream.provenance.source_event_ids) == {"older", "raise"}


@pytest.mark.parametrize("description", [
    "Prorated first salary", "Promotion arrears payment", "Quarterly performance bonus",
    "Prize proceeds", "Investment sale proceeds", "Employer expense reimbursement",
    "Performance commission", "Monthly sales commission", "Account commission payment",
    "Delivery platform payout", "Driver platform payout", "Weekly app earnings",
    "Task marketplace payout", "Website project payment", "Consulting invoice payment",
    "Freelance milestone payment", "Client retainer payment", "Design contract payment",
    "Content contract payment", "Application project payment", "Independent work payment",
])
def test_one_off_and_irregular_descriptions_never_recur(description):
    rows = [event("a", description, "2024-11-15"),
            event("b", description, "2024-12-15"),
            event("c", description, "2025-01-15"),
            event("future", description, "2025-02-15", status="scheduled")]
    assert project(rows) == []
    assert income_dates(rows, d("2025-02-01")) == []


def test_weekly_gig_commission_and_freelance_cannot_form_income():
    rows = [event(f"gig-{day}", "Weekly app earnings", f"2025-01-{day:02d}")
            for day in (3, 10, 17, 24, 31)]
    rows += [event("commission", "Performance commission", "2025-01-15"),
             event("freelance", "Freelance milestone payment", "2025-01-22")]
    assert project(rows) == []


def test_exact_fifteen_day_interleaving_keeps_both_streams():
    rows = [event("p-dec", "Primary household salary", "2024-12-01"),
            event("s-dec", "Second household income", "2024-12-16"),
            event("p-jan", "Primary household salary", "2025-01-01"),
            event("s-jan", "Second household income", "2025-01-16")]
    primary, second = project(rows, request="2025-01-20")
    assert dates(primary) == [d("2025-02-01"), d("2025-03-01"), d("2025-04-01")]
    assert dates(second) == [d("2025-02-16"), d("2025-03-16"), d("2025-04-16")]
    assert primary.cadence_days == second.cadence_days == 30


@pytest.mark.parametrize("year,feb_day", [(2024, 29), (2025, 28)])
def test_january_31_rolls_to_february_end_then_march_31(year, feb_day):
    rows = [event("jan", "Payroll credit", f"{year}-01-31")]
    stream, = project(rows, request=f"{year}-02-01", horizon=60)
    assert dates(stream) == [date(year, 2, feb_day), date(year, 3, 31)]
    assert stream.anchor_day == 31


@pytest.mark.parametrize("year,feb_day", [(2024, 29), (2025, 28)])
def test_latest_february_payroll_preserves_historical_day_31(year, feb_day):
    rows = [event("jan", "Payroll credit", f"{year}-01-31", 1000),
            event("feb", "Payroll credit", f"{year}-02-{feb_day}", 1500)]
    stream, = project(rows, request=f"{year}-03-01", horizon=60)
    assert dates(stream) == [date(year, 3, 31), date(year, 4, 30)]
    assert stream.level == 1500
    assert stream.provenance.anchor_event_id == "feb"
    assert stream.provenance.calendar_day_event_id == "jan"


def test_confirmed_new_anchor_does_not_inherit_old_day_31():
    rows = [event("jan", "Payroll credit", "2025-01-31"),
            event("next", "Next confirmed salary", "2025-02-28", status="scheduled")]
    stream, = project(rows, horizon=60)
    assert dates(stream) == [d("2025-02-28"), d("2025-03-28")]
    assert stream.anchor_day == 28
    assert stream.provenance.calendar_day_event_id == "next"


def test_calendar_month_crosses_year_boundary():
    stream, = project([event("dec", "Payroll credit", "2024-12-31")],
                      request="2025-01-01", horizon=90)
    assert dates(stream) == [d("2025-01-31"), d("2025-02-28"), d("2025-03-31")]


@pytest.fixture
def synthetic_fx(monkeypatch):
    # Isolate the existing helper's global tables and audit log from other tests.
    monkeypatch.setattr(fx, "_rate_table", {})
    monkeypatch.setattr(fx, "_conversion_log", [])
    fx.init_rates([
        ExchangeRate(d("2025-01-15"), "USD", "INR", 80),
        ExchangeRate(d("2025-02-15"), "USD", "INR", 81),
        ExchangeRate(d("2025-03-15"), "USD", "INR", 82),
        ExchangeRate(d("2025-04-15"), "USD", "INR", 83),
    ])


def test_foreign_salary_uses_existing_fx_on_each_occurrence_date(synthetic_fx):
    stream, = project([event("usd", "International employer payroll", "2025-01-15", 100)],
                      home_currency="INR")
    assert stream.level == 100 and stream.currency == "USD"
    assert [o.amount for o in stream.occurrences] == [8100, 8200, 8300]
    assert all(o.currency == "INR" and o.source_event_ids == ("usd",)
               for o in stream.occurrences)
    log = fx.get_conversion_log()
    assert [row["on_date"] for row in log] == ["2025-02-15", "2025-03-15", "2025-04-15"]
    assert all(row["from_ccy"] == "USD" and row["to_ccy"] == "INR" for row in log)


def test_foreign_next_confirmation_uses_authoritative_settlement_date(synthetic_fx):
    rows = [event("old", "International employer payroll", "2025-01-15", 100),
            event("next", "Next confirmed salary", "2025-02-10", 200,
                  status="scheduled", settlement_date=d("2025-02-15"))]
    stream, = project(rows, home_currency="INR")
    assert [o.amount for o in stream.occurrences] == [16200, 16400, 16600]
    assert fx.get_conversion_log()[0]["on_date"] == "2025-02-15"
    assert stream.provenance.level_event_id == "next"


def test_foreign_reverse_pair_uses_existing_fx(synthetic_fx):
    rows = [event("inr", "International employer payroll", "2025-01-15", 8100,
                  currency="INR")]
    stream, = project(rows, horizon=20, home_currency="USD")
    assert stream.occurrences[0].amount == 100
    assert fx.get_conversion_log()[0]["direction"] == "reverse"


def test_income_dates_are_sorted_unique_and_only_projected_income():
    rows = [event("primary", "Primary household salary", "2025-01-15", 2000),
            event("second", "Second household income", "2025-01-15", 1000),
            event("next", "Next confirmed salary", "2025-02-15", 2200, status="scheduled"),
            event("bonus", "Quarterly performance bonus", "2025-01-21", 4000),
            event("gig", "Weekly app earnings", "2025-01-28", 400)]
    actual = income_dates(iter(reversed(rows)), d("2025-02-01"))
    assert actual == [d("2025-02-15"), d("2025-03-15"), d("2025-04-15")]
    streams = project(rows)
    assert len(streams) == 2
    assert sum(o.amount for s in streams for o in s.occurrences if o.date == actual[0]) == 3200
    assert actual == sorted({o.date for s in streams for o in s.occurrences})


def test_income_dates_need_no_fx_rates_or_profile():
    rows = [event("foreign", "International employer payroll", "2025-01-15",
                  currency="EUR")]
    assert income_dates(rows, d("2025-02-01")) == [
        d("2025-02-15"), d("2025-03-15"), d("2025-04-15"),
    ]


@pytest.mark.parametrize("status", ["pending", "failed", "cancelled", "unrealized"])
def test_unconfirmed_income_neither_changes_level_nor_ends_employment(status):
    rows = [event("confirmed", "Payroll credit", "2024-12-15", 1000),
            event("unconfirmed", "Payroll credit", "2025-01-15", 8000, status=status),
            event("terminal", "Final employer payroll", "2025-01-20", status=status),
            event("next", "Next confirmed salary", "2025-02-15", 9000, status=status)]
    stream, = project(rows)
    assert stream.level == 1000
    assert stream.provenance.source_event_ids == ("confirmed",)


def test_future_settled_rows_cannot_leak_into_request_time_state():
    rows = [event("known", "Payroll credit", "2025-01-15", 1000),
            event("future", "Payroll credit", "2025-02-15", 9000),
            event("future-end", "Final employer payroll", "2025-02-20")]
    stream, = project(rows)
    assert stream.level == 1000
    assert stream.provenance.source_event_ids == ("known",)


def test_unknown_description_logged_and_never_projected(caplog):
    rows = [event("mystery", "Unrecognized payroll label", "2025-01-15"),
            event("known", "Payroll credit", "2025-01-15")]
    stream, = project(rows)
    assert stream.description == "Payroll credit"
    assert "Unrecognized payroll label" in caplog.text
    assert "mystery" in caplog.text
    assert "Unrecognized payroll label" not in SALARY_DESCRIPTION_CLASSES


@pytest.mark.parametrize("amount", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_current_level_cannot_revive_old_employer(amount, caplog):
    rows = [event("old", "Previous employer payroll", "2024-12-15", 1000),
            event("current", "New employer payroll", "2025-01-15", amount)]
    assert project(rows) == []
    assert "current" in caplog.text and "non-projectable" in caplog.text


def test_unknown_terminal_amount_still_ends_employment():
    rows = [event("old", "Payroll credit", "2024-12-15"),
            event("final", "Final employer payroll", "2025-01-15", None)]
    assert project(rows) == []


def test_missing_settlement_date_is_not_replaced_with_event_date(caplog):
    rows = [event("undated", "Payroll credit", "2025-01-15", settlement_date=None)]
    assert project(rows) == []
    assert "no settlement date" in caplog.text


def test_anchor_uses_settlement_not_event_date():
    rows = [event("pay", "Payroll credit", "2025-01-10", settlement_date=d("2025-01-20"))]
    stream, = project(rows)
    assert stream.last_date == d("2025-01-20")
    assert dates(stream) == [d("2025-02-20"), d("2025-03-20"), d("2025-04-20")]


def test_empty_non_salary_and_non_credit_inputs():
    assert project([]) == []
    assert income_dates([], d("2025-02-01")) == []
    rows = [event("debit", "Payroll credit", "2025-01-15", direction="debit"),
            event("other", "Payroll credit", "2025-01-15", category="refund")]
    assert project(rows) == []


def test_forecast_window_excludes_today_and_includes_end():
    rows = [event("jan", "Payroll credit", "2025-01-15")]
    assert income_dates(rows, d("2025-02-01"), 13) == []
    assert income_dates(rows, d("2025-02-01"), 14) == [d("2025-02-15")]
    assert income_dates(rows, d("2025-02-15"), 0) == []
    assert income_dates(rows, d("2025-02-15"), 28) == [d("2025-03-15")]
    assert project(rows, horizon=0) == []


def test_override_outside_horizon_suppresses_earlier_assumed_payday():
    rows = [event("jan", "Payroll credit", "2025-01-15"),
            event("next", "Next confirmed salary", "2025-03-15", status="scheduled")]
    assert project(rows, horizon=28) == []
    assert income_dates(rows, d("2025-02-01"), 28) == []


@pytest.mark.parametrize("horizon", [-1, 2.5, True])
def test_invalid_horizon_rejected(horizon):
    with pytest.raises(ValueError, match="horizon"):
        project([], horizon=horizon)
    with pytest.raises(ValueError, match="horizon"):
        income_dates([], d("2025-02-01"), horizon)


def test_mixed_users_rejected():
    rows = [event("one", "Payroll credit", "2025-01-15"),
            event("two", "Payroll credit", "2025-01-15", user_id="another-household")]
    with pytest.raises(ValueError, match="one user"):
        project(rows)


def test_home_currency_must_be_explicit():
    with pytest.raises(ValueError, match="home_currency"):
        project([], home_currency="")


def test_permutation_invariance_and_no_input_mutation():
    rows = [event("old", "Previous employer payroll", "2024-12-15", 1000),
            event("new", "New employer payroll", "2025-01-15", 2000),
            event("second", "Second household income", "2025-01-20", 1500),
            event("next", "Next confirmed salary", "2025-02-15", 2200, status="scheduled")]
    original = copy.deepcopy(rows)
    expected = project(rows)
    expected_dates = income_dates(rows, d("2025-02-01"))
    for ordering in permutations(rows):
        assert project(iter(ordering)) == expected
        assert income_dates(ordering, d("2025-02-01")) == expected_dates
    assert rows == original


def test_every_occurrence_has_identity_amount_date_and_transition_provenance():
    rows = [event("old", "Previous employer payroll", "2024-12-15"),
            event("new", "New employer payroll", "2025-01-15"),
            event("next", "Next confirmed salary", "2025-02-15", 1200, status="scheduled")]
    stream, = project(rows)
    provenance = stream.provenance
    assert provenance.identity_event_ids == ("new",)
    assert provenance.level_event_id == provenance.anchor_event_id == "next"
    assert provenance.transition_event_ids == ("new", "old")
    assert provenance.scheduled_next_event_ids == ("next",)
    assert provenance.source_event_ids == ("new", "next", "old")
    assert all(o.source_event_ids == provenance.source_event_ids for o in stream.occurrences)


def test_classification_table_has_all_required_classes():
    expected_counts = {
        SalaryClass.CORE_RECURRING: 13, SalaryClass.TERMINAL: 1,
        SalaryClass.ONE_OFF: 9, SalaryClass.IRREGULAR: 12, SalaryClass.SCHEDULED_NEXT: 1,
    }
    assert len(SALARY_DESCRIPTION_CLASSES) == 36
    for kind, count in expected_counts.items():
        assert list(SALARY_DESCRIPTION_CLASSES.values()).count(kind) == count
