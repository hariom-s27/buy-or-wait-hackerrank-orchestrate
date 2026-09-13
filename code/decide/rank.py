"""Official ordering of safe plans and their output status mapping."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from code.domain.models import CandidatePlan


def plan_sort_key(plan: CandidatePlan) -> tuple:
    """Deadline is a filter, not a preference for finishing sooner."""
    return (
        bool(plan.spending_changes),
        plan.total_payable,
        min(on_date for on_date, _ in plan.schedule),
        len(plan.schedule),
        plan.payment_option_id or "",
    )


def rank_plans(plans: Iterable[CandidatePlan], deadline: date) -> list[CandidatePlan]:
    """Return a new ordered list; do not mutate candidates or their schedules."""
    return sorted((plan for plan in plans if plan.is_safe and plan.schedule
                   and max(on_date for on_date, _ in plan.schedule) <= deadline),
                  key=plan_sort_key)


def select_plan(plans: Iterable[CandidatePlan], deadline: date) -> CandidatePlan | None:
    ranked = rank_plans(plans, deadline)
    return ranked[0] if ranked else None


def affordability_status(plan: CandidatePlan | None, request_date: date) -> str:
    if plan is None:
        return "not_affordable"
    if not plan.is_safe:
        raise ValueError("Cannot recommend an unsafe plan")
    if plan.spending_changes or plan.method in {"partial_payment", "installments"}:
        return "affordable_with_plan"
    if plan.method == "full_payment" and plan.schedule[0][0] == request_date:
        return "affordable_now"
    if plan.method == "wait":
        return "affordable_later"
    raise ValueError(f"Unexpected winning plan: {plan.method}")
