"""Output contract validator for Buy or Wait? (Step 1).

validate_rows(...) checks a candidate set of output rows against the rules in
AGENTS.md Section 6.2 / BUILD-PLAN.md Step 1. It collects every violation it
finds (so a single run reports the full picture) and RAISES ValidationError
if the list is non-empty; on success it returns an empty list.

This module intentionally contains no forecasting, ledger, or decision logic.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pandas as pd

from code.config import (
    ALLOWED_AFFORDABILITY_STATUS,
    ALLOWED_RECOMMENDED_PAYMENT_METHOD,
    OUTPUT_COLUMNS,
)

AMOUNT_TOLERANCE = 0.01

PAYMENT_PLAN_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}:\d+(\.\d{2})?(\|\d{4}-\d{2}-\d{2}:\d+(\.\d{2})?)*$"
)
STOP_RE = re.compile(r"^stop:(?P<event_id>[^:|]+)$")
REDUCE_RE = re.compile(r"^reduce_to:(?P<event_id>[^:|]+):(?P<amount>-?\d+(?:\.\d+)?)$")


class ValidationError(Exception):
    """Raised by validate_rows when one or more contract violations exist."""

    def __init__(self, violations):
        self.violations = list(violations)
        super().__init__("\n".join(self.violations))


def _parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _split_pipe(value):
    if value is None or pd.isna(value):
        return []
    return [v for v in str(value).split("|") if v]


def _parse_payment_plan(plan_str, violations, request_id):
    """Return a list of (date, amount) tuples, or None for 'none' / bad input."""
    if plan_str == "none":
        return None
    if not isinstance(plan_str, str) or not PAYMENT_PLAN_RE.match(plan_str):
        violations.append(
            f"{request_id}: payment_plan does not match the required format: {plan_str!r}"
        )
        return None
    entries = []
    for part in plan_str.split("|"):
        d_str, amt_str = part.split(":", 1)
        try:
            d = _parse_date(d_str)
        except ValueError:
            violations.append(f"{request_id}: payment_plan has an invalid date {d_str!r}")
            return None
        entries.append((d, float(amt_str)))
    dates = [d for d, _ in entries]
    if dates != sorted(dates):
        violations.append(
            f"{request_id}: payment_plan dates are not non-decreasing: {plan_str!r}"
        )
    return entries


def _parse_spending_changes(s, request_id, user_id, events_by_id, profile, violations):
    if s == "none":
        return []
    if not isinstance(s, str):
        violations.append(f"{request_id}: spending_changes_needed is not a string: {s!r}")
        return []

    parts = s.split("|")
    if len(parts) > 3:
        violations.append(
            f"{request_id}: spending_changes_needed has more than 3 entries: {s!r}"
        )

    protect = set(_split_pipe(profile.get("expense_categories_to_protect")))
    willing_reduce = set(_split_pipe(profile.get("expense_categories_user_is_willing_to_reduce")))
    willing_stop = set(_split_pipe(profile.get("expense_categories_user_is_willing_to_stop")))

    seen_event_ids = set()
    changes = []
    for part in parts:
        m_stop = STOP_RE.match(part)
        m_reduce = REDUCE_RE.match(part)
        if m_stop:
            kind, event_id, amount = "stop", m_stop.group("event_id"), None
        elif m_reduce:
            kind, event_id, amount = "reduce_to", m_reduce.group("event_id"), float(m_reduce.group("amount"))
        else:
            violations.append(f"{request_id}: invalid spending_changes_needed syntax: {part!r}")
            continue

        if event_id in seen_event_ids:
            violations.append(
                f"{request_id}: duplicate event_id in spending_changes_needed: {event_id!r}"
            )
            continue
        seen_event_ids.add(event_id)

        event = events_by_id.get(event_id)
        if event is None:
            violations.append(
                f"{request_id}: spending_changes_needed references unknown event_id {event_id!r}"
            )
            continue
        if event.get("user_id") != user_id:
            violations.append(
                f"{request_id}: spending_changes_needed event {event_id!r} does not belong to user {user_id!r}"
            )
            continue

        flexibility = event.get("flexibility")
        category = event.get("category")

        if pd.isna(flexibility) or flexibility == "fixed":
            violations.append(
                f"{request_id}: spending change on non-flexible event {event_id!r} (flexibility={flexibility!r})"
            )
            continue

        if category in protect:
            violations.append(
                f"{request_id}: spending change on protected category {category!r} (event {event_id!r})"
            )
            continue

        if kind == "stop":
            if flexibility not in ("stoppable", "reducible_or_stoppable"):
                violations.append(
                    f"{request_id}: stop not legal for event {event_id!r} with flexibility {flexibility!r}"
                )
                continue
            if category not in willing_stop:
                violations.append(
                    f"{request_id}: stop category {category!r} not in the user's willing-to-stop list (event {event_id!r})"
                )
                continue
        else:
            if flexibility not in ("reducible", "reducible_or_stoppable"):
                violations.append(
                    f"{request_id}: reduce_to not legal for event {event_id!r} with flexibility {flexibility!r}"
                )
                continue
            if category not in willing_reduce:
                violations.append(
                    f"{request_id}: reduce_to category {category!r} not in the user's willing-to-reduce list (event {event_id!r})"
                )
                continue
            min_allowed = event.get("minimum_allowed_amount")
            if not pd.isna(min_allowed) and amount < float(min_allowed) - AMOUNT_TOLERANCE:
                violations.append(
                    f"{request_id}: reduce_to amount {amount} is below minimum_allowed_amount "
                    f"{min_allowed} for event {event_id!r}"
                )
                continue

        changes.append((kind, event_id, amount))
    return changes


def validate_rows(rows, requests_df, options_df, profiles_df, events_df):
    violations: list[str] = []

    requests_by_id = requests_df.set_index("request_id").to_dict("index")
    profiles_by_user = profiles_df.set_index("user_id").to_dict("index")
    events_by_id = events_df.set_index("event_id").to_dict("index")

    options_by_request: dict[str, list[dict]] = {}
    for rec in options_df.to_dict("records"):
        options_by_request.setdefault(rec["request_id"], []).append(rec)

    expected_order = list(requests_df["request_id"])
    actual_order = [row.get("request_id") for row in rows]

    if actual_order != expected_order:
        missing = set(expected_order) - set(actual_order)
        extra = set(actual_order) - set(expected_order)
        dupes = {rid for rid in actual_order if actual_order.count(rid) > 1}
        if missing:
            violations.append(f"missing request_id(s) in output: {sorted(missing)}")
        if extra:
            violations.append(f"unexpected request_id(s) in output: {sorted(extra)}")
        if dupes:
            violations.append(f"duplicate request_id(s) in output: {sorted(dupes)}")
        if not missing and not extra and not dupes:
            violations.append("output row order does not match dataset/requests.csv order")

    for row in rows:
        request_id = row.get("request_id")

        if list(row.keys()) != OUTPUT_COLUMNS:
            violations.append(
                f"{request_id}: row does not have the exact 8-column schema/order: {list(row.keys())}"
            )
            continue

        request = requests_by_id.get(request_id)
        if request is None:
            violations.append(f"{request_id}: not found in dataset/requests.csv")
            continue

        user_id = request["user_id"]
        profile = profiles_by_user.get(user_id)
        if profile is None:
            violations.append(f"{request_id}: user {user_id!r} not found in financial_profiles.csv")
            continue

        accepted_methods = set(_split_pipe(profile.get("payment_methods_user_will_consider")))

        requested_amount = float(request["requested_amount"])
        request_date = _parse_date(str(request["request_date"]))
        desired_completion_date = _parse_date(str(request["desired_completion_date"]))
        allows_partial_payment = bool(request["allows_partial_payment"])

        amount_safe_to_pay = row["amount_safe_to_pay"]
        affordability_status = row["affordability_status"]
        recommended_payment_method = row["recommended_payment_method"]
        payment_plan_str = row["payment_plan"]
        earliest_str = row["earliest_date_for_full_payment"]
        spending_changes_str = row["spending_changes_needed"]
        decision_explanation = row["decision_explanation"]

        amt = None
        try:
            amt = float(amount_safe_to_pay)
        except (TypeError, ValueError):
            violations.append(f"{request_id}: amount_safe_to_pay is not numeric: {amount_safe_to_pay!r}")
        if amt is not None and not (-AMOUNT_TOLERANCE <= amt <= requested_amount + AMOUNT_TOLERANCE):
            violations.append(
                f"{request_id}: amount_safe_to_pay {amt} out of range [0, {requested_amount}]"
            )

        if affordability_status not in ALLOWED_AFFORDABILITY_STATUS:
            violations.append(f"{request_id}: invalid affordability_status {affordability_status!r}")
        if recommended_payment_method not in ALLOWED_RECOMMENDED_PAYMENT_METHOD:
            violations.append(
                f"{request_id}: invalid recommended_payment_method {recommended_payment_method!r}"
            )

        plan_entries = _parse_payment_plan(payment_plan_str, violations, request_id)

        earliest_date = None
        if earliest_str not in (None, ""):
            try:
                earliest_date = _parse_date(str(earliest_str))
            except ValueError:
                violations.append(
                    f"{request_id}: earliest_date_for_full_payment is not a valid date: {earliest_str!r}"
                )

        if affordability_status == "affordable_now" and earliest_date != request_date:
            violations.append(
                f"{request_id}: affordable_now requires earliest_date_for_full_payment == request_date"
            )

        if affordability_status == "not_affordable":
            if payment_plan_str != "none":
                violations.append(f"{request_id}: not_affordable requires payment_plan == 'none'")
            if earliest_str not in (None, ""):
                violations.append(
                    f"{request_id}: not_affordable requires an empty earliest_date_for_full_payment"
                )

        if recommended_payment_method in ("full_payment", "partial_payment", "installments"):
            if recommended_payment_method not in accepted_methods:
                violations.append(
                    f"{request_id}: recommended_payment_method {recommended_payment_method!r} "
                    f"not in the user's accepted methods {sorted(accepted_methods)}"
                )

        if recommended_payment_method == "wait" and "full_payment" not in accepted_methods:
            violations.append(f"{request_id}: wait requires the user to accept full_payment")

        if recommended_payment_method == "partial_payment":
            if affordability_status != "affordable_with_plan":
                violations.append(
                    f"{request_id}: partial_payment must have affordability_status == affordable_with_plan"
                )
            if plan_entries is None or len(plan_entries) != 2:
                violations.append(f"{request_id}: partial_payment requires exactly 2 payments")
            else:
                (d1, a1), (d2, a2) = plan_entries
                if d1 != request_date:
                    violations.append(f"{request_id}: partial_payment first payment date must equal request_date")
                if earliest_date is not None and d2 != earliest_date:
                    violations.append(
                        f"{request_id}: partial_payment second payment date must equal earliest_date_for_full_payment"
                    )
                if d2 > desired_completion_date:
                    violations.append(
                        f"{request_id}: partial_payment second payment date is after desired_completion_date"
                    )
                if abs((a1 + a2) - requested_amount) > AMOUNT_TOLERANCE:
                    violations.append(f"{request_id}: partial_payment payments do not sum to requested_amount")
                if amt is not None and abs(a1 - amt) > AMOUNT_TOLERANCE:
                    violations.append(f"{request_id}: partial_payment first payment must equal amount_safe_to_pay")
            if not allows_partial_payment:
                violations.append(
                    f"{request_id}: request does not allow partial payment (allows_partial_payment=False)"
                )
            if "partial_payment" not in accepted_methods:
                violations.append(f"{request_id}: user does not accept partial_payment")

        if recommended_payment_method == "installments":
            options = options_by_request.get(request_id, [])
            installment_options = [o for o in options if o.get("payment_method") == "installments"]
            max_months = profile.get("max_installment_months")
            matched = False
            if plan_entries:
                for opt in installment_options:
                    n = int(opt["number_of_payments"])
                    if len(plan_entries) != n:
                        continue
                    amt_per = float(opt["payment_amount"])
                    first_date = _parse_date(str(opt["first_payment_date"]))
                    freq_raw = opt.get("payment_frequency_days")
                    freq = int(freq_raw) if not pd.isna(freq_raw) else 0
                    expected_dates = [first_date + timedelta(days=freq * k) for k in range(n)]
                    dates_match = all(d == ed for (d, _a), ed in zip(plan_entries, expected_dates))
                    amounts_match = all(abs(a - amt_per) <= AMOUNT_TOLERANCE for _d, a in plan_entries)
                    if dates_match and amounts_match:
                        if pd.isna(max_months) or n > float(max_months):
                            violations.append(
                                f"{request_id}: installments plan exceeds the user's "
                                f"max_installment_months={max_months}"
                            )
                        else:
                            matched = True
                        break
            if not matched:
                violations.append(
                    f"{request_id}: installments plan does not exactly reproduce a "
                    "request_payment_options.csv row"
                )

        changes = _parse_spending_changes(
            spending_changes_str, request_id, user_id, events_by_id, profile, violations
        )

        if not isinstance(decision_explanation, str) or not decision_explanation.strip():
            violations.append(f"{request_id}: decision_explanation is empty")
        elif "\n" in decision_explanation:
            violations.append(f"{request_id}: decision_explanation contains a newline")

    if violations:
        raise ValidationError(violations)
    return violations
