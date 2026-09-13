"""
Controlled experiment runner: Step-14 Production Baseline vs Featherless Candidate.
Runs both systems on the 25 labelled sample requests without touching output.csv.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Safe API key initialization
from code.evidence.featherless import (
    get_featherless_api_key,
    extract_all_deltas_featherless,
    extract_all_images_featherless,
    FEATHERLESS_TEXT_MODEL,
    FEATHERLESS_VISION_MODEL,
)
from code.config import OUTPUT_COLUMNS, OUTPUT_PATH
from code.domain import fx
from code.domain.models import Decision, RequestCase
from code.evidence.images import extract_all_images
from code.evidence.messages import extract_all_deltas
from code.evaluation.main import check_explanation_consistency
from code.io import indexes
from code.io.loaders import _row_to_request
from code.main import decide_case, decision_row
from code.output.validator import validate_rows
from code.output.writer import write_output

BASELINE_FILES = [
    "output.csv",
    "output_v1_deterministic.csv",
    "output_v2_evidence.csv",
    "output_v3_image.csv",
    "output_v3_final.csv",
]

def get_hashes() -> Dict[str, str]:
    res = {}
    for fname in BASELINE_FILES:
        p = _REPO_ROOT / fname
        if p.exists():
            res[fname] = hashlib.sha256(p.read_bytes()).hexdigest()
    return res

def evaluate_predictions(preds: List[Dict[str, Any]], truth_df: pd.DataFrame, cases: List[RequestCase]) -> Dict[str, Any]:
    n = len(preds)
    status_exact = 0
    method_exact = 0
    plan_exact = 0
    plan_relaxed = 0
    earliest_exact = 0
    spending_exact = 0
    spending_set_match = 0
    explanation_consistent_count = 0

    earliest_days_diffs: list[float] = []
    exact_amt = 0
    tol_1pct = 0
    tol_2pct = 0
    tol_5pct = 0
    tol_10pct = 0
    rel_errors: list[float] = []

    pred_by_id = {r["request_id"]: r for r in preds}
    case_by_id = {c.request.request_id: c for c in cases}

    for _, truth in truth_df.iterrows():
        rid = truth["request_id"]
        pred = pred_by_id[rid]
        case = case_by_id[rid]

        t_status = str(truth.get("affordability_status", "") or "")
        p_status = str(pred.get("affordability_status", "") or "")
        if t_status == p_status:
            status_exact += 1

        t_method = str(truth.get("recommended_payment_method", "") or "")
        p_method = str(pred.get("recommended_payment_method", "") or "")
        if t_method == p_method:
            method_exact += 1

        t_plan = str(truth.get("payment_plan", "") or "")
        p_plan = str(pred.get("payment_plan", "") or "")
        if t_plan == p_plan:
            plan_exact += 1

        t_earliest = str(truth.get("earliest_date_for_full_payment", "") or "")
        if t_earliest == "nan":
            t_earliest = ""
        p_earliest = str(pred.get("earliest_date_for_full_payment", "") or "")
        if p_earliest == "nan":
            p_earliest = ""
        if t_earliest == p_earliest:
            earliest_exact += 1

        if t_earliest and p_earliest:
            try:
                td = datetime.date.fromisoformat(t_earliest)
                pd_ = datetime.date.fromisoformat(p_earliest)
                earliest_days_diffs.append(abs((pd_ - td).days))
            except ValueError:
                pass

        t_spend = str(truth.get("spending_changes_needed", "") or "")
        p_spend = str(pred.get("spending_changes_needed", "") or "")
        if t_spend == p_spend:
            spending_exact += 1

        t_spend_set = set() if t_spend in ("", "none") else set(t_spend.split("|"))
        p_spend_set = set() if p_spend in ("", "none") else set(p_spend.split("|"))
        if t_spend_set == p_spend_set:
            spending_set_match += 1

        p_amt = float(pred.get("amount_safe_to_pay", 0.0) or 0.0)
        t_amt_raw = truth.get("amount_safe_to_pay", 0.0)
        t_amt = float(t_amt_raw) if str(t_amt_raw) not in ("", "nan", "None") else 0.0

        amt_diff = abs(p_amt - t_amt)
        if amt_diff <= 0.01:
            exact_amt += 1

        if t_amt > 0:
            rel_err = amt_diff / t_amt
        else:
            rel_err = 0.0 if p_amt == 0.0 else 1.0

        rel_errors.append(rel_err)
        if rel_err <= 0.01:
            tol_1pct += 1
        if rel_err <= 0.02:
            tol_2pct += 1
        if rel_err <= 0.05:
            tol_5pct += 1
        if rel_err <= 0.10:
            tol_10pct += 1

        is_consistent, _ = check_explanation_consistency(case, pred)
        if is_consistent:
            explanation_consistent_count += 1

    rel_errors_sorted = sorted(rel_errors)
    median_rel_err = rel_errors_sorted[n // 2] if n > 0 else 0.0
    mean_days_off = (sum(earliest_days_diffs) / len(earliest_days_diffs)) if earliest_days_diffs else 0.0

    return {
        "n": n,
        "status_exact": status_exact,
        "method_exact": method_exact,
        "plan_exact": plan_exact,
        "earliest_exact": earliest_exact,
        "mean_days_off": mean_days_off,
        "spending_exact": spending_exact,
        "spending_set_match": spending_set_match,
        "exact_amt": exact_amt,
        "tol_1pct": tol_1pct,
        "tol_2pct": tol_2pct,
        "tol_5pct": tol_5pct,
        "tol_10pct": tol_10pct,
        "median_rel_err": median_rel_err,
        "explanation_consistent_count": explanation_consistent_count,
    }

def main():
    print("============================================================")
    print("FEATHERLESS INTEGRATION COMPARISON — CONTROLLED EXPERIMENT")
    print("============================================================")

    # 1. Pre-run file protection check
    hashes_pre = get_hashes()
    print(f"Recorded SHA256 hashes for {len(hashes_pre)} baseline files.")

    # 2. Key initialization
    api_key = get_featherless_api_key()
    print(f"Featherless API key present: {bool(api_key)}")

    # 3. Load 25 labelled sample requests
    data, _ = indexes.load_and_index()
    fx.init_rates(data.exchange_rates)

    sample_path = _REPO_ROOT / "dataset" / "sample_requests.csv"
    sample_df = pd.read_csv(sample_path)
    for _, row in sample_df.iterrows():
        indexes._requests_by_id[row["request_id"]] = _row_to_request(row)

    sample_rids = [row["request_id"] for _, row in sample_df.iterrows()]
    cases_for_deltas = [indexes.build_request_case(rid) for rid in sample_rids]

    # 4. System A: Step-14 Production Baseline
    print("\nRunning System A (Step-14 Baseline)...")
    deltas_a = extract_all_deltas(cases_for_deltas)
    imgs_a = extract_all_images()
    cases_a = [indexes.build_request_case(rid) for rid in sample_rids]
    rows_a = [decision_row(decide_case(case, deltas_a, imgs_a)) for case in cases_a]
    metrics_a = evaluate_predictions(rows_a, sample_df, cases_a)

    # 5. System B: Featherless Evidence Integration Candidate
    print("\nRunning System B (Featherless Candidate)...")
    deltas_b = extract_all_deltas_featherless(cases_for_deltas, api_key=api_key)
    imgs_b = extract_all_images_featherless(api_key=api_key)
    cases_b = [indexes.build_request_case(rid) for rid in sample_rids]
    rows_b = [decision_row(decide_case(case, deltas_b, imgs_b)) for case in cases_b]
    metrics_b = evaluate_predictions(rows_b, sample_df, cases_b)

    # 6. Save candidate output to output_v4_featherless.csv
    v4_path = _REPO_ROOT / "output_v4_featherless.csv"
    write_output(rows_b, v4_path)
    print(f"Wrote {len(rows_b)} rows to {v4_path}")

    # 7. Post-run file protection verification
    hashes_post = get_hashes()
    for fname in BASELINE_FILES:
        assert hashes_pre[fname] == hashes_post[fname], f"BASELINE FILE TAMPERED: {fname} hash changed!"
    print("Baseline file protection check: PASS (all baseline hashes identical).")

    # 8. Field-by-field Comparison
    print("\n" + "=" * 60)
    print("FIELD-BY-FIELD ACCURACY COMPARISON (25 LABELLED SAMPLES)")
    print("=" * 60)
    fields = [
        ("affordability_status", "status_exact"),
        ("recommended_payment_method", "method_exact"),
        ("payment_plan (exact)", "plan_exact"),
        ("earliest_date_for_full_payment", "earliest_exact"),
        ("spending_changes_needed", "spending_exact"),
        ("explanation consistency", "explanation_consistent_count"),
        ("amount_safe (exact +-0.01)", "exact_amt"),
        ("amount_safe (within 1%)", "tol_1pct"),
        ("amount_safe (within 2%)", "tol_2pct"),
        ("amount_safe (within 5%)", "tol_5pct"),
        ("amount_safe (within 10%)", "tol_10pct"),
    ]
    for label, k in fields:
        va = metrics_a[k]
        vb = metrics_b[k]
        print(f"{label:32s}: System A = {va}/25 ({va/25:.2%}) | System B = {vb}/25 ({vb/25:.2%})")

    print(f"{'median relative error':32s}: System A = {metrics_a['median_rel_err']:.2%} | System B = {metrics_b['median_rel_err']:.2%}")
    print(f"{'earliest_date mean days off':32s}: System A = {metrics_a['mean_days_off']:.2f}d | System B = {metrics_b['mean_days_off']:.2f}d")

    # 9. Changed Request IDs & Column Differences
    pred_a_by_id = {r["request_id"]: r for r in rows_a}
    pred_b_by_id = {r["request_id"]: r for r in rows_b}
    truth_by_id = {r["request_id"]: r for _, r in sample_df.iterrows()}

    changed_rids = []
    changes_detail = []
    improved_rids = []
    regressed_rids = []

    for rid in sample_rids:
        ra = pred_a_by_id[rid]
        rb = pred_b_by_id[rid]
        rt = truth_by_id[rid]
        diff_cols = [c for c in OUTPUT_COLUMNS if ra.get(c) != rb.get(c)]
        if diff_cols:
            changed_rids.append(rid)
            changes_detail.append((rid, diff_cols, ra, rb, rt))

    print(f"\nChanged Request IDs ({len(changed_rids)}): {changed_rids}")

    for rid, cols, ra, rb, rt in changes_detail:
        print(f"\n--- [{rid}] Changed columns: {cols} ---")
        for c in cols:
            print(f"  {c:30s}: A = {ra.get(c)!r}")
            print(f"  {' '*30}  B = {rb.get(c)!r}")
            print(f"  {' '*30}  Truth = {rt.get(c)!r}")

    # Validator check on rows_b
    data_full, _ = indexes.load_and_index()
    violations_b = validate_rows(rows_b, sample_df, data_full.options_df, data_full.profiles_df, data_full.events_df)
    print(f"\nValidator violations on System B output: {len(violations_b)}")

    # 10. Summary analysis
    results_summary = {
        "metrics_a": metrics_a,
        "metrics_b": metrics_b,
        "changed_rids": changed_rids,
        "changes_detail": [
            {
                "request_id": rid,
                "changed_columns": cols,
                "system_a": {c: ra.get(c) for c in cols},
                "system_b": {c: rb.get(c) for c in cols},
                "truth": {c: rt.get(c) for c in cols},
            }
            for rid, cols, ra, rb, rt in changes_detail
        ],
        "validator_violations": len(violations_b),
    }

    with open(_REPO_ROOT / "featherless_comparison_data.json", "w", encoding="utf-8") as f:
        json.dump(results_summary, f, indent=2, default=str)

    print("\nSaved comparison data to featherless_comparison_data.json")

if __name__ == "__main__":
    main()
