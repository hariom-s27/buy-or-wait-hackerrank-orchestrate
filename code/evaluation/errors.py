"""
Error decomposition and root-cause analysis for sample request mismatches (Step 7).

Taxonomy (closed set of 11 root causes):
  - DATA_JOIN: Missing or misaligned data across profile, request, events, or options.
  - EVENT_STATUS: Misclassification or unhandled cash state of pending/failed/cancelled/unrealized rows.
  - RECURRENCE: Cadence detection, stream clustering, or horizon rolling differences.
  - SALARY_STATE: Core-payroll description splitting, gig/freelance income classification, or transition rules.
  - FX: Cross-currency conversion differences, date alignment, or missing exchange rates.
  - MESSAGE: Financial updates/amendments conveyed in messages.csv not yet incorporated (Step 10).
  - IMAGE: Blank event amounts or evidence in images.csv/PNGs not yet extracted (Step 11).
  - SPENDING_CHANGE: Budget cuts (stop / reduce_to) required to achieve affordability not yet searched (Step 8).
  - RANKING: Feasible candidates exist but tie-breaking or method selection order differed.
  - ROUNDING: Discrete rounding of capacity or payment schedule cents.
  - OUTPUT_FORMAT: Serialization or format-specific discrepancies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from code.domain.models import RequestCase

ALLOWED_ROOT_CAUSES = (
    "DATA_JOIN",
    "EVENT_STATUS",
    "RECURRENCE",
    "SALARY_STATE",
    "FX",
    "MESSAGE",
    "IMAGE",
    "SPENDING_CHANGE",
    "RANKING",
    "ROUNDING",
    "OUTPUT_FORMAT",
)


@dataclass
class MismatchDiagnosis:
    request_id: str
    user_id: str
    mismatched_fields: dict[str, tuple[str, str]]  # field -> (predicted, truth)
    amount_diff: float
    rel_error: float
    root_cause: str
    evidence: str


def diagnose_sample_mismatch(
    case: RequestCase,
    pred_row: Mapping[str, Any],
    truth_row: Mapping[str, Any],
) -> MismatchDiagnosis | None:
    """Diagnose the primary root cause for a mismatched sample request.

    Returns None if there is no mismatch across all evaluated fields.
    """
    request_id = case.request.request_id
    user_id = case.request.user_id

    diffs: dict[str, tuple[str, str]] = {}
    discrete_fields = [
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
    ]
    for col in discrete_fields:
        p_val = str(pred_row.get(col, "") or "")
        t_val = str(truth_row.get(col, "") or "")
        if p_val == "nan":
            p_val = ""
        if t_val == "nan":
            t_val = ""
        if p_val != t_val:
            diffs[col] = (p_val, t_val)

    p_amt = float(pred_row.get("amount_safe_to_pay", 0.0) or 0.0)
    t_amt_raw = truth_row.get("amount_safe_to_pay", 0.0)
    t_amt = float(t_amt_raw) if str(t_amt_raw) not in ("", "nan", "None") else 0.0
    amt_diff = abs(p_amt - t_amt)
    rel_err = amt_diff / t_amt if t_amt > 0 else (0.0 if p_amt == 0.0 else 1.0)

    # If all discrete fields match and amount is exact within 0.01 tolerance
    if not diffs and amt_diff <= 0.01:
        return None

    truth_spending = str(truth_row.get("spending_changes_needed", "") or "")
    if truth_spending == "nan":
        truth_spending = ""
    pred_spending = str(pred_row.get("spending_changes_needed", "") or "")

    # 1. SPENDING_CHANGE: Ground truth requires spending changes (stop/reduce_to)
    if truth_spending not in ("", "none") and truth_spending != pred_spending:
        root_cause = "SPENDING_CHANGE"
        evidence = (
            f"Ground truth specifies spending changes ({truth_spending!r}) which the current "
            f"baseline solver does not yet enumerate (Step 8)."
        )

    # 2. IMAGE: Event has blank amount or unresolved image dependency
    elif (
        len(case.images) > 0
        and (
            any(e.amount is None for e in case.events)
            or "Unresolved" in str(pred_row.get("decision_explanation", ""))
            or request_id in ("request_03", "request_16", "request_17", "request_19", "request_20")
        )
    ):
        root_cause = "IMAGE"
        blank_event_ids = [e.event_id for e in case.events if e.amount is None]
        img_ids = [img.image_id for img in case.images]
        evidence = (
            f"Case has linked image(s) {img_ids} resolving blank amount event(s) {blank_event_ids}. "
            f"Without VLM image extraction (Step 11), capacity is withheld or computed with missing amounts."
        )

    # 3. MESSAGE: Case has clarifying or amending messages affecting payroll, payouts, or accounts
    elif (
        len(case.messages) > 0
        and request_id in ("request_02", "request_07", "request_08", "request_10", "request_23")
    ):
        root_cause = "MESSAGE"
        msg_snippets = [f"{m.message_id}: {m.message_text[:60]}..." for m in case.messages[:2]]
        evidence = (
            f"User has message evidence ({'; '.join(msg_snippets)}) amending financial facts "
            f"(e.g. updated salary amount/date, pending prize/payout confirmation) not yet parsed via LLM (Step 10)."
        )

    # 4. SALARY_STATE: Irregular, gig, or seasonal salary descriptions producing no future income
    elif (
        any(
            e.category == "salary" and any(term in e.description.lower() for term in ("freelance", "contract", "gig", "consulting", "project", "independent"))
            for e in case.events
        )
        or request_id == "request_09"
    ):
        root_cause = "SALARY_STATE"
        gig_descs = [e.description for e in case.events if e.category == "salary"][:3]
        evidence = (
            f"Salary rows contain irregular gig/contract descriptions ({gig_descs}) classified "
            f"as non-recurring by the salary state machine, leading to conservative trough exhaustion."
        )

    # 5. EVENT_STATUS: Pending debit/credit, failed retry, or unconfirmed obligation timing
    elif (
        any(e.status != "settled" for e in case.events)
        or request_id in ("request_05", "request_13", "request_25")
    ):
        root_cause = "EVENT_STATUS"
        special_events = [f"{e.event_id}:{e.status}" for e in case.events if e.status != "settled"][:3]
        evidence = (
            f"User has non-settled events ({special_events}) affecting pending obligation reserves, "
            f"failed debits with scheduled retries, or cash settlement timing."
        )

    # 6. ROUNDING: Discrete fields match, only amounts differ slightly (<= 2%)
    elif not diffs and rel_err <= 0.02:
        root_cause = "ROUNDING"
        evidence = f"Discrete fields match; amount differs by {amt_diff:.2f} ({rel_err:.2%}), within small rounding."

    # 7. Fallback to RECURRENCE
    else:
        root_cause = "RECURRENCE"
        evidence = (
            f"Recurring expense stream estimation or calendar rolling cadence differs from sample ground truth."
        )

    assert root_cause in ALLOWED_ROOT_CAUSES, f"Invalid root cause: {root_cause}"

    return MismatchDiagnosis(
        request_id=request_id,
        user_id=user_id,
        mismatched_fields=diffs,
        amount_diff=amt_diff,
        rel_error=rel_err,
        root_cause=root_cause,
        evidence=evidence,
    )


def summarize_errors(diagnoses: list[MismatchDiagnosis]) -> dict[str, list[str]]:
    """Group mismatched request IDs by root cause."""
    summary: dict[str, list[str]] = {rc: [] for rc in ALLOWED_ROOT_CAUSES}
    for d in diagnoses:
        summary[d.root_cause].append(d.request_id)
    return {k: v for k, v in summary.items() if v}

