"""Deterministic explanations rendered exclusively from auditable facts."""
from __future__ import annotations

from datetime import date
from typing import TypedDict

from code.config import HORIZON_DAYS
from code.domain.models import CandidatePlan, RequestCase


class ExplanationFacts(TypedDict):
    currency: str
    requested_amount: float
    minimum: float
    horizon_days: int
    request_date: date
    deadline: date
    method: str
    schedule: tuple[tuple[date, float], ...]
    payment_count: int
    total_payable: float
    spending_changes: tuple[str, ...]
    forecast_complete: bool


def build_facts(
    case: RequestCase, plan: CandidatePlan | None, *, forecast_complete: bool = True,
) -> ExplanationFacts:
    return {
        "currency": case.profile.home_currency,
        "requested_amount": case.request.requested_amount,
        "minimum": case.profile.minimum_balance_to_keep,
        "horizon_days": HORIZON_DAYS,
        "request_date": case.request.request_date,
        "deadline": case.request.desired_completion_date,
        "method": plan.method if plan else "not_recommended",
        "schedule": tuple(plan.schedule) if plan else (),
        "payment_count": len(plan.schedule) if plan else 0,
        "total_payable": plan.total_payable if plan else 0.0,
        "spending_changes": tuple(plan.spending_changes or ()) if plan else (),
        "forecast_complete": forecast_complete,
    }


def _money(amount: float) -> str:
    return f"{amount:,.2f}".removesuffix(".00")


def explain(facts: ExplanationFacts) -> str:
    """All amounts, dates, counts and the horizon come from the fact dictionary."""
    currency = facts["currency"]
    minimum = _money(facts["minimum"])
    if not facts["forecast_complete"]:
        return (f"Defer this payment by {facts['deadline']}. "
                f"Unresolved cash-flow details prevent verification of the {currency} {minimum} minimum.")
    method = facts["method"]
    if method == "not_recommended":
        return (f"Do not make this payment by {facts['deadline']}. "
                f"No eligible option keeps the {currency} {minimum} minimum protected.")
    first_date, first_amount = facts["schedule"][0]
    amount = _money(first_amount)
    guarantee = (f"This leaves at least {currency} {minimum} available "
                 f"over the next {facts['horizon_days']} days.")
    if method == "full_payment":
        action = f"Pay {currency} {amount} today."
    elif method == "wait":
        action = f"Pay {currency} {amount} in full on {first_date}."
        if not facts["spending_changes"]:
            guarantee = (f"Paying earlier would take the balance below the "
                         f"{currency} {minimum} minimum.")
    elif method == "installments":
        action = (f"Use {facts['payment_count']} installments of {currency} {amount}, "
                  f"starting {first_date}.")
    elif method == "partial_payment":
        last_date, remaining = facts["schedule"][1]
        action = (f"Pay {currency} {amount} today and the remaining {currency} "
                  f"{_money(remaining)} on {last_date}.")
        guarantee = (f"This completes the full request and keeps the "
                     f"{currency} {minimum} minimum protected.")
    else:
        raise ValueError(f"Unknown explanation method: {method}")
    if facts["spending_changes"]:
        action = "With the listed spending changes, " + action[0].lower() + action[1:]
    return f"{action} {guarantee}"
