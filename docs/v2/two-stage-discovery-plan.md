# Two-Stage Company Discovery + Fill Missing Fields — Plan

**Version:** 1.0-draft
**Date:** 2026-10-14
**Status:** IMPLEMENTED — approved and coded, all tests green (see two-stage-verification below).
**Depends on:** Phases 1–5 complete (all suites green)

---

## 1. Objective

One simple, resilient workflow for non-technical users, in two stages:

1. **Stage 1 – Get company names.** A fast run that saves *only names* into a sheet
   immediately, as they are found. If the internet drops, the names are already kept.
2. **Stage 2 – Fill missing details.** One clear button on any names sheet. A background
   job fills empty website / email / phone cells only, never overwrites, and marks each
   row's Status (`Enriched`, `No website found`, `Partial`).

Non-negotiable: company data only (contacts taken solely from the company's own
website, never invented, never personal); free sources by default; robots.txt and
rate limits respected throughout.

---

## 2. What already exists (reuse, don't rebuild)

| Need | Existing piece |
|------|----------------|
| Names from the web without visiting pages | `_expand_listings()` in `scraper/jobs.py` (listing → Name/Address/link rows) |
| Names from the map without websites | Places mode with enrich off (`_run_places`) |
| Website guess + verification without search engines | `discover.guess_website()` |
| Website search + contact-page crawl | `_discover_websites()` + `_enrich_place()` / `_enrich_web_row()` |
| Company-domain email preference | `_own_emails()` (own domain first, free-mail only as fallback) |
| Re-runnable blank-only enrichment | `_run_enrich()` (skips rows that already have contacts, capped per run) |
| Live progress, stop-keeps-rows, job log | `Job` + Tracker + `/api/jobs/<id>` (unchanged) |
| Sheets in IndexedDB, CSV/XLSX export | `Store` / `sheetops.js` (unchanged) |

## 3. Gaps to close

1. **No names-only mode.** Web always visits pages; Places website lookup can't be
   skipped independently of enrichment.
2. **Nothing is saved until a run finishes.** A dropped connection loses everything,
   including names already found.
3. **Enrichment requires a `Name` column.** Imported CSVs say `Company Name`.
4. **No Status column.** Neither discovery nor enrichment records per-row state.
5. **No selected-rows enrichment.** `_run_enrich` takes every contact-less row.
6. **Contact rule is implicit.** Company-only sourcing needs stating in UI copy and one
   explicit guard (see §4.4).

## 4. Design

### 4.1 Discovery: a `names_only` flag on web + places (no new job system)

- New-run form gains one checkbox: *“Collect company names only (recommended for
  large searches)”* + helper *“We will save the names first. You can add websites
  and contact details later.”*
- `POST /api/jobs` accepts `names_only: true` for `web` and `places`.
- Web + `names_only`: search + listing expansion run as today, but page visiting
  (`_process_page`) is skipped. Each search hit and each listing entry becomes a row
  directly: **Company Name | Source | Location / Search | Status=`Name only`**.
  Source = `Search: <query>` or the directory name/URL; Location/Search = the query
  or location string.
- Places + `names_only`: map search runs as today, website guessing, website search
  and contact visiting are all skipped. Same four columns.
- Discovery still respects robots.txt, per-domain delays and engine pacing — it only
  does *less* work, so it is strictly faster and kinder than full runs.

### 4.2 Continuous saving (frontend, small change)

- When a `names_only` run starts, the browser creates its sheet immediately (empty,
  correct columns) instead of on finish.
- On each Tracker progress tick, new preview rows are appended to that sheet
  (dedupe by Company Name, case-insensitive). Stopping the run keeps everything
  saved so far — the sheet *is* the progress.
- Message during run: *“Saving company names as we find them… You can stop anytime
  and continue later.”*
- No backend change: preview rows already stream via `to_dict()`.

### 4.3 Fill Missing Details (backend upgrades + one button)

- Sheet button *“Fill missing details”*, visible when a `Company Name` column exists
  and at least one row lacks website/email/phone. Empty-names-only state shows:
  *“This sheet has company names. Click ‘Fill missing details’ to find websites
  and contact information.”*
- Confirmation dialog (exact copy in §6), then `POST /api/jobs` with
  `mode: "enrich"` extended by:
  - **`name_column`**: `"Company Name"` accepted as an alias of `Name` (server maps
    it; validation error only if neither exists).
  - **`status_column`** (optional, default `"Status"`): server writes `Enriched`
    (website + at least one contact), `Partial` (website but no contacts, or contacts
    but no website), `No website found` (neither). Created if missing.
  - **`row_ids`** (optional): enrich only these rows (filtered/selected batches).
    Omitted = all contact-less rows, as today. Unattempted rows are returned untouched.
- Per-row pipeline per attempted row (existing pieces, same order): domain-guess →
  web-search website → contact-page crawl → fill blanks only → status.
- Progress: existing live counts; header line *“Looking for websites and
  contacts… X companies still need details”*; finish line *“All done – X companies
  now have contact details”*. Stop keeps filled rows (already true).

### 4.4 Company-only contact rule (explicit)

- Contacts are taken **only from pages on the company's own website** (already true
  for every enrich path: guess/verify, official-site search, contact-page crawl).
- `._own_emails()` preference stays: company-domain first; well-known free-mail
  addresses kept **only** when the site publishes no company-domain address (small
  businesses genuinely use Gmail), and the row's Source notes it.
- Never fabricated, never taken from third-party/directory pages, never personal
  mobiles: bare-digit runs are already rejected by `_looks_like_bare_phone()`.
- Import path unchanged: any CSV with a `Company Name` column parses via the existing
  importer and enriches identically.

### 4.5 Log

- No new log system: the run log already records what was tried per place
  (“Found by web search”, “Guessed from the name and verified”, “no official website
  found”). The sheet's **Source** column + run log jointly answer “what we found and
  where it came from”. The run-detail Log tab stays the one place to open it.

## 5. Files to touch

| File | Change |
|------|--------|
| `app.py` | `names_only` in job creation; enrich accepts `name_column`, `status_column`, `row_ids` |
| `scraper/jobs.py` | Skip visiting/lookup in `names_only`; discovery columns; enrich status writes + row subset |
| `scraper/config.py` | Discovery/enrich column constants (single source of truth) |
| `static/app.js` | Toggle + helper copy; sheet button + empty state; confirm dialog; live messages; immediate sheet create + append |
| `tests/test_two_stage.py` | NEW — mocked, no network (see §7) |
| `tests/test_api.py` | Untouched |
| `docs/` | Short user guide for the two-stage flow |

No template rewrite, no new dependencies, no schema change (sheets stay in IndexedDB).

## 6. UI copy (exact strings, non-technical)

- Toggle: *“Collect company names only (recommended for large searches)”* / helper:
  *“We will save the names first. You can add websites and contact details later.”*
- Running: *“Saving names…”* · *“Looking for websites and contacts…”* ·
  *“X companies still need details”* · done: *“All done – X companies now have
  contact details”*.
- Sheet button: *“Fill missing details”*.
- Confirm: *“We will search for the missing website, email and phone numbers for
  these companies. This may take some time. You can stop and continue later.”*
- Names-only empty state: *“This sheet has company names. Click ‘Fill missing
  details’ to find websites and contact information.”*

## 7. Testing plan (mocked, no network)

1. Names-only web: searches run, zero page visits, four discovery columns, every row
   `Status=Name only`.
2. Names-only places: no website lookup calls, same columns.
3. Enrich on `Company Name` sheet: fills blanks, keeps existing cells, writes Status
   values across all three outcomes.
4. Enrich with `row_ids`: unlisted rows byte-identical before/after.
5. Re-run safety: second enrich changes nothing.
6. Company-only rule: third-party emails never kept; free-mail kept only with no
   company-domain alternative (unit-level).
7. Imported names-only CSV enriches end to end.
8. All standing suites green; `py_compile` + `node --check` clean.

## 8. Acceptance criteria

- [x] A names-only run yields a four-column sheet without visiting pages or looking
  up websites.
- [x] Stopping mid-discovery keeps every name found so far.
- [x] One button enriches a `Company Name` sheet: blanks filled, existing kept, Status
  accurate, selected-rows respected.
- [x] No personal/third-party contacts in any output (asserted).
- [x] Every message on screen matches §6 plain-language copy.
- [x] All suites green.

Verified in `tests/test_two_stage.py` (27/27) plus the standing suites, all re-run
green after the change. One real bug caught by the tests before shipping: the
discovery loop read `hit["href"]` where `_search()` yields `hit["url"]`, which would
have produced empty sheets — fixed and covered.

## Relevance follow-up (user report: dictionary junk for a coaching-centers query)

- Root cause: the old keep-rule accepted any result mentioning *any single* signal
  word, so dictionary definitions sharing the word "digital" passed.
- Fix in `scraper/jobs.py`: `_on_topic` untouched (still the directory-confirmation
  check); new `_keep_hit` requires a topic word in title/domain plus a place word or
  directory confirmation, drops definition-titled pages, keeps trusted-TLD and
  no-place-query behavior. All prior relevance tests in `tests/test_places.py` pass
  unchanged, plus 6 new regression tests for the reported case.
- Two prior enrich tests were updated to the approved two-stage semantics (rows
  missing a website count as needing work; Status column appended).

## Relevance follow-up, part 2 (user report: aggregator hub kept as a company row)

- A GitHub Topics hub passed on one shared title word plus directory-area
  confirmation. Fix: aggregator-shaped URLs (tag/topic hubs, on-site search, numbered
  top-N lists, list indexes) join the directory bucket in `_search` — expanded when a
  parser exists, otherwise counted and dropped, never kept as rows. Company service
  pages, homepages and profile URLs are explicitly not matched. Covered by 8 new
  tests; every prior relevance test still passes unchanged.

## Directory reality check (same report: "100s of companies in a single website")

- JustDial serves an empty bot-wall shell (`<HTML></HTML>`) to server-side fetches,
  so no JustDial parser can work from here; a guessed Sulekha URL 404'd. Per the
  no-guessed-parsers rule, neither was added — they stay visible as counted skipped
  directories, and their names still surface via official-site hits.
- Coaching centers had no Places category at all. Added **Coaching & training
  centers** (`office=educational_institution`) after live verification against
  Ameerpet; the `amenity=training`-family tags returned nothing and were not added.
  *(Superseded - see "Recall fixes" below: the first version also put a name regex on
  the tag, which turned out to hide most real centers.)*

## 8b. Recall fixes: companies that were silently missing

Reported on a live "coaching centres in Hyderabad" run: real centers were absent. Three
separate causes, each checked against the code and live map data before changing anything:

1. **Map path.** The Coaching category matched names containing
   `coach|train|tutor|tuition|academy|institute`. In an 8 km box around Ameerpet that kept
   6 of 32 named `office=educational_institution` entries and missed Naresh iT, Zoom
   Technologies, Omni Techmasters, Satya Technologies, Qkoncept Learning, Skill Kshetra -
   names that say nothing about coaching. Now the tag is queried with no name regex, and
   ordinary schools/junior colleges (about 10 of the 26 previously-missed entries were high
   schools) are dropped by name afterwards via `places.NAME_RULES` (a school word loses to a
   coaching word: "Allen Coaching School" stays, "Brilliant Grammar High School" goes).
   Engineering colleges use the same mechanism now. Because some of what the map returns is
   thrown away, the query asks for up to 3x the limit (cap 1500) and trims back afterwards.
2. **Web path - spelling and word forms.** Topic words were compared as exact whole words:
   `centres` never equalled `centers`, `Coach` never equalled `coaching`, `Institutes` never
   `institute`. `scraper/topic.py` now stems both sides (plurals, `-ing`, British/American
   spellings). Spelling alone was a narrow cause (one matching topic word was always enough,
   so any title containing "Coaching" passed either way); the missing word-form handling was
   the larger one.
3. **Web path - vocabulary.** A center titled "Naresh IT - Software Training Institute" shares
   no word with "coaching centers". When only collecting names (`names_only`), a query's
   category now also accepts its usual synonyms (`topic.SYNONYM_GROUPS`: coaching accepts
   training, tuition, tutorial, academy, institute; likewise colleges, schools, hospitals,
   restaurants, gyms, salons, temples, builders). Full runs keep the exact (stemmed) rule.
   A topic word is still required - dropping it was considered and rejected: a wrong row is
   not free even when only collecting names, because Stage 2 spends up to three searches
   per name and the 80-lookup cap on it.

Also found while doing this: filler words ("best", "top", "contact", "phone"...) counted as
topic or place evidence, so "Best Biryani in Hyderabad" passed a "best coaching centers"
query and any page saying "contact" passed the place check. They no longer count
(`topic.NOISE`; if a query is made only of them they are kept rather than matching nothing).

Opt-in: **"Also search similar wordings"** on the web form adds up to 4 extra searches per
query ("training institutes in Hyderabad", "tuition centers in Hyderabad"...) capped at 40 per
run (`topic.expand_query`, `jobs.MAX_EXPANDED_QUERIES`). It is a toggle because it multiplies
search time (about 5x without a Brave key). Names-only runs now also log up to five titles
they skipped per search, so a miss is visible instead of silent.

Not shipped, deliberately: `amenity=prep_school` and `amenity=language_school`. The first
timed out on every Overpass mirror, so it is unverified; the second returns mostly mis-tagged
ordinary schools. Both stay out until a clean live check says otherwise.

## 9. Risks + rollback

- Discovery reuses listing/search paths, so odd queries yield odd names — same as
  today, and the user sees names immediately to judge.
- Enrichment output shape gains an optional Status column; old sheets without it get
  it added, never renamed. Rollback: `names_only` off + old enrich path untouched
  (flag-gated additions only).

## 10. Approval checklist

- [x] Confirm the four discovery columns and three Status values.
- [x] Confirm `Company Name` alias (vs renaming everything to it).
- [x] Confirm free-mail fallback with Source note (vs dropping free-mail entirely).
- [x] Confirm selected-rows via `row_ids` (vs whole-sheet only).
- [x] Authorise implementation.

**Reply with `approved` + any edits, and I will implement exactly this plan.**
