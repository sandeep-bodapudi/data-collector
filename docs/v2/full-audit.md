# Full Functionality Audit

**Date:** 2026-10-13
**Method:** live server on throwaway DB/store + all automated suites.
**Result:** PASS with one environmental note (see places timing).

## Live end-to-end (real HTTP, real app boot)

45 checks against a booted server: auth (login/logout/password change/viewer 403),
app shell, PWA files, healthz, settings, connections status, pacing/budget roundtrips,
toggle on/off, **web job live** (finished, plain-text result, persisted, deleted),
**social free-only live** (done), CSV parse, XLSX export, source CRUD + presets,
internal share open, customer link + public page + CSV, admin share list, share stops,
social estimate, schedule CRUD, org key save/delete, audit reads, proxy settings,
user add. **44/45 PASS.**

The one miss: a live places job exceeded the 280s check budget. Re-run with a 480s
budget: **done in 211s with 9 rows — PASS**. A direct function-level probe also
passed (geocode 3.2s, 5 schools in 40s). Root cause is external Overpass mirror
latency variance, not a product defect: the app reports progress throughout and
completes. Recommendation: keep places expectations in minutes, not seconds, on
first runs against cold mirrors.

## Automated suites (same day, all green)

- `test_api.py`: all PASS (133 checks: auth, jobs lifecycle, shares, customer links,
  sources, fallback DB, exports).
- `test_connection_health` + `test_phase2_cascade` + `test_frontend_static`: 43/43 OK.
- `test_phase2_admin_api.py`: 18/18. `test_phase3_social.py`: 30/30.
- `test_phase4_durable.py`: 18/18. `test_phase5_queue.py`: 24/24.
- `test_phase5_store.py`: 15/15. `test_phase5_schedule.py`: 30/30.
- `test_phase5_org_audit.py`: 23/23. `test_phase5_scale.py`: 20/20.
- `py_compile` (all touched files) + `node --check static/app.js`: clean.
- `instance/jobs/` empty after the full run (store isolation holds everywhere).

## Standing non-automatable items

- Manual browser walkthrough (visual layout, no harness in repo).
- Live Redis/RQ round trip and live provider spend (no servers/keys here by design).
