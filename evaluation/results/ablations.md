# Buy or Wait? Step 9 Ablation Study Results

## 1. Consolidated Ablation Table

| Experiment | Variant | status | method | plan | earliest | mean_days | spending | tol_<=1% | tol_<=2% | tol_<=5% | median_rel_err | improved | regressed | unchanged | changed IDs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Step 8 Baseline | Default frozen config | 18/25 | 19/25 | 18/25 | 16/25 | 10.92d | 22/25 | 4/25 | 6/25 | 10/25 | 11.31% | 0 | 0 | 25 | - |
| J1 | B: HORIZON_INCLUSIVE=False | 19/25 | 20/25 | 19/25 | 17/25 | 10.14d | 22/25 | 5/25 | 7/25 | 11/25 | 8.65% | 1 | 0 | 24 | request_09 |
| J2 | B: EARLIEST_WINDOW=rolling | 17/25 | 18/25 | 17/25 | 15/25 | 10.18d | 22/25 | 4/25 | 6/25 | 10/25 | 11.31% | 0 | 1 | 24 | request_11, request_23 |
| J3 | B: VARIABLE_SPEND=p60 | 16/25 | 17/25 | 16/25 | 15/25 | 10.18d | 21/25 | 3/25 | 4/25 | 10/25 | 11.27% | 6 | 10 | 9 | request_02, request_03, request_04, request_06, request_07, request_09, request_11, request_14, request_15, request_17, request_18, request_19, request_22, request_23, request_24, request_25 |
| J3 | C: VARIABLE_SPEND=p75 | 16/25 | 16/25 | 16/25 | 13/25 | 15.82d | 19/25 | 3/25 | 3/25 | 10/25 | 16.20% | 5 | 11 | 9 | request_02, request_03, request_04, request_06, request_07, request_09, request_11, request_14, request_15, request_17, request_18, request_19, request_22, request_23, request_24, request_25 |
| J4 | B: SPENDING_TIE=largest_saving | 18/25 | 19/25 | 18/25 | 16/25 | 10.92d | 22/25 | 4/25 | 6/25 | 10/25 | 11.31% | 0 | 0 | 25 | request_11 |
| J4 | C: SPENDING_TIE=lowest_event_id | 18/25 | 19/25 | 18/25 | 16/25 | 10.92d | 22/25 | 4/25 | 6/25 | 10/25 | 11.31% | 0 | 0 | 25 | request_11 |
| J5 | B: SEARCH_ALL_DAYS=True | 18/25 | 19/25 | 18/25 | 16/25 | 10.92d | 22/25 | 4/25 | 6/25 | 10/25 | 11.31% | 0 | 0 | 25 | - |

## 2. Decision Interpretation and Verdicts (D11–D15)

| Decision | Winner | Reason | Evidence |
|---|---|---|---|
| **D11 (J1)** | **Baseline A (HORIZON_INCLUSIVE=True)** | Rejected Variant B. Improved count is only 1 (`request_09`), failing the hard acceptance bar of `improved >= 2`. Moreover, 15/25 sample requests have transactions exactly on day 90 (e.g. rent debit in `request_09`); dropping day 90 creates an artificial capacity illusion. | Variant B improved: 1 (`request_09`), regressed: 0. Fails `improved >= 2`. |
| **D12 (J2)** | **Baseline A (EARLIEST_WINDOW='fixed')** | Rejected Variant B. Causes regression: 1 sample regressed (`request_23`), improved: 0 samples. Extending the safety window to `[d, d+90]` checks beyond the user's horizon and falsely rejects valid `wait` on 2025-07-15. | Variant B improved: 0, regressed: 1 (`request_23`), unchanged: 23. |
| **D13 (J3)** | **Baseline A (VARIABLE_SPEND_ESTIMATOR='mean')** | Rejected Variant B (p60) and Variant C (p75). Severe categorical regressions: p60 regressed 10 samples (status dropped from 18 to 16, method from 19 to 17, earliest from 16 to 14, breaking `request_11` and `request_23`); p75 regressed 11 samples. Over-conservative estimates destroy plan feasibility. | p60 regressed 10; p75 regressed 11. Heavy categorical penalties. |
| **D14 (J4)** | **Baseline A (SPENDING_TIE='fewest')** | Rejected Variant B (largest_saving) and Variant C (lowest_event_id). Improved: 0 samples (0 < 2). On `request_11`, both variants emit 3 changes instead of 2, adding an unnecessary 3rd spending change (`reduce_to:event_948`) that diverges further from ground truth (truth has 1 change). | Variant B & C: improved: 0, regressed: 0; changed: 1 (`request_11`). |
| **D15 (J5)** | **Baseline A (SEARCH_ALL_DAYS=False)** | Rejected Variant B (SEARCH_ALL_DAYS=True). No effect: changed 0 requests out of 25 (improved: 0, regressed: 0). Empirically proves Decision D4 / Step 5: cash balance is non-increasing between paydays, so full-payment safety can never step from unsafe to safe on non-income days. | Exactly 0 changed rows; identical results across all 25 requests. |
