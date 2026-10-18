# Free / Paid run flow - validation and plan

Status: **draft for review** (nothing implemented). Written 2026-10-15.

## 1. The idea

When the user clicks **New run**, the first choice is a tier:

- **Free** - map and directory sources, free search engines, the user's own pages. No keys needed.
- **Paid** - the run uses paid tools (Apify actors, Bright Data, Brave/Google keys, an AI connector) to get more intensive data.

After the tier: **Connections -> API keys -> AI connector -> the run form -> review and start.**
Paid tools are optional; a paid run may skip any of them.
The form is built dynamically from what is actually available, so every option shown works.

## 2. Validation

### What is right about it

| Point | Why it holds (evidence in the code) |
|---|---|
| Decide before configuring | Today `viewNewRun` (`static/app.js`) mixes free and paid options in one long form. Keys, AI and directory sources are discovered mid-form through "add it in Settings" hints. |
| Dynamic, honest options | `preflight()` already computes effective services (enabled AND healthy). The form does not use that result to hide or grey out options. |
| Fewer failed runs | Runs fail late today ("All search options are disabled or unavailable"). Checking capability before the form removes that class of failure. |
| Competitors confirm the pain | ZoomInfo/Apollo sell credits for "depth" (cost scales with data depth). Showing cost before a run is the same idea; users see it earlier. |

### What needs correcting before building

1. **"Paid" does not exist for most modes yet.** In `scraper/config.py` Apify and Bright Data are registered only as **social** providers (`SOCIAL_PROVIDER_ORDER`, per-profile cost). Web, Places and enrich runs have no paid backend at all. A "Paid" card for them would promise something that is not there. Paid must first mean something per mode (see section 4).
2. **Free vs Paid is really a spectrum.** A run that adds only a free Brave key is neither. Two hard cards force a wrong label. Better: two *starting points* plus a live "what this run will use" summary, so a free run can still pick up one key.
3. **Tier cannot be a security boundary.** "Paid" is just "uses the user's own keys". The server must still check keys and budgets on every request; the tier only drives the UI and the default settings.
4. **Fixed order is wrong for returning users.** After the first setup, keys and connections are already done. Showing them again each run is the same "again and again" pain. Steps that are already satisfied should collapse to a one-line "Ready" row.
5. **Intensity differs by source, not by tier.** The user wants precise data; what matters is which *fields* a source can fill. Tier should be an input to a field-capability map, not a label.

### Verdict

The idea is **sound with changes**: keep "decide first, then configure, then run", but (a) make Paid mean something per mode, (b) show a capability summary instead of a hard tier wall, and (c) collapse satisfied steps.

## 3. Recommended design (better plan)

**One entry, three outcomes.** New run opens with a **goal** card pair, then the tier is *derived*:

1. **Choose what to collect**: mode (companies / places / enrich contacts / social) and category/region.
2. **Choose depth**: `Standard (free)` or `Deep (paid tools)`. Standard is selected by default. Deep unlocks the paid providers that exist for that mode.
3. **Setup panel (only what is missing)**: lists connections/keys/AI for the chosen depth. Each row is *Ready*, *Optional - skip*, or *Needs key* with an inline "Add" that returns to the run. Skipping an optional row is always allowed.
4. **Run form**, generated from the capability map (section 5).
5. **Review**: services in the order they will be tried, expected fields, estimated time, estimated cost, and a plain list of what is unavailable and why.

Why this beats two fixed cards: it keeps the user's idea (free/paid up front, clear order) but avoids promising paid data where no provider exists, lets a user mix one key into a free run, and lets returning users skip straight through.

## 4. What "Paid" means per mode (needs building)

| Mode | Free today | Paid today | Paid to add |
|---|---|---|---|
| Web (companies) | Brave/Google/ddgs search + page crawl | Brave/Google keys speed it up | Apify website/contact crawler actor for sites that block plain fetch |
| Places | Overpass, Photon, Nominatim, Wikidata | none | Apify Google Maps places actor (phone, hours, rating, reviews) |
| Enrich | guess + crawl of the company site | none | Apify contact-info actor; email verification service |
| Social | none | Bright Data / Apify (exists) | - |

Each paid provider is a plug-in with: id, label, modes, fields it fills, cost per unit, key requirements, test call. This is the same registry pattern as `SERVICES` in `scraper/config.py`.

## 5. Capability map ("precise data, dynamically")

One server function, `run_options(mode, depth, user)`, returns:

- available **categories and regions** (only those some enabled source supports),
- per field (name, address, phone, website, email, hours, rating, services...): `fillable_by` = the enabled sources that can fill it, or `unavailable` + reason,
- the **ordered service chain** the run would use,
- **estimated cost and time**,
- **missing setup** items with where to fix them.

Built on `preflight()`, `effective_services()` and `/api/connections/status`. The form renders from this response, so a field nothing can fill is greyed with a reason, never an empty column. `build_job_spec` re-checks the same rules server-side.

Result rows carry `source`, `confidence` (found on the company's own site / found elsewhere / guessed) and `last_checked`. This also covers the email-trust gap found in the ZoomInfo/Apollo review.

## 6. Navigation

New run -> Connections -> API keys & AI -> Runs -> Sheets -> Tools -> Admin. Settings keeps key management; the run flow deep-links into it and returns to the same step.

## 7. Phases

1. **Foundation** - provider registry with field/mode metadata; `GET /api/run-options`; tier/depth in the job spec with server-side validation. No UI change. Tests.
2. **Paid providers for Places and Enrich** - at least one Apify actor each, behind the registry, with cost estimates and a spending cap. (Biggest unknown; do a spike first.)
3. **New run stepper** - goal, depth, setup panel, generated form, review. Collapsing satisfied steps.
4. **Menu and Settings reorder**, mobile layout of the stepper.
5. **Result quality columns** - source, confidence, last checked; MX/free-mail/role-address checks.
6. **Cleanup** - remove the old mixed form, update README and docs.

## 8. Risks

- Apify actor output formats change; wrap each in a normaliser and a test with a recorded fixture.
- Paid runs spend the user's money: need a per-run cap, a monthly budget (exists for social) and a confirm step above a threshold.
- Rewriting `viewNewRun` touches the largest function in the SPA; keep the old form behind the old route until the stepper passes the existing frontend tests.
- Free-tier hosting: long paid runs rely on the existing durable job store/worker; verify timeouts.

## 9. Open questions for the owner

1. OK to define **Paid = uses third-party paid tools with the user's own keys** (no billing in this app)? (Assumed yes.)
2. Which paid providers first - Apify only, or also Bright Data / a verification service?
3. Is a **Deep** run allowed to start with no keys set (and show the setup panel), or should it stay disabled until one is added?
4. Per-run spend cap default (suggest a small fixed amount with a confirm above it)?
5. Keep a "Quick run" shortcut that skips the stepper for returning users?

## 10. Code check (2026-10-15) - plan vs. the actual code

I re-read the plan against `app.py`, `scraper/config.py`, `scraper/social_apify.py` and `static/app.js`. What holds, and what the plan must change.

### Confirmed
- `preflight()` and `effective_services()` exist and are the right base for a capability map.
- `GET /api/connections/status` already returns per-service enabled/healthy and the user's social spend; the setup panel can reuse it.
- `build_job_spec` is the single gate shared by `POST /api/jobs` and the scheduler, so server-side tier/depth validation goes there once.
- The social path already has the pattern the paid modes need: estimate endpoint (`/api/social/estimate`), monthly budget, per-run cap (`MAX_SOCIAL_PER_RUN`), 402 when over budget.

### Corrections to the plan
1. **Routes.** The router only has `#/new/(web|places|social)` (`static/app.js:442`). "Enrich" is not a New-run route; it starts from a sheet. The stepper must treat enrich as "from a sheet", not as a fourth mode card. A mode switcher already exists inside the form (`[data-mode]`, `app.js:742`) - the stepper's step 1 replaces it.
2. **Size of the rewrite.** `viewNewRun` runs from line 579 to about 949 (~370 lines) in a 2,536-line file. Phase 3 is a substantial rewrite; it should be split into per-step render functions, and the old form kept until the existing frontend tests pass on the new one.
3. **Apify is not generic yet.** `scraper/social_apify.py` is built for one thing: `fill_profile(url, ...)` posts `{"profileUrls": [url]}` to one actor (default `apify/linkedin-profile-scraper`, one synchronous call per profile). A Google-Maps or contact-info actor takes different input (search strings / URLs, many results per call) and returns a different shape. Phase 2 needs a new generic actor client (input builder + output normaliser per actor), not a tweak. I have **not** verified that the default actor id exists on Apify today; confirm before relying on it.
4. **Budget is social-only.** `_social_limits`, `_social_spend` and the `MonthlyBudget` tracking are named and scoped for social enrichment. Paid Places/Enrich runs need that generalised to "paid spend" (or a second budget) before they can be allowed.
5. **Health defaults disagree.** `preflight()` treats a service with no fresh health entry as healthy, but `/api/connections/status` reports `healthy: False` for the same service (`entry.get("healthy", False)`). A capability map built on the status endpoint would show un-probed services as broken. Both must use one rule (unknown = usable, shown as "not checked yet").
6. **Registry labels.** `SERVICES["brightdata"]` has `"free": True` and `"apify"` has `"free": False`; the flag is not used consistently and must be defined (here: free = needs no paid account) before the depth card relies on it.
7. **Fields are not declared per source.** Nothing in the code states which fields (phone, email, hours, rating...) a Places or search source can fill. The field catalog in section 5 is new work: it has to be written from what each source actually returns (Overpass tags, page extraction, Wikidata) and tested, not assumed.
8. **Estimates exist for social only.** There is no cost/time estimate for web or Places runs on the server (the run form computes a rough count client-side). Time estimates for free runs would be new and approximate; plan them as "rough range", not a promise.

### Effect on phases
- Phase 1 gains two prerequisites: unify the health rule (item 5) and define `free` in the registry (item 6).
- Phase 2 is larger than the plan implied (items 3 and 4) and starts with a spike: pick one Places actor, call it once with a test key, record the real output as a fixture, and only then design the client.
- Phase 3 starts by splitting `viewNewRun` into step functions with no behaviour change (item 2).
- Nothing in the plan needs to be dropped; the order stays the same.

### Not started
No implementation work has begun. Waiting for owner approval and answers to the section 9 questions.

## 11. Phase 1 status (2026-10-15) - implemented, not yet committed

- `scraper/connection_health.py`: `is_unhealthy()` / `is_checked()` - the single health rule (unknown = usable). `jobs.preflight`, `places`, `search` and `/api/connections/status` all use it; the status endpoint now returns `checked` and `cost` per service, and the banner says "not checked yet" instead of FAIL for an unprobed service.
- `scraper/config.py`: `free` now means "needs no account"; new `cost` class per service (`free` / `key` / `paid`). Bright Data was wrongly `free: True`; now paid.
- `scraper/capabilities.py` (new): `service_state`, `check_depth`, `fields_for`, `run_options`; `DEEP_SERVICES` and `PLANNED` (designed-but-not-built providers are listed as planned only).
- `GET /api/run-options?mode=&depth=` (member/admin); never returns key values.
- `build_job_spec`: accepts `depth` (`standard` default, `deep`); `deep` with no usable paid service is refused with a reason. Depth is recorded on the spec only - it does not yet change what a run does.
- Tests: `tests/test_capabilities.py` (44 checks). Full Python suite and both JS suites pass.
- Decisions taken without the owner (section 9): Paid = the person's own keys; Deep allowed only when a paid service is usable; Deep for web/places/enrich currently means "AI connector present" until Phase 2 adds real providers.

### Phase 1 review fix (2026-10-15)
`GET /api/admin/connections` and the Admin connections table still used the old "unprobed = FAIL" default. Both now use the shared health rule (`checked` flag; the table shows "not checked").

## 12. Phase 2 status (2026-10-15) - implemented, not yet committed

Scope delivered: two paid Apify providers behind the registry, wired into Deep Places and Enrich runs, spend-capped.

- `scraper/apify_actors.py` (new): generic `run_actor` (sync endpoint, readable errors, token never echoed), and two actor entries:
  - **places** `compass/crawler-google-places` - input `searchStringsArray`, `locationQuery`, `maxCrawledPlacesPerSearch`, `scrapeContacts`; normalised to our Places columns (phone, website, address, hours, rating, optional emails).
  - **contacts** `vdrmota/contact-info-scraper` - input `startUrls`, `maxRequestsPerStartUrl`, `mergeContacts`, `proxyConfig`; normalised per website host.
  - Input fields and prices were read from the Apify Store (FREE-plan tier: $0.004/place, +$0.002/place for contacts; $0.002/page for the contact scraper). **Output field names are not confirmed against a live run** - every normaliser is tolerant. A live spike with the owner's token is still outstanding (see below).
- Registry: `apify_places`, `apify_contacts` (cost `paid`, key required, **off by default** - an admin enables them in Admin -> Connections, like the social providers; they share the `apify` health result via `health_as`).
- `capabilities.py`: Deep for Places = Apify places or AI; for Enrich = Apify contacts or AI; new fields (rating); the "planned" list for places is now empty (still planned for web crawler and email verification).
- `jobs.preflight` returns `paid_places` / `paid_contacts` (only for `depth=deep`, with a token, switch on, Apify not known-broken).
- Runs: Deep Places adds Google Maps rows to the free map rows (merged by the existing duplicate merger, so the same place is one row and gaps are filled); names-only Deep asks without the contacts add-on and labels rows "Google Maps". Deep Enrich runs the free crawl first and pays only for sites that still have no contact.
- Budget: `build_job_spec` returns 402 when a Deep run that would use a paid actor has no monthly budget or could exceed it; the run re-checks before every paid call and stops cleanly (free rows kept). Uses the existing monthly budget (`social_budget_*`) as one pot for all paid actor spend; audit entries are labelled `paid.places` / `paid.contacts`.
- Token only enters the spec for Deep Places/Enrich runs and is stripped from the public spec.
- Tests: `tests/test_paid_actors.py` (66 checks, mocked HTTP). Full Python and JS suites pass.

### Still open after Phase 2
1. **Live spike**: one real call to each actor with the owner's Apify token, to record true output and fix any normaliser mismatch. Not done - it spends the owner's money, so it needs their go-ahead and token.
2. The shared budget is still *named* social in settings and the admin UI; a rename/split needs a UI change (Phase 4).
3. Web mode has no paid provider yet (Apify website crawler stays planned).
4. Email verification (planned) is not built.

### Phase 2 review fix (2026-10-15)
Paid Google Maps rows were being relabelled "Map" in the Website Source column by the Places enrich step; they now keep "Google Maps". Known limit: a paid row with no coordinates cannot be matched to an OSM row by distance, so it can appear as a second row for the same place (the merger needs coordinates).

## 13. Phase 3 status (2026-10-15) - implemented, not yet committed

What changed in the UI (static/app.js, static/app.css, templates/index.html):
- **`#/new` chooser (new)**: step 1 pick **Standard (free)** or **Deep (paid tools)** - the Deep card says live which kinds of run it is ready for; step 2 pick what to collect. Every "New run" button now opens it; the Home quick cards still go straight to a form (Standard).
- **Form route** is now `#/new/<mode>/<standard|deep>`; `#/new/<mode>` still works and means Standard. Depth is remembered for the session.
- **Run depth + setup checklist** on every form, from `GET /api/run-options`: Ready / Optional / Needs setup rows with a link to fix each (maps, search, AI, each paid provider, the monthly budget).
- **Dynamic form**: web detail checkboxes with no usable source are greyed with the reason; Start is disabled with the blocker listed in the summary when a Deep run can't work; summary shows depth and, for Deep Places, the worst-case paid cost (the same figure the server gates on).
- **Social**: Standard runs are free-lookup-only (checkbox locked on); Deep unlocks paid lookups. This changes the default for direct `#/new/social` links - they used to allow paid lookups by default.
- Server: `run_options` gained `blockers`, `budget`, `pricing`, `paid_will_run`, per-provider setup rows; `can_start` is now `not blockers`.
- The existing three forms were NOT restructured: the new pieces are separate functions (`setupCard`, `viewNewChooser`) and the old form code is untouched apart from small hooks. A full per-step rewrite was judged riskier than useful.
- Checked live in the browser pane (desktop and 375px): chooser, Standard/Deep forms, social lock, Deep Places with token+switch+budget (estimate $1.80 for 300 places; Start enabled). No horizontal overflow at 375px.
- Tests: `test_capabilities.py` (+7 checks), `test_frontend_static.py` (+1 test). Full Python and JS suites pass.

Not done in Phase 3 (belongs to Phase 4/5): menu/Settings reorder, wizard-style steps (the form is one page with a checklist on top, not paged), review-screen polish, result quality columns, renaming the shared "social" budget.

## 14. Phase 4 status (2026-10-15) - implemented, not yet committed

- **Found and closed a gap from Phase 2**: Settings had no place to enter an Apify token or Bright Data key (only admins could share them as org keys), so the "Add your token in Settings" instructions led nowhere. New **Paid data** card in Settings with both fields, saved-key masks, Remove, and a **Test** button for each (`POST /api/settings/key-test`; never returns or echoes the key; an unexpected failure is a readable message).
- **Settings reordered** to the planned order: Connections overview -> Keys (Brave) -> Paid data (Apify, Bright Data) -> AI provider -> Directory sources -> social logins (if enabled) -> data on this device / install / password / appearance. A jump bar (Connections, Keys, AI, Sources, Account) scrolls to each section.
- **Connections overview (new, first card)**: every service in three groups - Free sources, With your own key, Paid tools - each with one honest status: OK, Not checked, Failing (with the reason), Needs key, or Off (admin). Same health rule as the run screen.
- **Deep links**: `#/settings/<connections|keys|ai|sources|account>`. The run screen's "Fix this" buttons, the Home AI prompt, the Brave nudges and the directory-source hint now open the right section (the jump waits for the connections list to fill in, otherwise the page shifts after scrolling).
- **Menu**: sidebar group "Account" is now **Setup**, with "Connections & settings"; page title and breadcrumb match. (New run stays the top button; Home, Runs, Sheets, Shared, Tools unchanged.)
- **Budget renamed in the UI** to "paid data": run-screen banner ("Paid data: ..."), admin labels ("Monthly paid-data budget"), and the hint now says one budget covers Google Maps, website contacts and social lookups. Storage keys (`social_budget_*`) are unchanged - no migration.
- Mobile: no horizontal overflow and no sub-36px controls on the new Settings page at 375px; fixed two CSS specificity slips found while checking (jump bar and preset buttons).
- Tests: `test_capabilities.py` (+12 checks: key-test endpoint, masks, deep links), `test_frontend_static.py` (+1 test incl. section order). Full Python and JS suites pass.

Not done: a paged step-by-step wizard for the run form (still one page with the checklist), a separate paid-data budget from the social one, result-quality columns (Phase 5), per-user health for Apify/Bright Data tokens (the health cache is shared across users, as it already was for Brave).

## 15. Phase 5 status (2026-10-18) - implemented, not yet committed

- **Email quality** (`scraper/quality.py`): Places, web and enrich runs gain **Email Quality** ("High/Medium/Low - reason") and **Last Checked**; Places also gains **Data Source** (OpenStreetMap / Google Maps (paid)). Checks are free and never contact a mail server: free-mail vs the company's own domain, domain match with the website, MX lookup (real, via `dnspython`; without it the check degrades to "the domain exists" and says so), shared inbox (info@...). A dead domain or malformed address is Low; free mail or a different domain is Medium; the company's own domain with a mail server is High. Time-boxed (max 300 domains, 25 s), failures never fail a run, names-only and social runs are untouched. `QUALITY_DNS=0` (or the test suite's `SKIP_STARTUP_PROBES=1`) switches lookups off.
- Verified against real DNS: gmail.com and iiit.ac.in -> mail server found; a made-up domain -> none; example.com -> none (it publishes a null MX).
- New dependency: `dnspython>=2.4` in requirements.txt (Render installs it on the next deploy).
- Two existing tests asserted exact result columns; updated to the new, intended columns.
- Cleanup: README now documents Standard/Deep runs, the budget, the Email Quality column, the new tests and modules. The old single-page form was kept (decided in Phase 3), so there was nothing legacy left to remove.
- Tests: `tests/test_quality.py` (31 checks, no network). Full Python and JS suites pass.

### Remaining / open
1. **Live Apify spike** with the owner's token (still not done - spends the owner's money).
2. Row-level confidence for fields other than email (phone, website) is not rated yet.
3. A separate paid-data budget (it still shares the `social_budget_*` setting).
4. Paid web crawler and email verification providers remain planned, not built.
5. A paid Google Maps row with no coordinates can duplicate an OSM row.
