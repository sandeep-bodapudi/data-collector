# OneBridge Data Collector V2 — Phase 4 Plan (Durable Jobs + Delivery Hardening)

**Version:** 4.0-phase4-draft
**Date:** 2026-10-13
**Status:** IMPLEMENTED — approved and coded, all tests green (see phase-4-verification.md)
**Depends on:** Phase 3 complete (social layer, all suites green)

---

## 1. Objective

Close the two structural gaps the original V2 plan left for last:

1. **Jobs survive a process restart** (original Phase B). Today `JOBS` is an in-memory
   dict: a redeploy or crash wipes finished results the browser hasn't collected yet.
2. **Delivery readiness** (original Phase E). The share flow needs a pre-send checklist,
   standard export naming, and pacing controls an admin can actually click.

Non-negotiable, unchanged: ₹0 mandatory spend, free path default, paid/social strictly
opt-in with budget, public data only.

---

## 2. Scope

### In scope

- A. Durable job store: finished (and cancelled-with-rows) results persisted to
  `instance/jobs/<job_id>.json.gz` (SQLite file stays for accounts/settings; job payloads
  stay out of the DB so shares/exports can't bloat it). TTL 2 hours, then purged.
- B. Restart recovery: on boot, rehydrate the finished-job index from disk so
  `GET /api/jobs/<id>` and `/result` keep working; running jobs from before the
  restart are marked `error` with "server restarted mid-run — start again" (never
  silently resumed, never silently dropped).
- C. Client-collects-then-delete preserved: `DELETE /api/jobs/<id>` removes disk + memory.
- D. Delivery checklist in the share dialog: % rows with contact, duplicates removed
  count, sample-QA reminder with explicit confirm checkbox (mirrors the existing
  customer-acknowledgement pattern).
- E. Standard export naming default: `Client_Vertical_Geo_YYYYMMDD` prefilled in the
  sheet-name field (still editable).
- F. Admin pacing presets: Conservative / Standard / Fast mapping to the existing
  `SEARCH_GAP`/`KEYED_GAP` env knobs via `AppSetting` (no redeploy to retune).
- G. Secret-rotation checklist doc (vault re-key steps, what breaks, rollback).
- H. Tests: restart-survival (write → simulate restart → collect), checklist stats,
  naming default, pacing preset effect, plus the standing regression suites.

### Out of scope (Phase 5, optional)

- Always-on host, Redis/Celery queue, schedules, org-wide keys, full audit log,
  residential proxies, volume contracts (original Phase F — deferred until paid scale
  is actually needed).

---

## 3. Design notes

- **Store shape:** `{spec_public, columns, rows, status, log_tail, finished_at}`.
  Secrets never touch disk: persist `public_spec()` only (SECRET_KEYS already covers
  social keys). Rows are already plain text via `Job.result()`.
- **Index:** tiny JSON index file or the existing `ConnectionHealth`-style approach —
  directory listing is the index (O(n) files, n small due to TTL purge). `purge_jobs()`
  deletes expired files as well as memory entries.
- **Concurrency:** jobs write their file once at finish (atomic rename); collection
  reads are plain file reads. No lock server needed at this volume.
- **Restart marking:** every run writes a crash marker at start; on boot, any marker
  still in flight becomes an `error` record ("server restarted mid-run — start again"),
  so a restart never silently drops a run the browser is waiting on and never resumes
  one either. Finished payloads are served straight from disk.
- **Checklist stats** are computed client-side from the sheet (rows, email/phone
  coverage, duplicate names) — server only enforces the existing acknowledgement flag.
- **Pacing presets** read `AppSetting` with env fallback, so current deployments keep
  exact current behavior when nothing is set.

---

## 4. Files to touch

| File | Change |
|------|--------|
| `scraper/job_store.py` | NEW — save/load/purge/list finished jobs on disk |
| `scraper/jobs.py` | Persist on finish/cancel-with-rows; rehydrate on boot; `_own_job` falls back to disk |
| `app.py` | Boot rehydrate call; pacing preset read; share-acknowledge unchanged |
| `static/app.js` | Checklist UI, naming default, pacing preset control, estimate row untouched |
| `tests/test_phase4_durable.py` | NEW — restart survival, delete-after-collect, checklist stats, naming, presets |
| `docs/` | Secret-rotation checklist + verification file |

---

## 5. Testing plan

1. Write finished job → drop memory entry → `GET` status/result still served from disk.
2. Cancelled-with-rows job persists and is collectible.
3. TTL purge removes disk files; collect-then-delete removes them immediately.
4. Secrets absent from every persisted file (scan for key material).
5. All standing suites green; `py_compile` + `node --check` clean.

---

## 6. Acceptance criteria

- [x] Finished results collectible after process restart within TTL.
- [x] No secret material on disk (automated scan test).
- [x] Share dialog shows contact %, dupe count, QA confirm.
- [x] Export name defaults to the standard (editable).
- [x] Pacing preset changes job gaps without redeploy.
- [x] All suites green.

See `phase-4-verification.md` for evidence.

---

## 7. Risks + rollback

- Disk fills: TTL purge + 25 MB cap pattern reused from shares; rollback = delete
  `instance/jobs/` (jobs become uncollectable, nothing else breaks).
- Partial writes on crash: atomic rename; corrupt files ignored with a log line.

---

## 8. Approval checklist

- [x] Confirm disk (not DB table) for payloads.
- [x] Confirm 2-hour TTL kept.
- [x] Confirm restarted mid-run jobs marked error, not resumed.
- [x] Confirm checklist items (% contacts, dedupe count, QA confirm).
- [x] Authorise implementation.

**Reply with `approved` + any edits, and I will implement exactly this plan.**
