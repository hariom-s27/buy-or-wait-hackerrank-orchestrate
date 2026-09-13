"""Exhaustive search over legal spending-change subsets (Step 8)."""
from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from code.decide.plans import enumerate_plans
from code.decide.rank import plan_sort_key, select_plan
from code.domain.models import CandidatePlan, RequestCase
from code.forecast.capacity import amount_safe_to_pay, earliest_date_for_full_payment
from code.forecast.ledger import LedgerTrace, build_ledger_trace
from code.output.writer import fmt_plan
from code.reconstruct.streams import ExpenseStream, reconstruct_expense_streams


@dataclass(frozen=True)
class SpendingChange:
    """A single proposed change to an expense stream."""
    kind: str  # 'stop' or 'reduce_to'
    event_id: str
    target_amount: float | None
    operation_str: str
    category: str


def eligible_changes(
    case: Any,
    streams: Sequence[ExpenseStream] | None = None,
) -> list[SpendingChange]:
    """Return every legally permitted single spending change for this case.

    A STOP operation is legal only when:
      - stream flexibility is 'stoppable' or 'reducible_or_stoppable'
      - stream category is in profile.expense_categories_user_is_willing_to_stop
      - category is NOT in profile.expense_categories_to_protect
      - the representative event_id belongs to this user

    A REDUCE operation is legal only when:
      - stream flexibility is 'reducible' or 'reducible_or_stoppable'
      - stream category is in profile.expense_categories_user_is_willing_to_reduce
      - minimum_allowed_amount is present and finite (>= 0)
      - category is NOT in profile.expense_categories_to_protect
      - representative event_id belongs to this user
    """
    profile = case.profile
    events = case.events
    req_date = case.request.request_date if hasattr(case, "request") else date.today()

    if streams is None:
        streams = reconstruct_expense_streams(events, req_date)

    protect = set(profile.expense_categories_to_protect or ())
    willing_stop = set(profile.expense_categories_user_is_willing_to_stop or ())
    willing_reduce = set(profile.expense_categories_user_is_willing_to_reduce or ())

    user_events_by_id = {e.event_id: e for e in events if e.user_id == profile.user_id}

    results: list[SpendingChange] = []
    for stream in streams:
        category = stream.category
        if category in protect:
            continue

        rep_id = stream.representative_event_id
        if rep_id not in user_events_by_id:
            continue

        rep_event = user_events_by_id[rep_id]
        flex = stream.flexibility or rep_event.flexibility
        if not flex or flex == "fixed":
            continue

        # Check STOP eligibility
        if flex in ("stoppable", "reducible_or_stoppable") and category in willing_stop:
            results.append(SpendingChange(
                kind="stop",
                event_id=rep_id,
                target_amount=None,
                operation_str=f"stop:{rep_id}",
                category=category,
            ))

        # Check REDUCE eligibility
        if flex in ("reducible", "reducible_or_stoppable") and category in willing_reduce:
            min_amt = (
                stream.minimum_allowed_amount
                if stream.minimum_allowed_amount is not None
                else rep_event.minimum_allowed_amount
            )
            if min_amt is not None and math.isfinite(min_amt) and min_amt >= 0:
                formatted_min = fmt_plan(min_amt)
                reduced_val = float(formatted_min)
                if reduced_val <= stream.level:
                    results.append(SpendingChange(
                        kind="reduce_to",
                        event_id=rep_id,
                        target_amount=reduced_val,
                        operation_str=f"reduce_to:{rep_id}:{formatted_min}",
                        category=category,
                    ))

    # Sort deterministically
    return sorted(results, key=lambda c: (c.category, c.event_id, c.kind))


def enumerate_change_subsets(
    eligible: Sequence[SpendingChange],
    max_size: int = 3,
) -> list[tuple[SpendingChange, ...]]:
    """Generate all legal subsets of size 1, 2, ..., max_size.

    Excludes any subset containing multiple changes for the same event_id
    (e.g., stop and reduce for the same event are mutually exclusive).
    """
    subsets: list[tuple[SpendingChange, ...]] = []
    for k in range(1, min(max_size, len(eligible)) + 1):
        for combo in itertools.combinations(eligible, k):
            event_ids = [c.event_id for c in combo]
            if len(event_ids) == len(set(event_ids)):
                # Order operations within subset deterministically by event_id
                sorted_combo = tuple(sorted(combo, key=lambda c: c.event_id))
                subsets.append(sorted_combo)
    return subsets


def search_spending_plans(
    case: RequestCase,
    baseline_trace: LedgerTrace | None = None,
    baseline_safe: float | None = None,
    baseline_earliest: date | None = None,
) -> CandidatePlan | None:
    """Exhaustively search legal spending-change subsets if baseline plan is infeasible."""
    request, profile = case.request, case.profile
    if baseline_trace is None:
        baseline_trace = build_ledger_trace(case, request.request_date)
    if baseline_safe is None:
        baseline_safe = amount_safe_to_pay(
            baseline_trace.ledger, profile.current_available_balance,
            profile.minimum_balance_to_keep, request.request_date,
            request.requested_amount,
        )
    if baseline_earliest is None:
        baseline_earliest = earliest_date_for_full_payment(
            baseline_trace.ledger, profile.current_available_balance,
            profile.minimum_balance_to_keep, request.request_date,
            request.requested_amount,
            {row.date for row in baseline_trace.contributions if row.amount > 0},
        )

    # 1. Zero-change preference: if baseline safe plan exists, do NOT search changes
    zero_winner = select_plan(
        enumerate_plans(case, baseline_trace.ledger, baseline_safe, baseline_earliest),
        request.desired_completion_date,
    )
    if zero_winner is not None:
        return zero_winner

    # 2. Get eligible spending changes
    eligible = eligible_changes(case, baseline_trace.expense_streams)
    if not eligible:
        return None

    # 3. Enumerate all legal subsets of size 1, 2, 3
    subsets = enumerate_change_subsets(eligible, max_size=3)
    baseline_movement_sum = sum(baseline_trace.ledger.values())

    feasible_candidates: list[tuple[CandidatePlan, float, tuple[str, ...]]] = []
    for subset in subsets:
        changes_dict = {c.event_id: c.target_amount for c in subset}
        op_strs = tuple(c.operation_str for c in subset)
        try:
            changed_trace = build_ledger_trace(case, request.request_date, changes=changes_dict)
        except Exception:
            continue

        saving = sum(changed_trace.ledger.values()) - baseline_movement_sum
        plans = enumerate_plans(
            case, changed_trace.ledger, baseline_safe, baseline_earliest,
            spending_changes=op_strs,
        )
        for p in plans:
            if p.is_safe and p.schedule and max(d for d, _ in p.schedule) <= request.desired_completion_date:
                feasible_candidates.append((p, saving, op_strs))

    if not feasible_candidates:
        return None

    # Official global ranking key first, then secondary tie-break:
    # 1. fewer changed events
    # 2. largest total 90-day saving (-saving)
    # 3. lowest deterministic event-id ordering (op_strs)
    feasible_candidates.sort(key=lambda item: (
        plan_sort_key(item[0]),
        len(item[2]),
        -round(item[1], 2),
        item[2],
    ))
    return feasible_candidates[0][0]


search = search_spending_plans

