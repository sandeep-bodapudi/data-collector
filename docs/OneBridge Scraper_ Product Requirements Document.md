# PRD: OneBridge Scraper (working title)

**Type:** Internal web tool · **Version:** 1.0 draft · **Stack:** MERN, self-hosted, free/open-source only

## 1. Overview

**Problem.** Team members need data from many websites and APIs. Today that means manual copying or paid tools with usage caps. **Solution.** One internal web app where anyone can pick a connector, fill a generated form, run a scrape, then clean, merge, share and export the results as sheets. There are no per-run credits and no usage limits.

**Goals**

1. Non-technical users get from "I need data" to an Excel file in under 3 minutes.
2. No artificial caps on runs, rows, sheets or exports.
3. Every data operation (merge, dedupe, convert, share) is available in the UI.
4. New sites can be added as connectors without frontend changes.

**Non-goals (v1):** public/customer access, billing, mobile-native apps, resale of data.

**Roles**

| Role | Can do |
| --- | --- |
| Admin | Everything, plus users, connectors on/off, credential vault policy, audit log, settings |
| Member | Run connectors, own sheets, share, export, schedules |
| Viewer | Open shared sheets, export if the owner allows |

## 2. "Unlimited" plan

"Unlimited" means **no quotas, credits or per-user caps**. The real limit is server capacity, so the design manages capacity instead:

- **Fair-share queue:** each user gets a concurrency slice. Idle capacity is shared, so one heavy user cannot starve others, and nobody is blocked by a monthly cap.
- **Horizontal scaling:** workers are stateless Docker containers. Add machines to raise throughput.
- **Per-domain politeness:** delays and concurrency per target site, to avoid IP blocks. This protects the tool and is not a user quota.
- **Large data:** sheets are stored in chunks. The grid uses server-side paging, so a 1M-row sheet stays responsive.
- **Storage:** no row limits. Admins see a disk-usage dashboard and can set optional retention rules (default: keep forever).
- **Capacity indicator:** the UI shows queue position and an estimated wait when the system is busy.

## 3. Information architecture

Left sidebar (collapsible) and top bar.

| Section | Purpose |
| --- | --- |
| Home | Dashboard: recent runs, quick start, shortcuts |
| Connectors | Search and browse scrapers |
| Runs | All runs, live status |
| Sheets | Library of all result sheets (the core workspace) |
| Tools | Merge & Dedupe, Convert, Compare |
| Schedules | Recurring runs and monitors |
| Shared with me | Sheets and presets others shared |
| Vault | Saved logins and API connections |
| Admin | Users, connectors, audit, system (Admin only) |

Top bar: global search (sheets, runs, connectors, people), notifications bell, theme toggle, profile menu. Shortcut `Ctrl/Cmd+K` opens a command palette.

## 4. UI design system

- **Layout:** 12-column grid, 8px spacing scale, max content width 1440px, data screens full width.
- **Look:** clean, dense, data-first. Neutral gray surfaces, one accent color (blue), semantic colors (green success, amber warning, red error).
- **Typography:** Inter (UI), JetBrains Mono (selectors, logs, JSON). Sizes 12/14/16/20/28.
- **Themes:** light and dark, with system default. Use design tokens (CSS variables) for all colors.
- **Components:** buttons (primary/secondary/ghost/danger), inputs, selects with search, multi-select chips, tag input, date-range picker, tabs, modals, side drawers, toasts, tooltips, skeleton loaders, empty states with a call to action, status badges (Queued, Running, Paused, Succeeded, Failed, Cancelled), progress bars, confirm dialogs.
- **States:** every screen defines loading, empty, error and partial-result states.
- **Responsive:** desktop first; tablet supported; phones are read-only for viewing sheets and run status.
- **Accessibility:** WCAG 2.1 AA, keyboard navigation everywhere, visible focus, ARIA labels, color is never the only signal.
- **Feedback:** optimistic UI where safe, undo toast for destructive actions (10 s), autosave on edits.

## 5. Screens

### 5.1 Login

Email and password (JWT with refresh), optional company SSO later. Forgot password via admin reset. Lockout after repeated failures. Remember-me option.

### 5.2 Home dashboard

- Quick start card: search box "What do you want to scrape?" that opens matching connectors.
- Recent sheets (cards with thumbnail preview, row count, last updated), recent runs table, pinned connectors, schedule health, and queue status.
- Empty state for new users: guided 3-step walkthrough.

### 5.3 Connector library

- **Search and filters:** by name, category (Business listings, Jobs, E-commerce, News, Social, Documents/APIs, Custom), data type, "needs login", "official API", and favorites.
- **Cards:** icon, name, one-line description, input summary, average run time, risk badge (Open / Login / Restricted), favorite star.
- **Connector detail page:** description, what fields you get (sample output table), input form preview, limitations, last-tested date, "Run" and "Save preset".
- **Restricted connectors** are hidden unless an Admin enables them, and show a policy notice when used.
- **Custom scraper builder** (see 5.4).

### 5.4 New run (auto-generated form)

Left: form built from the connector's JSON input schema. Right: live summary panel.

- Field types: text, long text, number, toggle, select, multi-select, URL list, keyword list, location picker, date range, file upload (CSV of URLs/keywords), credential picker (from Vault).
- **Bulk input:** paste lines or upload CSV; shows count and validation errors per line.
- **Options (collapsible):** max items (default none), depth, delay, retries, schedule, run-as-preset.
- **Presets:** save, name, share and load input sets.
- Actions: Run now, Schedule, Save preset. Pre-flight check (valid URLs, credential present, robots.txt notice) before running.
- **Custom URL scraper:** paste a URL, preview the page in a side panel, click elements to pick CSS selectors, name each column, test on 3 pages, then run across a URL list or pagination rule. Selectors can be edited as text.

### 5.5 Runs list

Table: name, connector, status, started, duration, items, owner, schedule. Filters (status, connector, owner, date), search, bulk actions (cancel, rerun, delete, share). Click opens Run detail.

### 5.6 Run detail (live)

- Header: status badge, progress bar, items collected, elapsed time, ETA, queue position (if waiting).
- Buttons: Pause, Resume, Cancel, Rerun, Duplicate settings, Open results.
- Tabs: **Results** (live-updating grid, rows appear as scraped), **Log** (filterable, downloadable), **Input**, **Errors** (failed URLs with reason and "retry failed only"), **Screenshots** (error captures).
- Partial results are saved so nothing is lost if a run fails.
- Every successful run creates a **Sheet** automatically.

### 5.7 Sheets library

Grid or list view. Columns: name, source, rows, size, owner, tags, updated, shared state. Search, filter by tag/owner/connector, folders and tags, sort, multi-select with bulk actions (merge, export, share, move, delete). Trash with 30-day restore.

### 5.8 Sheet workspace (data grid), the main screen

Toolbar: Undo/Redo, Find, Filter, Sort, Columns, Dedupe, Merge, Export, Share, Views.

**Grid features**

- Virtualized rows, server-side paging for large sheets, sticky header and first column, resizable and reorderable columns, column type icons.
- Sort (multi-column), filters (text contains/equals/regex, number ranges, date ranges, empty/not empty), quick filter chips, search across all columns with highlighted matches.
- Column operations: rename, hide/show, change type, split, merge columns, trim, change case, find & replace (with regex), fill, delete.
- Cell editing with autosave and edit history; add/delete rows; multi-row select; copy/paste to and from Excel.
- Row expand drawer showing all fields, source URL (clickable), scraped-at time.
- **Saved views:** filters, sort and column layout saved per sheet, shareable.
- Summary bar: row count, selected count, unique counts per column (hover), null counts.
- Version history: every operation (merge, dedupe, edit batch) creates a version; restore any version.
- Comments on rows or cells, with @mentions.

### 5.9 Merge & Dedupe (wizard)

Entry points: select 2+ sheets and click Merge, or open Tools → Merge & Dedupe, or click Dedupe inside one sheet.

**Step 1: Choose sheets.** Add 2 or more sheets (drag to reorder; the order sets priority). Show row counts.

**Step 2: Combine mode.**

| Mode | Result |
| --- | --- |
| Stack (append) | All rows from all sheets, columns unioned |
| Join (like lookup) | Inner, left, right or full join on key columns |
| Compare | Rows only in A, only in B, in both (no merge) |

**Step 3: Map columns.** Auto-matches by name and type. Users drag to fix, rename the output columns, ignore columns, or add a source-sheet column.

**Step 4: Duplicate rules.**

- Choose key columns (one or several, such as email, or name + phone).
- Match options: exact, ignore case, trim spaces, ignore punctuation, normalize phone/URL/domain, optional fuzzy match with a similarity slider (default 90%).
- **When duplicates are found, keep:** first, last, most complete row (fewest empty cells), or from a priority sheet.
- Merge-fill option: fill empty cells in the kept row from the removed duplicates.
- Option to save removed duplicates to a separate sheet instead of discarding.

**Step 5: Preview.** Summary (rows in, duplicates found, rows out), sample table with removed rows highlighted and a "why removed" explanation. Fuzzy matches are shown as pairs for approval.

**Step 6: Run.** The operation runs as a background job for large data. Result is saved as a **new sheet** (originals are untouched), with lineage ("merged from A, B, C"). One click to undo by deleting the result, since originals are preserved.

### 5.10 Convert and Export

Export dialog from any sheet, selection or view.

- **Formats:** Excel (.xlsx), CSV, TSV, JSON, JSON Lines, and PDF (table report).
- **Options:** all rows / filtered rows / selected rows; choose and order columns; header row on/off; date and number formats; delimiter and encoding for CSV (UTF-8 BOM option for Excel); split into files by N rows.
- **Excel specifics:** multi-sheet workbook (export several sheets into one file, one tab each), frozen header, auto-filter, auto column width, clickable URL cells, optional summary tab.
- **Import:** upload .xlsx, .csv or .json to create a sheet, so existing files can join merges.
- Large exports run in the background; a toast and notification appear with a download link. Export history is kept for 7 days.

### 5.11 Sharing

Share button on sheets, runs, presets, schedules and saved views.

- Share with specific users or groups (internal accounts only).
- Permission levels: **Viewer**, **Commenter**, **Editor**, **Owner** (transfer).
- **Link sharing:** "anyone in the company with the link" (login required) with viewer or editor access, optional expiry, and password.
- Toggle: allow viewers to export / copy data.
- Dialog shows who has access, with change and remove options.
- Notifications on share, on comment and on edit of shared items.
- Live presence (avatars of people currently viewing) and activity log per sheet.
- Share as snapshot (frozen copy) or live (updates as the sheet changes).
- Quick actions: copy link, email link via the company mail, download a copy.

### 5.12 Schedules and monitors

- Create from a run or preset: every N minutes/hours, daily, weekly, cron expression, with timezone.
- On completion: append to the same sheet, replace, or create a new sheet; dedupe automatically on key columns.
- **Change monitor:** compare each run with the previous one and highlight new, removed and changed rows; notify by in-app alert, email or webhook.
- Pause, resume, run now, history of runs per schedule.

### 5.13 Vault (credentials)

Used only for sites where the company owns the account or the terms allow automation.

- Add credential types: username/password, cookie/session, API key, OAuth connection (Google, Microsoft).
- Encrypted at rest (AES-256-GCM); values are never shown again after saving.
- Shows owner, last used, status (valid / expired / blocked), and which runs used it.
- Share a credential with a team without revealing it. Test connection button. Admin can revoke any credential.

### 5.14 Admin console

- **Users:** invite, roles, deactivate, reset password, groups.
- **Connectors:** enable/disable, set tier (Open / Login / Restricted), concurrency per domain, upload or version a connector, health checks and last test result.
- **Audit log:** who ran what, on which site, exported what; filterable and exportable.
- **System:** queue depth, worker status, disk usage, error rates, retention rules, SMTP, backups, app settings.

### 5.15 Notifications and profile

In-app notification center (run finished/failed, shared with you, comments, schedule alerts) with email options; profile with name, theme, default export format, notification settings and API token for personal scripts.

## 6. Connector model

Each connector is a module with: `meta` (name, category, tier), `inputSchema` (JSON Schema, drives the form), `run(input, ctx)`, `outputSchema` (drives grid columns), and tests.

| Tier | Examples | Policy |
| --- | --- | --- |
| Open | Public sites that allow it, job boards, directories | Enabled; robots.txt and rate limits enforced |
| API/OAuth | Google (Places, Sheets), Microsoft Graph | Official APIs with user consent |
| Login | Company-owned portals, vendor sites | Admin enabled; uses Vault |
| Restricted | LinkedIn and similar platforms | Disabled by default; legal approval needed; prefer official APIs or imports |

Also included: a generic "any URL + selectors" connector and an optional Apify bridge connector for users who bring their own account.

## 7. Architecture

- **Frontend:** React + TypeScript + Vite, Tailwind, a component library (shadcn/ui), TanStack Query, TanStack Table or AG Grid Community for the grid, react-hook-form with a JSON-Schema form renderer.
- **Backend:** Node.js + Express (TypeScript), REST API plus WebSocket/SSE for live updates, JWT auth.
- **Workers:** Node processes running Crawlee + Playwright, controlled by BullMQ (Redis).
- **Database:** MongoDB (users, sheets metadata, runs, schedules, audit); sheet rows in chunked collections; files (exports, screenshots) on local disk or MinIO.
- **Files:** ExcelJS for XLSX, fast-csv for CSV.
- **Deployment:** Docker Compose (web, api, worker, redis, mongo, minio, nginx), HTTPS, reverse proxy, restricted to the company network or VPN.
- **Backups:** nightly Mongo dump and file sync.

**Key data models:** User, Group, Connector, Preset, Run, Sheet (schema, version, owner, tags), SheetChunk (rows), SheetVersion, ShareGrant, Schedule, Credential, Export, AuditEvent, Notification.

**Main API groups:** `/auth`, `/connectors`, `/runs`, `/sheets` (rows, columns, versions), `/tools/merge`, `/tools/dedupe`, `/exports`, `/shares`, `/schedules`, `/vault`, `/admin`, `/notifications`.

## 8. Non-functional requirements

| Area | Target |
| --- | --- |
| Performance | Grid scroll stays smooth at 1M rows; filter/sort on 100k rows under 2 s; merge preview under 5 s for 100k rows |
| Reliability | Runs resume after worker crash; partial results always kept |
| Security | Encrypted credentials, role checks on every endpoint, audit logging, CSRF/XSS protection, input validation |
| Compliance | robots.txt respect, rate limiting per domain, retention rules, internal-only access, review of personal data handling |
| Observability | Structured logs, run metrics, error alerts to admins |
| Usability | First successful run without training; full keyboard support |

## 9. Delivery phases

| Phase | Scope |
| --- | --- |
| 1 Foundation | Auth, roles, queue, workers, connector framework, base layout and design system |
| 2 Core flow | Connector library, auto-form, runs, run detail, sheets library, basic grid, 2-3 connectors, CSV/XLSX export |
| 3 Data tools | Full grid features, Merge & Dedupe wizard, versioning, import, Convert options |
| 4 Collaboration | Sharing, comments, presence, presets, notifications |
| 5 Automation | Schedules, change monitors, webhooks, Vault, OAuth connectors |
| 6 Admin and hardening | Admin console, audit, backups, load and security testing, docs |

## 10. Acceptance criteria (samples)

- A new user runs a connector from a generated form and downloads .xlsx within 3 minutes.
- Merging 3 sheets with a shared email column removes duplicates per the chosen rule, shows a correct preview, and leaves the originals unchanged.
- Sharing a sheet with a viewer who has export disabled hides export for that user.
- Cancelling a run keeps already-scraped rows.
- A 1M-row sheet opens in under 3 s and scrolls without freezing.
- Disabling a connector in Admin removes it from search and blocks scheduled runs.

## 11. Risks and open questions

- **Blocked sites:** protected sites will block a single IP; free proxies are unreliable. Mitigation: official APIs, politeness limits, optional paid proxies later.
- **Maintenance:** scrapers break when sites change; assign an owner and use health checks.
- **Legal:** platform terms and privacy law (including India's DPDP Act) apply to personal data; get legal review before enabling Login or Restricted connectors.
- **Open:** first 3-5 target sites; SSO needed or not; expected peak concurrent users; server specs; data retention policy; whether Google Sheets export is required.