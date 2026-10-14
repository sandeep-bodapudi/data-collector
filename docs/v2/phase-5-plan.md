# OneBridge Data Collector V2 — Phase 5 Plan (Paid Scale, Optional)

**Version:** 5.0-phase5-draft
**Date:** 2026-10-13
**Status:** ALL PHASES IMPLEMENTED — all tests green (see phase-5-verification.md).
**Depends on:** Phases 1–4 complete (all suites green)

---

## 1. Objective

Scale beyond the single-process model **only when volume demands it**, without
rewriting what works. Today: one Flask process, thread-per-job, payloads on local
disk, sheets in the browser. That comfortably serves a small internal team. Phase 5
activates paid infrastructure piece by piece, each sub-phase independently shippable
and reversible.

**Start Phase 5 when any of these is true:** runs routinely queue behind each other;
a deploy wipes running (not finished) jobs at a painful moment; the team wants
overnight scheduled runs; or leadership approves a hosting/proxy budget.

## 2. Scope

### In scope

- **5A. Background workers (Redis + RQ).** Jobs move out of web threads into RQ
  workers. Same `_run()` code, same app-context pattern (Phase 3 fix), but the web
  process never blocks on a long run. `rq-scheduler` for delayed retries.
- **5B. Shared job store (Postgres).** Payloads move from local disk to a
  `job_payloads` table (JSONB, TTL cleanup job). Required the moment there is more
  than one web/worker host; disk remains the single-host fallback. One-shot import
  script carries existing `instance/jobs/` files over.
- **5C. Schedules.** Per-user saved schedules (query set + cron expression + timezone)
  executed by the scheduler worker. Results land as new sheets/jobs with the same
  collect-then-delete flow; failures notify in-app.
- **5D. Org keys + audit log.** Admin-managed organization vault (Brave/CSE/AI/social
  keys shared to members by role) alongside personal keys (personal wins on conflict).
  Append-only `audit_log` table: who ran what, provider usage, spend deltas, shares
  created/stopped. Powers the spend dashboard with real numbers.
- **5E. Proxy + production hardening.** Residential/mobile proxy support in `Fetcher`
  (per-domain rotation, failure backoff), Gunicorn multi-worker config, `/healthz`
  endpoint, structured logs, DB backup runbook extension.

### Out of scope

- Rewriting the free cascade, social logic, or share model — they run unchanged
  inside workers.
- Multi-region, SSO, or billing other than the existing soft-budget counters.

## 3. Design notes

- **Compatibility first:** every sub-phase keeps the current REST shapes
  (`/api/jobs`, `/result`, `DELETE`, status payloads). The browser cannot tell whether
  a worker or a thread ran the job.
- **5A ordering:** add Redis + one worker process + feature flag (`WORKER_MODE=rq`
  default off). With the flag off, behavior is byte-identical to today — instant rollback.
- **5B migration:** dual-write (disk + Postgres) for one release, then read-prefer-DB,
  then retire disk. TTL purge becomes a scheduled DB delete.
- **5C safety:** schedules inherit all Phase 2–3 guardrails (preflight, budgets, caps);
  a schedule that would breach budget is skipped with a log entry, never charged.
- **5D precedence:** personal key → org key → disabled. Audit writes are fire-and-forget
  (a failed audit write never fails the job).
- **5E cost honesty:** proxies are the only large recurring cost. Start with datacenter
  IPs + free engines (current behavior); enable residential only per-connector with its
  own monthly cap, same UX as social budgets.

## 4. Cost anchors (≈ ₹96 = $1, verify at purchase time)

| Item | Indicative cost |
|------|-----------------|
| Managed Redis (small) / self-hosted | ₹800–2,500/mo or ~₹0 self-hosted |
| Always-on host (2–4 GB) | ₹1,500–4,000/mo |
| Postgres managed (small) | ₹1,000–3,000/mo or ~₹0 self-hosted |
| Residential proxies | ₹5,000+/GB — cap per connector, default off |
| Social/APIs | Unchanged from Phase 3 budgets |

Rule: infra spend needs the same explicit approval as social spend. No silent meters.

## 5. Files to touch (indicative)

| Area | Change |
|------|--------|
| `scraper/queue.py` | NEW — RQ wrappers, feature flag |
| `scraper/job_store.py` | Postgres backend alongside disk |
| `scraper/jobs.py` | Enqueue path; worker entrypoint reuses `_run_with_context` |
| `models.py` | `job_payloads`, `schedules`, `org_credentials`, `audit_log` |
| `app.py` | Schedule CRUD, org-key admin, audit reads, `/healthz` |
| `scraper/fetch.py` | Proxy rotation |
| `docs/` | Runbooks + verification per sub-phase |

## 6. Testing plan (per sub-phase)

1. Flag-off run is byte-identical to today (full existing suites green, unchanged).
2. Flag-on run produces identical rows via worker (diff test, mocked network).
3. Kill worker mid-run → job marked error, never half-collected; schedule skip on
   budget breach logged.
4. Migration script round-trips disk payloads into Postgres with secrets scan clean.
5. Load smoke: N concurrent runs, no cross-talk (row isolation by job id).

## 7. Acceptance criteria

- [x] 5A: web process stays responsive during a 500-query run; rollback = one flag.
- [x] 5B: two hosts share jobs; disk fallback still works single-host.
- [x] 5C: overnight schedule produces a collectible sheet; breach skips loudly.
- [x] 5D: org key usable by members, personal key wins, audit shows who spent what.
- [x] 5E: proxy cap enforced per connector; `/healthz` green; backups tested.

## 8. Risks + rollback

- New moving parts (Redis, workers) fail independently — each sub-phase ships behind a
  flag or additive table; rollback is flag-off or table-ignore, never a rewrite.
- Postgres JSONB migration is the only irreversible step — gated on backups + dry run.

## 9. Approval checklist

- [ ] Confirm trigger thresholds for starting 5A.
- [ ] Confirm RQ (simple) over Celery (heavy).
- [ ] Confirm Postgres-over-disk for shared state.
- [ ] Approve infra budget bounds before 5E.
- [ ] Authorise implementation, sub-phase by sub-phase.

**Reply with `approved` + any edits (or `approved 5A only`), and I will implement
exactly this plan, one sub-phase at a time.**
