"""Enumerate eligible, safe schedules without altering supplied payment options."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal

from code.domain.models import CandidatePlan, RequestCase
from code.forecast.ledger import trough
from code.output.writer import fmt_plan


def payment_debits(schedule: Sequence[tuple[date, float]]) -> dict[date, float]:
    """Net payment debits by date without losing payments sharing a date."""
    daily = defaultdict(list)
    for on_date, amount in schedule:
        daily[on_date].append(amount)
    return {on_date: math.fsum(amounts) for on_date, amounts in daily.items()}


def schedule_is_safe(
    case: RequestCase, ledger: Mapping[date, float],
    schedule: Sequence[tuple[date, float]],
) -> bool:
    """Check the complete original forecast window for every kind of plan."""
    request, profile = case.request, case.profile
    if not schedule or any(
        on_date not in ledger
        or not request.request_date <= on_date <= request.desired_completion_date
        or not math.isfinite(amount) or amount <= 0
        for on_date, amount in schedule
    ):
        return False
    return trough(
        ledger, profile.current_available_balance, request.request_date,
        extra_payments=payment_debits(schedule),
    ) >= profile.minimum_balance_to_keep


def enumerate_plans(
    case: RequestCase, ledger: Mapping[date, float], amount_safe_to_pay: float,
    earliest_date: date | None,
    spending_changes: Sequence[str] | None = None,
) -> list[CandidatePlan]:
    """Return safe candidate plans, attaching spending changes when provided."""
    request, profile = case.request, case.profile
    accepted = set(profile.payment_methods_user_will_consider)
    result = []

    def consider(method, schedule, total, option_id=None):
        if schedule_is_safe(case, ledger, schedule):
            result.append(CandidatePlan(
                method=method, payment_option_id=option_id, schedule=schedule,
                total_payable=total, is_safe=True,
                spending_changes=list(spending_changes) if spending_changes else None,
            ))

    requested = request.requested_amount
    today = request.request_date
    full_amount = float(fmt_plan(requested))
    if "full_payment" in accepted:
        if earliest_date == today or spending_changes:
            consider("full_payment", [(today, full_amount)], requested)
        if earliest_date is not None and today < earliest_date <= request.desired_completion_date:
            consider("wait", [(earliest_date, full_amount)], requested)

    if (request.allows_partial_payment and "partial_payment" in accepted
            and 0 < amount_safe_to_pay < requested and earliest_date is not None
            and today < earliest_date <= request.desired_completion_date):
        # Check the cents that will actually be serialized, including any upward
        # rounding of capacity. Never grant a safety tolerance for that rounding.
        first = Decimal(fmt_plan(amount_safe_to_pay))
        remaining = Decimal(fmt_plan(requested)) - first
        if first > 0 and remaining > 0:
            consider("partial_payment", [
                (today, float(first)), (earliest_date, float(remaining)),
            ], requested)

    if "installments" in accepted and profile.max_installment_months is not None:
        for option in sorted(case.payment_options, key=lambda row: row.payment_option_id):
            if option.request_id != request.request_id or option.payment_method != "installments":
                continue
            count, gap = option.number_of_payments, option.payment_frequency_days
            if count < 1 or count > profile.max_installment_months:
                continue
            if count > 1 and (gap is None or gap <= 0):
                continue
            schedule = [
                (option.first_payment_date + timedelta(days=k * (gap or 0)), option.payment_amount)
                for k in range(count)
            ]
            # A supplied schedule must also survive serialization unchanged.
            if option.payment_amount != float(fmt_plan(option.payment_amount)):
                continue
            consider("installments", schedule, option.total_payable_amount, option.payment_option_id)
    return result
