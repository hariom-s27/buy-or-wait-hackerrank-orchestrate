"""Step 1 validator tests: prove validate_rows has teeth.

Uses small synthetic fixtures (no real request_ids/user_ids from dataset/)
so these tests are independent of the real data and of any forecasting logic.
"""
from __future__ import annotations

import copy

import pandas as pd
import pytest

from code.output.validator import ValidationError, validate_rows
from code.output.writer import fmt_safe


REQUEST_ID = "request_test_01"
USER_ID = "user_test_01"


def _fixtures():
    requests_df = pd.DataFrame(
        [
            {
                "request_id": REQUEST_ID,
                "user_id": USER_ID,
                "request_date": "2025-01-01",
                "request_type": "purchase",
                "requested_amount": 1000.0,
                "desired_completion_date": "2025-02-01",
                "allows_partial_payment": True,
                "request_text": "test fixture request",
            }
        ]
    )

    profiles_df = pd.DataFrame(
        [
            {
                "user_id": USER_ID,
                "home_currency": "USD",
                "current_available_balance": 5000.0,
                "minimum_balance_to_keep": 100.0,
                "financial_priorities": "groceries",
                "expense_categories_to_protect": "rent",
                "expense_categories_user_is_willing_to_reduce": "groceries",
                "expense_categories_user_is_willing_to_stop": "dining",
                "payment_methods_user_will_consider": "full_payment|partial_payment|installments",
                "max_installment_months": 6,
            }
        ]
    )

    events_df = pd.DataFrame(
        [
            {
                "event_id": "event_rent",
                "user_id": USER_ID,
                "event_type": "expense",
                "description": "Rent",
                "category": "rent",
                "direction": "debit",
                "amount": 500.0,
                "currency": "USD",
                "event_date": "2024-12-01",
                "settlement_date": "2024-12-01",
                "status": "settled",
                "linked_event_id": None,
                "flexibility": "fixed",
                "minimum_allowed_amount": None,
            },
            {
                "event_id": "event_groceries",
                "user_id": USER_ID,
                "event_type": "expense",
                "description": "Groceries",
                "category": "groceries",
                "direction": "debit",
                "amount": 200.0,
                "currency": "USD",
                "event_date": "2024-12-01",
                "settlement_date": "2024-12-01",
                "status": "settled",
                "linked_event_id": None,
                "flexibility": "reducible",
                "minimum_allowed_amount": 50.0,
            },
            {
                "event_id": "event_dining",
                "user_id": USER_ID,
                "event_type": "expense",
                "description": "Dining out",
                "category": "dining",
                "direction": "debit",
                "amount": 80.0,
                "currency": "USD",
                "event_date": "2024-12-01",
                "settlement_date": "2024-12-01",
                "status": "settled",
                "linked_event_id": None,
                "flexibility": "stoppable",
                "minimum_allowed_amount": None,
            },
            {
                # Flexible (so it clears the flexibility gate) but its
                # category is protected -- isolates the protected-category
                # rejection from the separate "not flexible" rejection.
                "event_id": "event_protected_flex",
                "user_id": USER_ID,
                "event_type": "expense",
                "description": "Rent (hypothetically stoppable)",
                "category": "rent",
                "direction": "debit",
                "amount": 500.0,
                "currency": "USD",
                "event_date": "2024-12-01",
                "settlement_date": "2024-12-01",
                "status": "settled",
                "linked_event_id": None,
                "flexibility": "stoppable",
                "minimum_allowed_amount": None,
            },
        ]
    )

    options_df = pd.DataFrame(
        [
            {
                "payment_option_id": "payment_option_test_01",
                "request_id": REQUEST_ID,
                "payment_method": "installments",
                "payment_amount": 340.0,
                "number_of_payments": 3,
                "first_payment_date": "2025-01-01",
                "payment_frequency_days": 30,
                "financing_fee": 20.0,
                "total_payable_amount": 1020.0,
            }
        ]
    )

    return requests_df, options_df, profiles_df, events_df


def _base_row():
    """A legitimate 'wait' row that should pass validation cleanly."""
    return {
        "request_id": REQUEST_ID,
        "amount_safe_to_pay": fmt_safe(500),
        "affordability_status": "affordable_later",
        "recommended_payment_method": "wait",
        "payment_plan": "2025-01-15:1000",
        "earliest_date_for_full_payment": "2025-01-15",
        "spending_changes_needed": "none",
        "decision_explanation": "Wait until 2025-01-15 to pay in full.",
    }


def _run(row):
    requests_df, options_df, profiles_df, events_df = _fixtures()
    return validate_rows([row], requests_df, options_df, profiles_df, events_df)


def test_valid_row_passes():
    assert _run(_base_row()) == []


def test_rejects_invalid_enum():
    row = copy.deepcopy(_base_row())
    row["affordability_status"] = "definitely_not_a_real_status"
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("invalid affordability_status" in v for v in exc_info.value.violations)


def test_rejects_out_of_range_amount():
    row = copy.deepcopy(_base_row())
    row["amount_safe_to_pay"] = fmt_safe(1500)  # requested_amount is only 1000
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("out of range" in v for v in exc_info.value.violations)


def test_rejects_three_payment_partial_plan():
    row = copy.deepcopy(_base_row())
    row["recommended_payment_method"] = "partial_payment"
    row["affordability_status"] = "affordable_with_plan"
    row["payment_plan"] = "2025-01-01:300|2025-01-10:300|2025-01-20:400"
    row["earliest_date_for_full_payment"] = "2025-01-20"
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("exactly 2 payments" in v for v in exc_info.value.violations)


def test_rejects_fabricated_installment_schedule():
    row = copy.deepcopy(_base_row())
    row["recommended_payment_method"] = "installments"
    row["affordability_status"] = "affordable_with_plan"
    # Real option is 3 x 340.00 starting 2025-01-01 every 30 days -- this
    # schedule matches no row of request_payment_options.csv.
    row["payment_plan"] = "2025-01-01:999|2025-01-31:999|2025-03-02:999"
    row["earliest_date_for_full_payment"] = "2025-03-02"
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("does not exactly reproduce" in v for v in exc_info.value.violations)


def test_rejects_spending_change_on_protected_category():
    row = copy.deepcopy(_base_row())
    row["spending_changes_needed"] = "stop:event_protected_flex"  # category 'rent' is protected
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("protected category" in v for v in exc_info.value.violations)


def test_rejects_duplicate_event_id_in_spending_changes():
    row = copy.deepcopy(_base_row())
    row["spending_changes_needed"] = "stop:event_dining|reduce_to:event_dining:10"
    with pytest.raises(ValidationError) as exc_info:
        _run(row)
    assert any("duplicate event_id" in v for v in exc_info.value.violations)
