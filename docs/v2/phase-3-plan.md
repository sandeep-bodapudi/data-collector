# OneBridge Data Collector V2 — Phase 3 Plan (Social Enrichment, Controlled Spend)

**Version:** 3.0-phase3-draft
**Date:** 2026-10-13
**Status:** IMPLEMENTED — approved and coded, all tests green (see phase-3-verification.md)
**Depends on:** Phase 2 complete (registry, preflight, dynamic cascades, admin matrix, verification green)

---

## 1. Objective

Add **optional, controlled social enrichment** without touching the free core:

1. Free path stays default and fully functional with ₹0 spend.
2. Social (LinkedIn first, then Instagram/Facebook) runs only when admin enables it, a provider key is set, and monthly soft budget remains.
3. Hybrid default: free web search first, paid social only on miss or explicit user choice.
4. Every social run shows estimated cost before start, actual spend after, and honest coverage (found / missed / skipped + why).

Non-negotiable: public data only, robots.txt respected, session/cookie method stays restricted + admin-only, OneBridge Social API never used for scraping.

---

## 2. Scope

### In scope

- A. Provider abstraction (`scraper/social_base.py`): common record shape, error taxonomy, cost estimator.
- B. Bright Data free-tier connector (first provider; ~5,000 records/month free).
- C. Cheapest-reliable Apify actor connector (pay-per-result fallback).
- D. New job mode `social`: input URLs/names → output columns → merge + dedupe into sheet pipeline.
- E. Hybrid logic: free lookup first, social only for misses or explicit selection.
- F. Budget guardrails: per-app monthly soft limit (INR + USD), 80% warn, 100% free-path-only, per-job estimate + block/confirm.
- G. Admin controls: enable/disable per provider, key storage (vault), budget inputs, test buttons, spend dashboard.
- H. Legal/safety: no login-walled scraping by default, explicit user consent for customer shares, no guaranteed-contact claims.
- I. Tests (mocked, no live spend) + docs.

### Out of scope

- Session/cookie scraping as primary method (stays restricted, off by default).
- Email guessing / waterfall enrichment.
- Redis/Celery/scheduler/scale-out (Phase 4).
- Frontend redesign beyond one Social form + admin additions.

---

## 3. Design

### 3.1 Record shape (all providers normalize to this)

Name, Headline, Current Company, Location, About, Experience summary, Website, Profile URL, Platform.

Empty string when unknown; never fabricate. Each row keeps `_source` (free / brightdata / apify) for the log, dropped before export.

### 3.2 Provider order (India-cost optimised)

1. Free web lookup (existing `web_search` + `extract.parse` for public snippets) — ₹0.
2. Bright Data free tier (if enabled + key + quota left).
3. Cheapest enabled Apify actor (if enabled + key + budget left).

Skip any provider that is disabled, unhealthy, out of budget, or missing a key — with a log line saying which and why.

### 3.3 Job mode `social`

Input: textarea of LinkedIn/Instagram/Facebook URLs or names (one per line, max 500), plus platform filter and `hybrid` toggle (default ON).

Flow per item:

1. Normalize URL → platform; if a name, free-search `site:linkedin.com/in <name>` etc. first.
2. If free path yields headline + company + location, keep it and skip paid (hybrid ON).
3. Else if social enabled and budget allows, call providers in order until filled or budget/estimate blocks.
4. Merge duplicates by normalized profile URL; fill blanks only, never overwrite good free data with emptier paid data.
5. Respect `MAX_SOCIAL_PER_RUN` (default 50, env-overridable) so one run cannot burn the month.

Complexity: O(N × P), N items, P providers tried (≤ 2 paid); memory O(N) rows. Pacing per provider key (≥1.1s) like Brave.

### 3.4 Cost control

- Admin sets `social_budget_inr` (suggested 2000–3000) + `social_budget_usd` auto-display at ₹96 = $1.
- `MonthlyBudget` rows (Phase 1/2) track spend per service; increment only on successful paid fills.
- Pre-run estimate = items × cheapest-enabled unit price; if estimate would breach limit, job refuses with message or offers free-only run.
- UI shows `Monthly spend: ₹X / ₹Y (Z%)`; 80% amber warning, 100% free-only + red banner.
- Realistic anchors: 1,000 profiles ≈ $1.40–$3.50 ≈ ₹135–₹340.

### 3.5 Admin matrix additions

Extend existing Connections table (no new page):

- `social` master toggle + `brightdata` + `apify` toggles.
- Key inputs via vault (masked, `__keep__` semantics like Brave/AI keys).
- Budget INR/USD inputs, reset cadence note (monthly).
- Test buttons hit cheap provider endpoints (quota check, not a full scrape).
- Spend table from `MonthlyBudget`.

Member-safe `GET /api/connections/status` gains social entries (enabled/healthy/spend %, no keys).

### 3.6 APIs (new, additive)

- `POST /api/jobs` gains `mode: social` spec validation (URLs ≤ 500, platforms subset, hybrid bool).
- `GET /api/admin/social/spend` (or reuse budgets API) for dashboard.
- No changes to existing routes; `public_spec()` must strip social keys.

### 3.7 Legal posture

- Social stays OFF by default; enabling requires explicit admin action + budget.
- Restricted cookie path untouched and still off by default with legal warning.
- Customer-share flow unchanged; social rows carry same passcode/expiry/audit behaviour.
- UI copy: “public profile fields only, coverage varies, no guaranteed contacts.”

---

## 4. Files to touch

| File | Change |
|------|--------|
| `scraper/social_base.py` | NEW — record shape, error taxonomy, estimator interface |
| `scraper/social_brightdata.py` | NEW — free-tier client, quota check, fill function |
| `scraper/social_apify.py` | NEW — actor client, per-result cost accounting |
| `scraper/jobs.py` | Add `_run_social()` + preflight `social` branch + fail-fast/estimate logic |
| `scraper/config.py` | Register `social`, `brightdata`, `apify` services + order |
| `scraper/connection_health.py` | Add `check_brightdata()` real probe + `check_apify()`; wire into `run_all_checks`/`refresh_single` |
| `app.py` | Vault keys for social, budget endpoints, job-spec validation, status endpoint gains social |
| `static/app.js` | One Social form + admin additions + spend banner (minimal) |
| `tests/test_phase3_social.py` | NEW — mocked providers, hybrid, budget-block, dedupe (no network/spend) |
| `tests/test_api.py` | Untouched (new coverage lives in the new file) |
| `docs/` | User + admin notes for social (separate small file) |

---

## 5. Testing plan (no live spend)

1. Unit: URL normalize, record merge, estimator math, budget-block boundary (80/100%).
2. Mocked provider tests: free-hit skips paid; free-miss calls Bright Data; Bright Data down falls to Apify; all-paid-off yields free-only rows.
3. Job tests: 500-row cap, dedupe by URL, `_source` dropped before `result()`.
4. API tests: social spec validation (bad URL, too many, bad platform), budget-block message, key masking.
5. Regression: `test_api.py`, `test_connection_health`, `test_phase2_*` stay green; `py_compile` + `node --check` clean.

---

## 6. Acceptance criteria

- [x] Free core untouched: web/places/enrich pass with social disabled.
- [x] Social OFF by default; enabling needs key + budget.
- [x] Hybrid ON by default; free hits never trigger paid calls (assert via mocks).
- [x] Estimate shown pre-run; breach blocks or offers free-only.
- [x] Spend increments only on successful paid fills; 80/100% banners correct.
- [x] All suites green; no secrets in logs/responses.

---

## 7. Risks + rollback

- Provider API drift → abstraction isolates; one broken provider degrades to others/free.
- Accidental spend → estimate gate + per-run cap + master toggle; rollback = disable `social` (one flag, immediate).
- Legal overreach → public-fields-only copy, no cookie path expansion, admin-gated.

---

## 9. Approval checklist

- [x] Confirm platform order (LinkedIn first?).
- [x] Confirm starting provider (Bright Data free tier?).
- [x] Confirm soft budget default (₹2,000–₹3,000?).
- [x] Confirm hybrid-ON default and 50/run cap.
- [x] Authorise implementation.

**Reply with `approved` + any edits, and I will implement exactly this plan.**
