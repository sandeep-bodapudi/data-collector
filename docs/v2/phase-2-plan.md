# OneBridge Data Collector V2 — Phase 2 Plan

**Version:** 2.0-phase2-draft
**Date:** 2026-10-13
**Status:** IMPLEMENTED — approved and coded, all tests green
**Depends on:** Phase 1 complete (connection_health.py, ConnectionHealth/MonthlyBudget models, admin connection/budget APIs)

---

## 1. Objective

Make the website **workable without limits, with full clarity**:

1. Every external connection is visible, testable, and individually **enable / disable** by admin.
2. Before any job starts, the system **pre-flight checks** free options and only tries **successfully connected** ones.
3. Free internet options are used **dynamically** — cascade, merge, and degrade gracefully instead of hard-failing.
4. User always sees **what was tried, what worked, what was skipped, and why**.

Non-negotiable: **₹0 mandatory spend**. Paid/social stays off unless explicitly enabled with budget.

---

## 2. Scope

### In scope (Phase 2)

- A. Central connection registry (`config.py` + `connection_health.py` hardening).
- B. Admin connection matrix UI + APIs (enable/disable per connection).
- C. Pre-flight validation before every job (`effective_engines` / `effective_services`).
- D. Dynamic free search cascade (`search.py` + `jobs.py`).
- E. Dynamic places cascade (`places.py`).
- F. Dynamic AI provider order (`ai_extract.py`).
- G. Job-level transparency (log + status + UI badges).
- H. Tests + type checks for everything touched.

### Out of scope (deferred)

- Social enrichment providers (Bright Data / Apify) — Phase 3.
- Redis / Celery / scheduler / persistent cross-restart job store — Phase 4.
- New visual redesign — only minimal admin UI additions.

---

## 3. Current State (Phase 1 findings)

Completed:

- `scraper/connection_health.py` with 7 checks + 30-min cache.
- `ConnectionHealth`, `MonthlyBudget` models.
- `GET/POST /api/admin/connections`, `GET/POST /api/admin/budgets`.
- Startup health check call.
- 133 existing `test_api.py` tests still pass.

Gaps to fix in Phase 2 (no new features until these are solid):

1. `run_all_checks()` cache logic is confusing (fresh-check condition inverted for nominatim/photon/wikidata).
2. `status_string()` mixes health + enable state; needs `enabled + healthy + last_checked`.
3. `is_over_soft_limit` defined as property with args — must be a method.
4. `admin_toggle_connection` re-runs full network checks synchronously — must not block request.
5. No single source of truth for service list; strings scattered across files.
6. Unicode-safe output already fixed — keep ASCII `OK / FAIL / OFF`.
7. `tests/test_connection_health.py` has 1 failing test — must go green before Phase 2 code.

---

## 4. Design

### 4.1 Central registry — `scraper/config.py` (new)

Single source of truth. No other file hardcodes service names.

```python
SERVICES = {
  "brave":      {"label": "Brave Search", "kind": "search", "needs_key": True,  "free": True},
  "google_cse": {"label": "Google CSE",   "kind": "search", "needs_key": True,  "free": True},
  "ddgs":       {"label": "Free engines (DDGS)", "kind": "search", "needs_key": False, "free": True},
  "nominatim":  {"label": "Nominatim", "kind": "places", "needs_key": False, "free": True},
  "photon":     {"label": "Photon",    "kind": "places", "needs_key": False, "free": True},
  "overpass":   {"label": "Overpass",  "kind": "places", "needs_key": False, "free": True},
  "wikidata":   {"label": "Wikidata",  "kind": "places", "needs_key": False, "free": True},
  "ai":         {"label": "AI custom fields", "kind": "ai", "needs_key": True, "free": False},
}
FREE_SEARCH_ORDER = ["brave", "google_cse", "ddgs"]
FREE_PLACES_ORDER = ["overpass", "photon", "nominatim", "wikidata"]
```

Rules:

- `conn_<name>` in `AppSetting` = admin enable (`1` / `0`, default `1`).
- Health cache = network reality. Effective = `enabled AND healthy`.
- Time: O(1) per lookup. Space: O(n) services, n ~ 8.

### 4.2 Health hardening — `connection_health.py`

- Fix `run_all_checks()`: if cache fresh → return cache; else check and store.
- Add `is_effective(name)` = enabled (AppSetting, needs app context or injected dict) + healthy (cache).
- Add `check_overpass()` and `check_ddgs()` lightweight probes (Overpass: `?query=[out:json];out;` small; DDGS: one `ddgs.text()` with 4s timeout, failure-tolerant).
- Never raise; always return `{"healthy": bool, "error": str}`.
- Keep per-call timeouts ≤ 10s; total startup probe budget ≤ ~30s, run in background thread, never block Flask boot.

### 4.3 Admin matrix

APIs (extend existing, no breaking change):

- `GET /api/admin/connections` → `{services: {name: {label, kind, enabled, healthy, error, last_checked}}, status_string}`.
- `POST /api/admin/connections/<name>` → `{ok, enabled}`; does NOT run network probe inline; only flips flag.
- `POST /api/admin/connections/test` → `{name}` runs single probe on demand, updates cache + `ConnectionHealth` row.
- `POST /api/admin/connections/test-all` → background thread, returns immediately `{started: true}`.

UI (minimal, in existing admin panel):

- Table: Service | Kind | Enabled toggle | Health | Last checked | Test button.
- Banner: current `status_string` + budget line.
- Social section stays hidden/disabled until Phase 3.

### 4.4 Pre-flight before every job — `jobs.py`

New helper:

```python
def preflight(spec) -> dict:
  # returns {"search": [...effective names...], "places": [...], "ai_ok": bool, "notes": [...]}
```

- Called once at job start, logged via `job.say()`.
- If `mode==web` and `preflight["search"]` empty → fail fast with actionable error ("Enable Brave or free engines in Admin → Connections").
- If `mode==places` and no effective places service → same.
- If `custom_fields` requested and `ai_ok` False → continue without AI, note it (do not fail job).
- Store `job.effective` for `to_dict()` so UI can show badges.

### 4.5 Dynamic free search — `search.py`

Change `web_search(query, region, max_results, say, engines=None)`:

1. Build try-order = admin `FREE_SEARCH_ORDER` filtered to enabled.
2. Brave: skip if exhausted / disabled / unhealthy; on 429 mark exhausted once.
3. Google CSE: skip if no key/CX or disabled.
4. DDGS: merge across healthy free engines until `max_results` or all tried (existing merge logic kept).
5. Log each step: `used brave (12)`, `skipped google_cse (disabled)`, `nothing more from: ...`.
6. Raise `SearchBlocked` only if all *effective* engines refused; return `[]` if genuinely empty.

Complexity: same as today — O(E × R) where E = engines tried (≤ 6), R = max_results. No extra memory beyond merged list.

### 4.6 Dynamic places — `places.py`

Change `search_places(category, location, ...)`:

1. Read effective places list from preflight (Overpass → Photon → Nominatim → Wikidata, admin-filtered).
2. Try in order; first non-empty wins; log which source answered + caveat (existing caveats kept).
3. If all fail → raise original `PlaceError` (most informative one).
4. No behaviour change when all enabled (default).

### 4.7 Dynamic AI — `ai_extract.py`

- No provider-hopping in Phase 2 (user key is per-user, not global). Change is smaller:
- Add `ai_available(ai_dict)` helper: False if no key and provider != custom.
- `extract_fields()` already catches errors → keep, but ensure message is user-friendly and does not leak key.
- Admin `conn_ai` acts as global kill-switch; per-user key still required.

### 4.8 Transparency

- Job log lines must answer: tried what → got how many → skipped what + why.
- `to_dict()` gains `effective` field (no secret leakage via `public_spec`).
- Frontend: show badges next to job form (`Using: Brave + DDGS`, `Maps: Nominatim + Photon`, `AI: off — no key`). Frontend changes minimal; backend must supply the data.

---

## 5. Files to touch

| File | Change |
|------|--------|
| `scraper/config.py` | NEW — registry, orders, `is_enabled()` helper |
| `scraper/connection_health.py` | Fix cache logic, add overpass/ddgs probes, add `is_effective()`, fix `status_string` |
| `models.py` | Fix `is_over_soft_limit` property → method; add `ConnectionHealth.upsert()` helper |
| `app.py` | Wire config in, add `test` / `test-all` endpoints, make toggle non-blocking |
| `scraper/search.py` | Effective-order cascade, better skip reasons |
| `scraper/places.py` | Respect enabled flags, keep fallback caveats |
| `scraper/jobs.py` | `preflight()`, `effective` on Job, fail-fast with actionable errors |
| `scraper/ai_extract.py` | `ai_available()` + kill-switch respect |
| `tests/test_connection_health.py` | Fix failing test, add registry/preflight tests |
| `tests/test_phase2_cascade.py` | NEW — mocked-engine cascade tests (no network) |
| `tests/test_api.py` | Add 5–8 tests for new endpoints (no existing tests changed) |

No template rewrite; admin UI additions only.

---

## 6. Testing plan

1. `python tests/test_api.py` — must stay 100% green.
2. `python -m unittest tests.test_connection_health -v` — must go 27/27 green (fix 1 failure).
3. NEW `tests/test_phase2_cascade.py` (mocked, no network):
   - All disabled → fail-fast error.
   - Brave disabled → DDGS used.
   - Brave 429 → fallback used, exhaustion logged once.
   - Places: Overpass disabled → Photon tried.
   - AI off → job continues without custom fields.
4. Manual: toggle each service off in admin, run web + places job, confirm skip message.
5. Type check: `python -m py_compile` on all touched files; no new dependencies.

---

## 7. Acceptance criteria (Phase 2 done when)

- [x] Admin can enable/disable each connection; job behaviour changes immediately.
- [x] Job with all search disabled fails fast with actionable message (not silent empty).
- [x] Job with Brave exhausted still completes via DDGS with clear log.
- [x] Places works with any single service left enabled.
- [x] `status_string` + badges show enabled + health + reason everywhere needed.
- [x] All old + new tests green; no `&&`-style shell assumptions in docs.
- [x] No mandatory paid key; free path works out of box.

See `phase-2-verification.md` for evidence.

---

## 8. Risks + rollback

- Network probes flaky on cloud IPs → mitigated by cache + `enabled AND healthy` (admin can force-enable; probe never disables by itself).
- Startup slowdown → probes in background thread, ≤30s budget, failures silent.
- Rollback: every change is flag-gated; setting all `conn_*` to `1` restores Phase 1 behaviour. No schema drop needed.

---

## 9. Approval checklist

- [ ] Approve scope (no social providers in Phase 2).
- [ ] Approve `config.py` service list + default orders.
- [ ] Approve non-blocking toggle (probe only on explicit Test button).
- [ ] Approve fail-fast vs empty-success behaviour.
- [ ] Authorise implementation to start.

**Reply with `approved` + any edits, and I will implement exactly this plan.**
