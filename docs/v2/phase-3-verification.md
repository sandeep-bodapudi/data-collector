# Phase 3 Verification

**Date:** 2026-10-13
**Result:** PASS

## Suites

- `py_compile` on all touched Python files: clean.
- `node --check static/app.js`: clean.
- `tests/test_phase3_social.py`: 30/30 PASS (mocked, no network, no spend).
- `tests/test_phase2_cascade` + `tests/test_connection_health`: 35/35 OK.
- `tests/test_phase2_admin_api.py`: 18/18 PASS.
- `tests/test_api.py`: all PASS, no regressions.

## Acceptance

- [x] Free core untouched with social disabled (regression suites green).
- [x] Social OFF unless master + provider enabled with key (preflight notes + free-only fallback).
- [x] Hybrid ON by default; free hits never trigger paid calls (mock-counter test).
- [x] Estimate pre-run; breach returns 402 with remaining-budget math.
- [x] Spend increments only on successful paid fills; 100% cap blocks further paid calls.
- [x] No secrets in logs/responses (mask + SECRET_KEYS + status tests).

## Fix round (re-verification findings, all closed)

- Worker threads now run inside an app context (`_run_with_context`), so toggles,
  limits, spend reads and spend writes work at runtime; fallback is a context-free
  run rather than a dead job. Covered by "threaded job finishes with context".
- `social` / `brightdata` / `apify` default to OFF in the registry; paid calls additionally
  require a set monthly budget (402 otherwise). Covered by "social off by default" and
  "paid blocked with no budget".
- 80% warning logged at start and when crossed mid-run. Covered by "80% warning fires mid-run".
- Spend and `paid_fills` increment only when a provider reply adds data; empty replies
  log "not counted as spend". Covered by "empty reply counts no spend".
- Pre-run estimate visible in the Social form via `POST /api/social/estimate`
  (worst-case, labelled); spend % shown in the connections banner and status payload.
  Covered by "estimate endpoint answers".
- Spend table added to the admin Connections card from the budgets API.
- Linkless items skip paid lookup with a note. Covered by "linkless item skips paid".
