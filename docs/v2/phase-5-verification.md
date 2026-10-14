# Phase 5 Verification (5A implemented; 5B–5E pending)

**Date:** 2026-10-13
**Result:** PASS (5A)

## Suites

- `py_compile` on all touched Python files: clean.
- `node --check static/app.js`: clean (untouched by 5A).
- `tests/test_phase5_queue.py`: 24/24 PASS (no Redis server needed; FakeQueue + mocks).
- `tests/test_phase4_durable.py`: 18/18 PASS.
- `tests/test_phase3_social.py`: 30/30 PASS.
- `tests/test_phase2_cascade` + `tests/test_connection_health`: 35/35 OK.
- `tests/test_phase2_admin_api.py`: 18/18 PASS.
- `tests/test_api.py`: all PASS, no regressions.
- `instance/jobs/` empty after the full run (store isolation holds everywhere).

## 5A Acceptance

- [x] Web process stays responsive: enqueue path returns a placeholder immediately,
  nothing runs locally (asserted).
- [x] Flag-off byte-identical: default mode is threads; missing rq/Redis degrades to
  threads automatically (asserted in this env, which has neither installed).
- [x] Worker runs the same `_run()` to done with persisted result (asserted, mocked net).
- [x] Progress snapshots visible mid-run with real phase/activity/counts.
- [x] Stop reaches workers via the disk cancel flag (asserted); thread path unchanged.
- [x] Enqueue failure falls back to threads (asserted); initial flake traced to live
  DDGS in the test and fixed by mocking search (no product change).
- [x] No secrets leave the process boundary: only `(job_id, spec, user_id)` cross it,
  and `spec` still carries per-user keys by design of the existing vault flow —
  same as thread mode; RQ payload uses the same channel as the current code path.
  Note: a shared Redis must be trusted network, documented in `.env.example`.

## 5A re-check fixes (all closed)

- `scraper/worker.py` no longer assumes the removed `rq.Connection` API: it uses the
  rq>=1 `Worker(queues, connection=...)` form with a legacy fallback.
- Orphan `.cancel` flags are purged alongside payloads (live flags kept); `_run`
  clears the flag on finish so stopped jobs never look stoppable later.
- Still unverifiable here: the live Redis/RQ round trip (no server on this machine).
  The unreachable-Redis branch is covered by the enqueue-failure test; the missing-deps
  branch by the worker-deps test.

## Still pending (5E, per plan)

- Proxy caps, `/healthz`, backups.

## 5C re-check fixes (all closed)

- Cron weekday `7` (Sunday alias) never matched — normalized to `0` in `parse_cron`,
  with star-range checks fixed to compare full sets. Covered by "weekday 7 means sunday".

## 5B re-check fixes (all closed)

- Live DB snapshots can no longer look expired: `job_payloads.updated_at` refreshes on
  every write and the TTL purge judges unfinished rows by it (asserted: "live snapshot
  immune"). Stale stop-flag rows are purged too.

## 5C Acceptance (implemented)

- [x] Saved schedules (name + cron + full job body, 20/user cap) with per-owner
  isolation (asserted, including member-cannot-touch and viewer-403).
- [x] One validation gate for manual and scheduled runs: `build_job_spec()` extracted
  from `create_job` with identical messages and list-tolerant inputs, so saved run
  specs replay (asserted via repeat + trigger tests).
- [x] Due schedules fire once per minute with the run tagged (`schedule_id/name`);
  budget breach is recorded on the row, never charged (asserted).
- [x] Scheduler loop runs in-process behind `SCHEDULER_ENABLED` (multi-web note in
  `.env.example`); per-row failures recorded, loop never raises.
- [x] UI: repeat-from-run modal, Runs-view schedule list with toggle/delete and
  last-run status (contract-guarded; no JS harness in repo).

Suites: `tests/test_phase5_schedule.py` 30/30 PASS; all standing suites re-run green.

## 5D Acceptance (implemented)

- [x] Org keys fill the gap for members without personal keys; personal always wins;
  per-key role gates hold (asserted, including viewer-only invisibility to members).
- [x] Names only ever leave the server; values masked even for admins (asserted).
- [x] Audit records job starts/finishes, social spend deltas, share create/stop,
  schedule fire/skip, org key save/delete — writes never fail callers (asserted).
- [x] Admin-only org/audit APIs with per-owner isolation; UI cards + shared-key hints
  in Settings and run forms (contract-guarded).

Suites: `tests/test_phase5_org_audit.py` 23/23 PASS; all standing suites re-run green.

## 5D re-check fixes (all closed)

- Org-key roles are now editable in the admin UI (per-row selector), not just the API.

## 5E Acceptance (implemented)

- [x] Proxy is fully off unless master + connector enabled with a monthly cap set and
  a URL configured — same posture as social budgets (asserted).
- [x] Page-fetch bytes metered exactly, search reply bytes metered as received, usage
  visible in admin settings payload and UI; over-cap traffic goes direct (asserted).
- [x] Per-connector toggles (search engines vs page fetching) with validated cap input
  and member-proof admin APIs (asserted, including 403s and bad-cap 400).
- [x] `/healthz` public, secret-free, 200/503 with database/store/redis parts (asserted).
- [x] `backup.py` copies SQLite + job payloads with a manifest, `--check` dry-run,
  Postgres path degrades to instructions (asserted).

Suites: `tests/test_phase5_scale.py` 20/20 PASS; all standing suites re-run green.

## 5B Acceptance (implemented)

- [x] Disk remains the default; unknown backend values fall back to disk (asserted).
- [x] `db` mode round-trips payloads with no disk file written (asserted).
- [x] `dual` writes both and falls back to disk when the row is gone (asserted).
- [x] Stop flags live in the shared table on shared backends, disk files otherwise (asserted).
- [x] TTL purge, restart conversion, and discard cover both backends (asserted).
- [x] One-shot migration moves disk files, skips present rows, reruns safely (asserted).
- [x] DB rows hold public payloads only (decompressed scan test).

Suites: `tests/test_phase5_store.py` 13/13 PASS; all standing suites re-run green
after the call-site switch (`persist`/`retrieve`/`discard`/`clean`/`all_ids`,
`cancel_request`/`cancel_requested`/`cancel_clear`).
