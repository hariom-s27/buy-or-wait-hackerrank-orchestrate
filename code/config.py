"""Shared configuration and enums for the Buy or Wait? solution (Step 1)."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
OUTPUT_PATH = REPO_ROOT / "output.csv"

HORIZON_DAYS = 90

# Step 9 Frozen Judgment Call Defaults (Ablation Winners D11-D15)
HORIZON_INCLUSIVE: bool = True
EARLIEST_WINDOW: str = "fixed"
VARIABLE_SPEND_ESTIMATOR: str = "mean"
SPENDING_TIE: str = "fewest"
SEARCH_ALL_DAYS: bool = False

ALLOWED_AFFORDABILITY_STATUS = {
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
}

ALLOWED_RECOMMENDED_PAYMENT_METHOD = {
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
}

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]
