# PRD status: OneBridge Scraper

Checked against `OneBridge Scraper_ Product Requirements Document.md` (v1.0 draft) on 5 Oct 2026, updated 6 Oct 2026 for the storage change and PWA.

**Legend:** ✅ done · 🟡 partly done · ❌ not started

## Stack decision

The PRD names a MERN stack (React, Node/Express, MongoDB, BullMQ, Playwright, Docker). The working product is **Python + Flask** with **MySQL / PostgreSQL / SQLite** (via `DATABASE_URL`) and a no-build front end. Everything below is built on that stack. Moving to MERN would be a rewrite. Decide whether that's still wanted before Phase 3.

## Storage decision (6 Oct 2026)

The PRD says sheets are stored on the server (chunked collections, server-side paging, §2 and §7). The team chose a different model because the free Render database is small:

- **Database:** only user accounts, roles, admin switches and encrypted AI keys.
- **Browser (IndexedDB):** every sheet and the run history, one database per signed-in user.
- **Server memory:** a live run's rows, until the browser collects them.

**What this gives up compared with the PRD** (decide before starting Phase 4/5):

| PRD item | Effect |
|---|---|
| §5.11 Sharing, "Shared with me", comments, presence | Sharing is built as an explicit snapshot copy uploaded to the server (expiring, size-capped). Live sharing, comments and presence are not possible while sheets exist only on one device. |
| §5.12 Schedules and change monitors | A scheduled run has nowhere to put its result when nobody has the browser open. It needs server-side result storage, at least for scheduled runs. |
| §5.14 Admin: audit of what each person exported, disk dashboard | Partly covered: Admin lists every active customer link (who, to whom, opens, downloads). Exports to a person's own device are not visible to the server. |
| §2 "1M-row sheet stays responsive" | Sheets are held in the browser's memory when opened; fine for roughly 100k rows, not 1M. |
| Data safety | Clearing browser data deletes sheets. Settings has backup and restore; there is no automatic copy. |

## Phase-by-phase

| PRD phase | Status | Notes |
|---|---|---|
| 1 Foundation: auth, roles, queue, workers, connector framework, layout, design system | 🟡 | Auth, roles, the design system and an installable offline-capable app (PWA) are done. Jobs run in background threads, not a Redis queue. There is no plug-in connector framework yet; the two connectors are built in. |
| 2 Core flow: connector library, auto-form, runs, run detail, sheets library, basic grid, CSV/XLSX export | 🟡 | Runs, run detail, sheets library, grid and exports are done. There's no connector library or schema-driven auto-form, because only 2 connectors exist. |
| 3 Data tools: full grid, Merge & Dedupe wizard, versioning, import, Convert | 🟡 | The Merge & Dedupe wizard and import are done. Full grid editing, versioning and PDF/TSV export are not. |
| 4 Collaboration: sharing, comments, presence, presets, notifications | ❌ | |
| 5 Automation: schedules, monitors, webhooks, Vault, OAuth | 🟡 | The per-user Vault exists, encrypted, for AI keys and restricted cookies. No schedules, monitors or webhooks. |
| 6 Admin and hardening: admin console, audit, backups, testing | 🟡 | Users and the connector policy are in Admin. No audit log, backups or load tests. |

## Section by section

| § | Requirement | Status | Where / gap |
|---|---|---|---|
| 1 | Roles: Admin, Member, Viewer | ✅ | Enforced on every API (`roles_required`, ownership checks). |
| 2 | Unlimited: fair-share queue, per-domain politeness, capacity indicator | 🟡 | Per-domain delay and robots.txt are done. No per-user fair-share queue or queue position. |
| 3 | Information architecture: sidebar + top bar | 🟡 | Home, Runs, Sheets, Merge & Dedupe, Settings and Admin are done. No global search, notifications or `Ctrl+K` palette. |
| 4 | Design system: tokens, light/dark, badges, states, responsive, accessibility | ✅ | `static/app.css`. Every screen has empty, loading and error states. Keyboard focus is visible. |
| 5.1 | Login, lockout, remember me | ✅ | Forgot-password is an admin reset. No JWT or SSO (cookie sessions). |
| 5.2 | Home dashboard: quick start, KPIs, recent runs and sheets, 3-step walkthrough | ✅ | |
| 5.3 | Connector library | ❌ | Two fixed connectors: Web search and Places (OpenStreetMap). |
| 5.4 | New run form + live summary + pre-flight check | ✅ | No presets, schedules, CSV upload of inputs, or point-and-click custom URL scraper. |
| 5.5 | Runs list: filters, search, delete | ✅ | No bulk rerun. |
| 5.6 | Run detail: live status, ETA, Results/Log/Input tabs, partial results saved | ✅ | No Pause/Resume, Errors tab or screenshots. |
| 5.7 | Sheets library: search, filter, multi-select, bulk merge/delete | ✅ | Stored in the browser (see Storage decision). No folders, tags or trash/restore. Backup and restore in Settings. |
| 5.8 | Sheet workspace: grid, search with highlight, paging, row drawer | 🟡 | No cell editing, column operations, saved views, versions or comments. |
| 5.9 | Merge & Dedupe wizard | ✅ | Stack mode with key columns, match options (case, spaces, smart phone/email/URL, punctuation), keep first/last/most complete, merge-fill, save removed duplicates, preview with reasons. No join/compare modes or fuzzy matching. |
| 5.10 | Export and import | 🟡 | xlsx/csv/json export (CSV has a BOM for Excel). Import of xlsx/csv/tsv/json. No PDF, selected-rows export or split files. |
| 5.11 | Sharing | 🟡 | Two kinds. **Customer link** (the main use: sending a spreadsheet to a client outside the company): public branded page without an account, optional passcode with lockout, message, expiry (1/7/30 days), Excel and CSV download, open/download counts, sender confirmation, admin list with Stop. **Colleagues**: chosen people or anyone signed in with the link, downloads on/off. Links can be sent from Gmail, the email app, WhatsApp, Telegram or the device share menu. Not built: Editor/Commenter roles, groups, live (auto-updating) shares, presence, notifications. Turning downloads off for colleagues hides the buttons; it cannot stop someone copying what is on screen. |
| 5.12 | Schedules and monitors | ❌ | |
| 4 (PWA) | Installable, offline-capable app | ✅ | Manifest, icons (any and maskable), service worker, install button, offline indicator, update prompt. The signed-in page copy is removed on sign-out. Starting runs, importing and Excel export still need a connection. |
| 5.13 | Vault | 🟡 | AES-256-GCM at rest, values never shown again, per user. No team sharing of credentials, status tracking or OAuth. |
| 5.14 | Admin console | 🟡 | Users (add, role, deactivate, reset password) and connector policy. No audit log, system dashboard or retention rules. |
| 5.15 | Notifications and profile | 🟡 | Profile: password and theme. No notification centre. |
| 6 | Connector tiers: Restricted off by default | ✅ | Logged-in LinkedIn/Facebook collection only works after an admin enables it, with a legal warning. |
| 8 | Security: encrypted credentials, role checks, XSS protection | ✅ | See "Fixed in this review" below. No CSRF tokens yet (SameSite=Lax cookies reduce the risk). |

## Fixed in this review

These problems were found in the code committed before this review:

1. **API keys and LinkedIn/Facebook cookies were written into every Excel file** (the "Run Info" tab) and stored in plain text in the database. Anyone who received an exported file got the keys. **Rotate any AI key or cookie that was used with the old version.**
2. **Anyone could watch or stop anyone else's run**: there was no login or ownership check on the job status and stop endpoints.
3. **The first visitor to a fresh server became admin.** On a server, the admin now comes from `APP_USER` / `APP_PASSWORD`.
4. **The app could not start on Render.** It used a hard-coded local XAMPP MySQL address, and `requirements.txt` was missing the database, login and pandas packages.
5. **Contact pages were fetched while ignoring robots.txt** (`ignore_robots=True`). The PRD requires robots.txt respect.
6. **The "obfuscated email" detector invented addresses** from ordinary sentences, e.g. "Great place.Visit" became `gre@place.visit`.
7. **Scraped text starting with `=` became a live Excel formula** (formula injection).
8. **The "Check contact pages" switch did nothing.** Contact pages were always fetched.
9. **AI defaults had outdated models.** These included a retired Claude 3 Haiku, Gemini 1.5 Flash and Groq `llama3-8b-8192`. The Gemini key was also sent in the URL.
10. **The map lookup sent a fake Chrome User-Agent**, which OpenStreetMap rejects (HTTP 406). This made "Find places" fail every time.
11. **Failed runs said "Succeeded".** Runs that found nothing because a service was down now show "Failed" with the reason.
12. **Placeholder phone numbers** like `+91-8888888888` were accepted.
13. **Merge used file names as identifiers.** An unsanitised output name could inject HTML into the page.

## Found on 6 Oct 2026

- **`render.yaml` was committed with `SECRET_KEY`, `APP_USER` and `APP_PASSWORD` in plain text.** The values are removed from the file now, but they remain in git history. Rotate them: change the admin password in Settings, and set a new `SECRET_KEY` in Render (everyone signs in again and re-enters their AI key).
- **The obfuscated-email detector and Telugu text:** the "ignore punctuation" match option deleted Telugu/Hindi vowel signs, so different names could look identical. Fixed and covered by a unit test.
- **A dead database would have taken the whole site down** (free Render databases expire after 30 days). The app now falls back to a temporary SQLite file and Admin shows a warning.

## Suggested next steps (in order)

1. **Add a persistent database on the server.** Set `DATABASE_URL` to a PostgreSQL database. Without it, the server's SQLite file is wiped on every redeploy and added users are lost. Free Render databases expire after 30 days; see the README.
2. **Add a Brave Search API key** (`BRAVE_API_KEY`). Free engines often block cloud servers.
3. **Phase 4: finish collaboration** (5.11). Snapshot sharing exists; comments, notifications and live sharing would need the storage decision above revisited.
4. **Phase 5: Schedules** (5.12), using a job queue (RQ/Celery or BullMQ if moving to MERN).
5. **Add an audit log** (5.14).
