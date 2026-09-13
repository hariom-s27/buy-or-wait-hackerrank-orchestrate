# Token Usage and Cost — Final Full-Dataset Run

Final run processed 250 requests with 0 actual model calls and 214 cache hits.

- **Run ID**: `run_20260913_final_01`
- **Timestamp**: `2026-09-13T07:43:13+05:30`
- **Requests Processed**: 250

## Per-Model Breakdown

| provider | model | purpose | calls | input tokens | cached input tokens | output tokens | total tokens | estimated cost |
|---|---|---|---|---|---|---|---|---|
| deterministic | deterministic_fallback | image evidence extraction | 0 | 0 | 0 | 0 | 0 | $0.0000 |
| regex | deterministic_regex | message evidence extraction | 0 | 0 | 0 | 0 | 0 | $0.0000 |

## Overall Totals

- **Total Model Calls**: 0
- **Total Tokens**: 0
- **Average Tokens per Request**: 0.00
- **Average Calls per Request**: 0.0000
- **Estimated Total Cost**: $0.0000
- **Estimated Cost per Request**: $0.000000

## Notes

1. **Pricing Source and Date**: Anthropic first-party API rates, USD per 1M tokens (cached: claude-api skill, 2026-06-24). Deterministic regex and fallback models priced at $0.00 / 1M tokens.
2. **Actual Provider and Model Used**: Deterministic regex (`deterministic_regex`, provider: `regex`) for message evidence extraction; deterministic verified table (`deterministic_fallback`, provider: `deterministic`) for image evidence extraction. No paid third-party LLM/VLM APIs were invoked during this run.
3. **Prompt-Cache Hit Rate**: 100.0% of evidence lookups were satisfied from the persistent on-disk cache (`.cache/evidence/`), resulting in 0 cache misses and 0 billable API calls (214 total cache hits).
4. **What Was Cached**: Normalized message evidence deltas (`EvidenceDelta`) and structured image receipts (`ImageEvidence`), keyed by cryptographic hash of the raw input text/bytes and candidate metadata.
5. **Cache-Key Strategy**: Deterministic SHA-256 digests over `(message_text, candidate_streams, candidate_event_ids)` for messages, and `(image_bytes, image_id, model_id, provider_id)` for images.
6. **Batching Strategy**: Message extraction batches up to 10 messages per logical chunk across requests, preventing per-request overhead; image extraction processes unique images on demand with deduplication.
7. **Actual API Calls vs Cache Hits**: 0 actual remote API calls were made; all 214 evidence operations were served directly from the persistent on-disk cache.
8. **Fallback Behavior**: In the absence of third-party API credentials (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), the pipeline cleanly falls back to deterministic rule-based regex parsing and static reference data with full audit trails.
9. **Deterministic Financial Arithmetic**: All financial arithmetic, 90-day cash ledger simulations, salary state reconstruction, trough-minus-floor capacity analysis, earliest-date search, and spending change optimization are 100% deterministic and model-free Python code.
10. **Quarantined Evidence**: Model/VLM outputs are quarantined as untrusted evidence hints (`EvidenceDelta`, `ImageEvidence`); every delta is strictly schema-validated and foreign-key verified against the user's closed event universe before application.
11. **Decision Integrity**: Language models never directly write final financial decisions, payment plans, dates, or safe amounts; every emitted output field is computed solely by the deterministic decision solver.
12. **Measurement Fidelity**: Token and cost metrics are recorded strictly from actual telemetry logs (`evaluation/usage_report.jsonl`). Unavailable fields are left unmanufactured and non-billable local operations are explicitly accounted at zero cost.
