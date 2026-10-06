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
- The Places sheet has only real data: Name, Phone, Email, Website, Address and a map link (rows with contacts first). Coordinates, how a website was found and fetch problems are in the run log, not the sheet. "Engineering colleges" now matches any college/institute/university in the area and drops junior, medical, pharmacy, law and similar ones, so colleges without "engineering" in the map name (e.g. "CMR College") are included.
- To get contacts, the app first tries the address a college normally has (its initials + `.ac.in`, `.edu.in`, `.in`, …, e.g. `griet.ac.in`) and keeps it only if the page really is that college's — no search engine involved, so it isn't blocked or paced. Only places that fails for are searched on the web (see the table below). Contacts are then read from the site and its contact pages: emails are limited to the college's own domain (plus Gmail/Yahoo-style), at most 3 phone numbers, and the address is filled from the site when the map has none. (Old text follows:) The app searches the web for each place's own website, then reads the emails and phone numbers from that site and its contact pages. The *Website Source* column says whether a website came from the map or from the search. Numbers in *Phone* are reformatted consistently (`+91 98480 12345`) when they're clearly an Indian mobile; everything else (landlines with an STD code, foreign numbers, toll-free) is kept as found rather than guessed at. *Other Details* now only ever holds genuinely useful facts (who runs the place, its short/former name, a Wikipedia reference) — never raw map database tags.
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

"Search the web" sheets contain only real data: **Name** (the organisation's name read from its site, not the search-result title), the details you ticked (Emails, Phone Numbers, Address…) and **Website**. Search queries, snippets and fetch status stay in the run log. Directory/aggregator pages (Collegedunia, Shiksha, Justdial, Wikipedia, social sites…) are skipped because they list many places rather than being one. For a complete list of places in an area, **Places** mode is the better tool: it starts from a map list (100+ colleges) instead of however many sites a search engine returns.

For broad coverage ("100+ websites"), **New run → Search the web** has a **Generate many searches at once** panel: list the areas/items you want (one per line) and one pattern using `{area}`, e.g. `engineering colleges in {area} contact email phone` — it expands to one search per area and adds them all in one click. Up to 300 searches can be queued in a single run.

With no search key, one free engine alone often returns well under the number of results you asked for on a specific query, even when it isn't blocked at all — it just doesn't have more to give. The app merges results from every free engine (DuckDuckGo, Yahoo, Brave, Google, Mojeek, Startpage) for each search instead of stopping at the first one that answers, so "Websites per search" is a real target, not just an upper limit on one engine's small reply. A run can still end up with fewer rows than requested when the area genuinely doesn't have that many matching, non-directory sites — that's the real number, not a bug.

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
