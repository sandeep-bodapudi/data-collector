# OneBridge Data Collector

An internal tool for our team. We collect **public** information from the web into spreadsheets, clean them up (merge, dedupe, export), and **deliver them to customers** such as an overseas education company that asked for student data. No logins to the target websites are needed. It is an **installable app (PWA)** and keeps working with your saved sheets when you're offline.

- **Web search:** emails, phones, addresses, social links and page details from websites matching your searches. Optional AI columns fill any detail you describe, using **each person's own AI key** (OpenAI, Gemini, Claude, OpenRouter, Groq or any OpenAI-compatible service).
- **Places:** lists of temples, hospitals, schools, hotels, banks… in any city, from OpenStreetMap.
- **Sheets:** every run saves a sheet. You can search it, open single rows, and export it as Excel, CSV or JSON. Excel, CSV and JSON files can also be imported.
- **Merge & Dedupe:** combine sheets and remove duplicates using smart matching for phones, emails and websites.
- **Send to a customer:** press **Share** on a sheet and choose *Customer outside the company*. The customer gets a private, branded page (no account needed) where they download Excel or CSV. Add an optional passcode (sent separately), a short message and an expiry. Send the link from Gmail, your email app, WhatsApp, Telegram or any app on your phone. The sender sees when it was opened and downloaded.
- **Share with colleagues:** the same dialog's *Colleagues* tab shares a copy with chosen people, or anyone signed in with the link.
- **Admin:** users with Admin, Member and Viewer roles, the connector policy, and a list of every customer link that is active right now (who sent it, to whom, whether it was opened) with a Stop button.

Product requirements: [docs/OneBridge Scraper_ Product Requirements Document.md](docs/OneBridge%20Scraper_%20Product%20Requirements%20Document.md). What's built so far: [docs/PRD-status.md](docs/PRD-status.md).

## Where data is stored

| What | Where | Why it matters |
|---|---|---|
| User accounts, roles, saved AI keys (encrypted) | **Database** (PostgreSQL on Render) | Small; this is all the database holds. |
| Sheets and run history | **Each person's browser (IndexedDB)** | Never uploaded to the server. They don't appear on other computers, and are lost if the person clears their browser data. **Settings → Data on this device** offers backup and restore. |
| Shared copies and customer links | **Database** (`shares` table), only when someone presses Share | An explicit snapshot, never a live link. Compressed, capped at 5 MB per sheet, 25 active per person and 200 MB in total (`SHARE_MAX_TOTAL_MB`). Deleted automatically when it expires (1, 7 or 30 days) or the owner stops sharing. Not encrypted inside the database, so share only what colleagues may see. |
| Live runs | Server memory, only until the browser collects the rows | A server restart during a run loses that run. |

IndexedDB is kept separately for each signed-in user, so people sharing a computer never see each other's sheets.

## Run it locally (Windows)

Double-click **`start.bat`** and open http://localhost:5000.

On first start you create the admin account. Add your team under **Admin**.

**Database.** Set `DATABASE_URL` in `.env`. The XAMPP MySQL example is in `.env.example`. Without it, the app uses `instance/data.db` (SQLite).

## Run it on a server (Render)

[render.yaml](render.yaml) holds the build and start commands. Keep `--workers 1`: live runs are tracked in memory.

Environment variables (set them in the Render dashboard, never in git):

| Variable | Why |
|---|---|
| `SECRET_KEY` | A long random value. **Never change it**, because it signs logins and encrypts saved AI keys. |
| `APP_USER`, `APP_PASSWORD` | The first admin, created when the database has no users. |
| `DATABASE_URL` | The **Internal Database URL** of a Render PostgreSQL database. **Without it, users are lost on every restart.** If it can't be reached, the app falls back to a temporary SQLite file so the site stays up, and Admin shows a warning. |
| `COMPANY_NAME` | Optional. Shown on the customer page and in the messages (default "OneBridge Infotech"). |
| `SHARE_MAX_TOTAL_MB` | Optional. Total space for shared copies in the database (default 200). |
| `BRAVE_API_KEY` | Optional, but recommended. Free tier 2,000 queries/month. Free search engines often block cloud servers. |
| `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX` | Optional. A second, independent free search option (100 queries/day), tried after Brave and before the free engines. |
| `DISCOVER_MAX_PER_RUN`, `DISCOVER_GAP_SECONDS`, `DISCOVER_RETRY_WAIT`, `DISCOVER_KEYED_GAP_SECONDS` | Optional. Tune how hard **Places** and **Search the web** pace requests to search engines. See *Scaling up* below. |
| `HTTP_PROXY`, `HTTPS_PROXY`, `DDGS_PROXY` | Optional. Route outgoing requests through your own proxy (see *Scaling up*). |

**Free Render PostgreSQL expires 30 days after it is created** (with a short grace period to upgrade). Because it only holds users and saved AI keys, losing it is recoverable: the admin account is recreated from `APP_USER` / `APP_PASSWORD`, and people re-add their AI keys. For a database that doesn't expire, use a paid Render database or a free external PostgreSQL such as Neon or Supabase, and put its URL in `DATABASE_URL`.

## Installing as an app

Open the site in Chrome or Edge and use the **Install app** button in the top bar (or the install icon in the address bar). On iPhone/iPad: Share → Add to Home Screen. Settings → *Install the app* shows the right instructions for the current browser.

How it works offline: a service worker ([templates/sw.js](templates/sw.js)) caches the app's own files and the last signed-in page. API calls and the login page are never cached. Starting a run, importing a file and exporting to Excel need a connection; browsing, searching, merging and CSV/JSON export of saved sheets work offline.

The logo is [static/logo.svg](static/logo.svg) (a vector trace of the OneBridge logo). App icons are in `static/icons/`.

## Tests

```
python tests/test_api.py        # backend: login, roles, PWA files, runs, import/export, sharing, database fallback
python tests/test_places.py     # place search: finding the area, duplicate merging, website lookup (no network needed)
node tests/sheetops.test.js     # browser logic: merge & dedupe, CSV/JSON export
```

## Finding places with their contacts (for example, engineering colleges in an area)

1. **New run → Places.** Pick a category (Education → *Engineering colleges*), and in *Where* type the area and city, such as `Bachupally, Hyderabad`.
2. Keep **Find emails & phone numbers** and **Look up websites the map doesn't list** switched on, then start the run.

How it works:

- The list of places comes from OpenStreetMap, a community-edited map. It knows names and positions but almost never phone numbers, emails or websites (measured on all 149 "Colleges & universities" in Hyderabad: 1 had a phone, 1 an email, 3 a website — straight map data alone is not usable for this). It also may not list every place. The public map servers are sometimes slow, so a search can take a few minutes.
- The area is matched as a whole area where possible. Say `Bachupally, Hyderabad` or just `Bachupally`: the run log's first line shows exactly which area was searched. If a name matches only one business, the app searches about 3 km around it and says so.
- **One search already covers every mandal and village inside the area, in a single pass** — the map query runs against the whole matched boundary (district, city, state…), not a point, so it doesn't need to be split into one search per village to be thorough. The thing that actually limits coverage is *which* area you give it: `Hyderabad, India` matches the Hyderabad district/GHMC boundary only — it does **not** include Bachupally, Kukatpally's outer edge, or most of the suburban colleges people call "Hyderabad", because those sit in the neighbouring Medchal-Malkajgiri, Rangareddy or Sangareddy districts (this is exactly why `Bachupally, Hyderabad` needs its own fallback match, above). For the broadest possible single run, search `Telangana, India` — the whole state's boundary, which recurses through every district, mandal and village in one query — or add Hyderabad, Medchal-Malkajgiri, Rangareddy and Sangareddy as separate locations in the same run (duplicates across them are merged automatically).
- The Places sheet has only real data: Name, Phone, Email, Website, Address and a map link (rows with contacts first). Coordinates, how a website was found and fetch problems are in the run log, not the sheet. "Engineering colleges" now matches any college/institute/university in the area and drops junior, medical, pharmacy, law and similar ones, so colleges without "engineering" in the map name (e.g. "CMR College") are included.
- To get contacts, the app first tries the address a college normally has (its initials + `.ac.in`, `.edu.in`, `.in`, …, e.g. `griet.ac.in`) and keeps it only if the page really is that college's — no search engine involved, so it isn't blocked or paced. Only places that fails for are searched on the web (see the table below). Contacts are then read from the site and its contact pages: emails are limited to the college's own domain (plus Gmail/Yahoo-style), at most 3 phone numbers, and the address is filled from the site when the map has none.
- **The real ceiling on how many colleges a search returns is how many are tagged in OpenStreetMap, not the area searched.** Even `Telangana, India` in one run won't return anywhere near all ~1000 AICTE-approved engineering colleges in the state, because most simply aren't mapped there yet (measured: only 149 "Colleges & universities" of any kind are tagged for all of Hyderabad). Reaching that full count needs an authoritative seed list (e.g. AICTE's or JNTUH's own published list of approved institutions) fed through the same website-lookup and contact-scraping pipeline — ask about adding a "enrich a list of names" mode for this.
- The same place often appears twice on the map under different names, or an abbreviation of one ("griet college" / "GRIET COLLEGE", "vnr vjiet college" / "VNR Vignana Jyothi Institute of Engineering and Technology"). These are merged automatically; the other name is noted in *Other Details*.

**Scaling this up — what's realistic for free, and where it stops being free.** The real bottleneck for volume is the website lookup, because it's a web search per place, and search engines actively detect and slow down scripted use:

| Setup | What's realistic | Why |
|---|---|---|
| No key (default) | ~80 places/run, a few hundred/day in short bursts | Free search engines (DuckDuckGo, Yahoo, Brave, Google, Mojeek, Startpage, tried through the `ddgs` library) work, but in testing, querying faster than about one every 5 seconds made *every* engine start refusing within a handful of requests — not occasionally, consistently. The app already paces itself this way and retries once; `DISCOVER_MAX_PER_RUN` / `DISCOVER_GAP_SECONDS` / `DISCOVER_RETRY_WAIT` are there to tune it, but pushing the gap down reliably made things worse in measurement, not better. |
| `BRAVE_API_KEY` (free, 2,000/month) | ~65/day sustainable, instantly, no pacing needed | An official API with its own quota instead of being guessed at and throttled by IP. |
| `GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX` (free, 100/day) | A second ~100/day on top of Brave | Also official; used automatically after Brave, before the free engines. Together, two free keys cover roughly 170 reliable lookups/day with no pacing delay — a few thousand a month. |
| Running from an office PC instead of the cloud (`start.bat`) | No hard cap, limited mainly by patience | Free Render sleeps the app after ~15 minutes idle, which kills a long-running "Places" job partway through. A PC that stays on doesn't have that problem, and office/residential IPs are generally throttled less aggressively than cloud datacenter IPs for the free engines. |

**For 10,000+ clean rows:** that's genuinely a multi-hour-to-multi-day job even with both free keys, because it's bounded by lookups/day, not by anything in this app. Realistic free path: run it in batches across many areas over several days (locations can already be queued as a list in one job), ideally from a PC that stays on. The two free keys above remove the pacing entirely for the first ~170/day; beyond that it falls back to the paced free engines. There's no setting that makes thousands of accurate, verified website lookups happen in minutes without paying for query volume somewhere — that's a limit of the free search providers themselves, not of this code.

- If a place shows no contacts, its website either wasn't found or doesn't publish them as text — not every institution does.
- For a wider list, search a larger or neighbouring area, or use **Search the web** with a query such as `engineering colleges in Bachupally Hyderabad contact email phone`.

## Scaling "Search the web" to hundreds of searches

"Search the web" sheets contain only real data: **Name** (the organisation's name read from its site, not the search-result title), the details you ticked (Emails, Phone Numbers, Address…) and **Website**. Search queries, snippets and fetch status stay in the run log. Directory/aggregator pages (Collegedunia, Shiksha, Justdial, Wikipedia, social sites…) are skipped as a *page* - they list many places, not one - because a generic "find the list on this page" rule was tried against real directory pages and produced unreliable results (real college names tangled up with site navigation, course-category links, even other people's unrelated profiles on a general directory like Sulekha). **A handful of specific, high-value directories have a dedicated parser instead** ([scraper/listings.py](scraper/listings.py) - currently colleges9.in), matched to that exact site's real markup: those pages are expanded into one row per real name found (plus its address, when the listing shows one), rather than being skipped or kept as a single row for the whole page. Names already found from an official site elsewhere in the same run aren't duplicated from a listing. **Each college’s own profile page is read first.** colleges9.in keeps the phone number, email, official website and address on a separate page for every college (the category listing only has the name and address). Those pages are read for every listed college, in parallel, before anything else: the phone and email come from there, and the profile’s official website is used directly, so the free guess and the search are skipped for every college whose profile names one. **Each name is then enriched automatically** - the same two-step, search-engine-aware lookup Places mode uses: first a free guess at the institution's likely address (e.g. `name.ac.in`), verified by reading the page, no search engine involved at all; then, only for whatever's left, a paced web search (capped per run, same as Places, so this can't hammer the free engines for hundreds of names at once). A name nothing resolves for keeps the directory's own profile link as its Website, with no contacts invented. Adding another directory means writing a parser for its specific markup, not guessing generically - ask if there's a specific one you want covered. For a complete list of places in an area without depending on any directory's markup at all, **Places** mode is the more reliable tool: it starts from OpenStreetMap's own data instead of however many sites a search engine returns.

**"Fill missing details"** (a button on any saved sheet that has a Name column and an Emails/Phone column with blanks in it) runs that same two-step lookup directly on an *existing* sheet - useful after a big "Known directories" or batch run leaves many rows with a name but no contacts yet. It only looks up rows that are still blank; a row that already has an email or phone is left completely untouched, never looked up again. **Capped at the same `DISCOVER_MAX_PER_RUN` (80 by default) per click, on purpose** - press it again on the result and it picks up exactly where it left off, since the rows it already filled no longer qualify for another lookup. A large sheet (e.g. the 370-college Telangana seed) is realistically a handful of clicks over a few minutes, not one very long run that free search engines are more likely to start refusing partway through. A name nothing resolves for just stays blank - press the button again later, or add a search key, rather than the run inventing something.

**Why a lookup can feel slow, and what actually helps.** Real log investigated: a lookup for one name was taking close to a minute. Not JS rendering (this app doesn't use a headless browser at all - no Playwright/Selenium anywhere in it, checked and confirmed) - the cause is the free-engine fallback itself. Looking up one name with no match yet can try up to 3 internal searches, each of which can try up to 6 free engines in turn before giving up; when an engine is slow or half-blocking rather than cleanly refusing, every one of those attempts can sit near its full timeout before moving to the next - worst case, over a dozen several-second waits stacked up for a single name. Two real fixes, already in effect:
- The free-engine timeout itself was uncapped (the `ddgs` library's own default, 5s); now bounded and tunable (`FREE_ENGINE_TIMEOUT_SECONDS`, default 4s) so a dead/slow engine can't eat the full budget on every single attempt.
- The 5-second pause *between* a lookup's own internal search attempts was fixed regardless of whether a key was configured - even with a fast official API answering in a second, it still waited the full free-engine-safe gap. It's now key-aware like everywhere else: ~1 second instead of 5 once `BRAVE_API_KEY` or `GOOGLE_CSE_KEY`/`GOOGLE_CSE_CX` is set.

The free guess step (no search engine at all, see above) is unaffected by any of this and stays fast regardless. **The search fallback's realistic ceiling without a key is still seconds-to-tens-of-seconds per name, not milliseconds** - that's the free engines themselves, not something a code change removes entirely. A configured key remains the one change that makes this fast and consistent rather than "usually quick, sometimes very slow."

**Known directories (step 1, "Known directories") skip search engines entirely.** colleges9.in organises every Telangana district's colleges under a fixed, discoverable URL - no search needed to even find the pages, so there's zero exposure to free-engine blocking or pacing. Picking **"Telangana engineering colleges"** fetches all 10 district pages directly and expands them with the same parser above. Measured live: **368 real colleges, in 9 seconds, one checkbox, no search engine call at all.** This can be ticked with an empty search list (it works standalone) or alongside your own searches (both feed the same sheet, deduplicated). It's the single most reliable way to get "500+ real rows" for this category - not a search result that might come back empty depending on engine mood, but the same ~370 real colleges every time.

For broad coverage ("100+ websites"), **New run → Search the web** has a **Generate many searches at once** panel: list the areas/items you want (one per line) and one pattern using `{area}`, e.g. `engineering colleges in {area} contact email phone` — it expands to one search per area and adds them all in one click. Up to 300 searches can be queued in a single run, each returning up to 500 "Websites per search" (both sliders go to 500 now, matching Places).

**Getting 500+ rows in one run, honestly.** There is no setting that makes one narrow query (`engineering colleges in Bachupally`) return 500 real results — that many distinct, real, non-directory colleges simply don't exist for one small suburb, on the map or on the open web, and the off-topic filter (above) means the app won't pad the gap with junk to hit a number. 500+ is realistic the same way it is for Places (below): as the **total across many broad searches in one run**, not from any single one —
- Use the batch generator with city/district-level areas (`Hyderabad, India`, `Secunderabad, India`, `Warangal, India`, …), not micro-neighbourhoods — each one has a real population of matching sites to find.
- "One row per website" + the off-topic filter already dedupe across all of them, so the run's final row count is the genuine, deduplicated total, not an inflated sum of overlapping queries.
- For a *guaranteed* 500+ of a specific, known category (e.g. every AICTE-approved engineering college in a state), the reliable source is an authoritative seed list, not search at all — ask about adding a "look up contacts for a list of names" mode, which would feed a pasted/uploaded list of institution names through the same website-lookup and contact-scraping pipeline instead of depending on what a search engine happens to index.

With no search key, one free engine alone often returns well under the number of results you asked for on a specific query, even when it isn't blocked at all — it just doesn't have more to give. The app merges results from every free engine (DuckDuckGo, Yahoo, Brave, Google, Mojeek, Startpage) for each search instead of stopping at the first one that answers, so "Websites per search" is a real target, not just an upper limit on one engine's small reply. A run can still end up with fewer rows than requested when the area genuinely doesn't have that many matching, non-directory sites — that's the real number, not a bug.

**Search queries that are too small an area usually get nothing — and that's expected, not broken.** A generic web search engine doesn't have a database of colleges by micro-neighbourhood: it matches a query like "engineering colleges in Tarnaka" against whatever's actually written on the web, and if nothing on the web happens to describe a college as being "in Tarnaka" specifically, it has nothing to return — even though real colleges exist nearby under a different, more commonly-used area name. The run log now says exactly why each search kept nothing:
- *"X unrelated skipped"* — the engine answered, but with pages that don't actually mention anything distinctive from the query (a sign it's padding a weak answer rather than admitting it has nothing — see above).
- *"X already seen skipped"* — the same pages already showed up for an earlier, different-sounding search in this run; several free engines fall back to the same generic, city-wide result set when a query is too narrow for them to tell apart from the last one.
- *"no results from any free search engine..."* — nobody had anything at all for that exact wording.

What works better than one query per small neighbourhood: search at the city/metro level (`engineering colleges in Hyderabad, India`) and let a higher "Websites per search" surface more of them, or use **Places** mode instead — its map query inherently covers every neighbourhood inside whatever area you give it in one pass, rather than needing a separate, narrow search per suburb.

**A repeated pattern of only one or two free engines answering while the rest say "no results" for every search** is usually this server's shared IP being rate-limited, not a real absence of results (a search for "engineering colleges in Hyderabad, India" getting zero from all six engines would be very unusual otherwise — Hyderabad is enormous). A free `BRAVE_API_KEY` or `GOOGLE_CSE_KEY`/`GOOGLE_CSE_CX` (see above) removes this uncertainty entirely, since those are official APIs with their own quota rather than being guessed at by IP.

Throughput is governed by the same measured search-engine pacing as Places (see the table above), shared by both modes through `_current_gap()` in [scraper/jobs.py](scraper/jobs.py):

| Setup | Pace between searches | Why |
|---|---|---|
| No key | 5 seconds (tune with `DISCOVER_GAP_SECONDS`) | Same measured limit as Places: faster than this made every free engine start refusing within a handful of requests. |
| `BRAVE_API_KEY` or (`GOOGLE_CSE_KEY` + `GOOGLE_CSE_CX`) set | 1.1 seconds (`DISCOVER_KEYED_GAP_SECONDS`) | An official key has its own quota instead of being IP-throttled, so the app paces much faster automatically. |

**What Apify does that this app doesn't, and why:** Apify's scale comes from infrastructure this app deliberately doesn't build, because the free/low-cost equivalent either doesn't exist or isn't safe to fake:

| Apify | This app | Free equivalent |
|---|---|---|
| Managed rotating proxy pools (residential/datacenter) so each request looks like a different visitor | None built in | Bring your own proxy — `HTTP_PROXY`/`HTTPS_PROXY` (page fetching) and `DDGS_PROXY` (free search engines) are honoured automatically with zero code changes if you already pay for a proxy provider. There's no free proxy pool worth using; free/public proxies are unreliable and often unsafe to route data through. |
| Managed headless-browser rendering (Puppeteer/Playwright) for JS-heavy sites, at scale | Plain HTTP fetch + meta-refresh redirect following | Tested against 8 real target sites: 6 worked immediately, 1 needed meta-refresh handling (now fixed), 1 was an unrelated DNS failure — genuine JS-rendering need wasn't found in practice here. Headless rendering is real infra cost (CPU, memory, far lower throughput) not currently justified by evidence, so it isn't included. |
| A RequestQueue + Dataset architecture and per-domain concurrency, run on Apify's cloud workers | A single background job per run, in server memory, paced sequentially | Good enough for the volumes free search engines allow anyway (see table); concurrency wouldn't raise the real ceiling, which is engine-side throttling, not this app's own speed. |
| Pay-per-result pricing that buys you all of the above | Free tier search APIs (Brave 2,000/month, Google CSE 100/day) + free scraping fallback | Honest ceiling: roughly 170 keyed lookups/day with no pacing delay, more beyond that at the paced free-engine rate. For 10,000+ rows this means running in batches over days, same as Places — there is no free setting that removes the engines' own rate limits. |

## Sending data to customers

A customer link is `https://<your-site>/s/<random code>`. It works without an account, so treat it like the file itself:

- The code is long and unguessable, and the page tells search engines not to index it. Turn on the **passcode** (default) and send it in a separate message. The sender can look it up again under **Shared → Shared by me → Passcode**; it is stored encrypted, and nobody else, including admins, can see it.
- Wrong passcodes are locked out after 5 tries (10 minutes). The link stops working on its expiry date and is deleted; an admin or the sender can stop it sooner.
- Opens and downloads are counted, ignoring the sender's own visits and chat-app link previews, so you can see whether the customer received it.
- Before creating a link, the sender must tick a confirmation that the company may share this data with that customer. The sheet probably contains **personal details of people** (for example students). Check the agreement with the customer and the privacy rules that apply, including India's DPDP Act and rules for people abroad.
- Prices, invoices and payments are not part of this app. Handle them outside it.

## Responsible use

- **Robots.txt.** The app follows each site's robots.txt and waits between requests to the same site.
- **Social sites.** LinkedIn, Facebook, Instagram and X are never opened by default; only their public search results are used. Logged-in collection is a restricted connector: an admin must enable it, and only after legal review.
- **Personal data.** Emails and phone numbers of people are personal data under India's DPDP Act. Use them in line with company policy, and don't send spam.

## Code map

```
app.py                 Flask app: login, roles, PWA files, run API, import/export helpers, settings, admin
models.py              Database models (users, vault, settings) + AES-256-GCM vault encryption
templates/             index.html (app shell), login.html, sw.js (service worker), offline.html
static/app.css         Design system (light/dark tokens, components)
static/app.js          Single-page front end: views, run tracker, install/offline handling
static/store.js        IndexedDB storage for sheets and run history (one database per user)
static/sheetops.js     Merge & Dedupe and CSV/JSON export (pure functions, unit-tested)
static/logo.svg        Logo; static/icons/ holds the app icons
scraper/search.py      Web search (Brave API, else free engines with fallbacks)
scraper/fetch.py       Polite fetcher (robots.txt, per-site delay, restricted-site rules)
scraper/extract.py     Email / phone / address / social extraction
scraper/places.py      OpenStreetMap places (categories, finding the area, mirrors)
scraper/discover.py    Finds a place's own website by web search; merges duplicate listings
scraper/ai_extract.py  Bring-your-own-key AI providers
scraper/jobs.py        Background runs and progress (in memory, nothing stored)
scraper/sheets.py      Reads uploaded Excel / CSV / JSON files
scraper/excel.py       Excel formatting (no secrets, no formula injection)
```
