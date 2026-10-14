# Phase 4 Verification

**Date:** 2026-10-13
**Result:** PASS

## Suites

- `py_compile` on all touched Python files: clean.
- `node --check static/app.js`: clean.
- `tests/test_phase4_durable.py`: 18/18 PASS.
- `tests/test_frontend_static.py`: 5/5 PASS (UI contract guards).
- `tests/test_phase3_social.py`: 30/30 PASS.
- `tests/test_phase2_cascade` + `tests/test_connection_health`: 35/35 OK.
- `tests/test_phase2_admin_api.py`: 18/18 PASS.
- `tests/test_api.py`: all PASS, no regressions.

## Acceptance

- [x] Finished results collectible after process restart within TTL (memory dropped,
  status + result served from disk).
- [x] No secret material on disk (social spec with keys persisted via public spec only;
  automated scan test).
- [x] Share dialog shows contact %, dupe count, QA confirm (customer links require it).
- [x] Export name defaults to the standard (editable, date-filled).
- [x] Pacing preset changes job gaps without redeploy (fast vs conservative asserted).
- [x] All suites green.

## Fix round 2 (pre-Phase-5 audit findings, all closed)

- Recovered runs keep their stats: `elapsed` / `with_email` / `with_phone` are persisted
  and served, so post-restart run details no longer show zeros. Covered by
  "stored stats survive".
- Plan §3 restart-marking paragraph corrected to the shipped behavior (markers become
  error records; nothing 404s silently, nothing resumes).
- Test suites no longer litter the real `instance/jobs/`: `test_api.py`,
  `test_phase2_admin_api.py` and `test_phase3_social.py` now point `JOB_STORE_DIR` at
  their throwaway dirs (additive change; no existing test altered).
- Frontend contracts guarded by `tests/test_frontend_static.py` (checklist, naming,
  pacing, social form, transparency elements) — the repo has no JS harness, so these
  are presence + wiring assertions alongside `node --check`.
- Boot wiring proven by a real subprocess boot ("boot converts running markers"),
  not just a direct `rehydrate_jobs()` call.
- Store isolation completed for every suite: `test_connection_health.py` and
  `test_phase2_cascade.py` also point `JOB_STORE_DIR` at throwaway dirs, and a full
  run of all suites leaves `instance/jobs/` empty (verified).
