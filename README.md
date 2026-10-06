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
| `BRAVE_API_KEY` | Optional, but recommended. Free search engines often block cloud servers. |

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

How it works, and what to expect:

- The list of places comes from OpenStreetMap, a community-edited map. It knows names and positions but almost never phone numbers, emails or websites, and it may not list every place. The public map servers are sometimes slow, so a search can take a few minutes.
- The area is matched as a whole area where possible. Say `Bachupally, Hyderabad` or just `Bachupally`: the run log's first line shows exactly which area was searched. If a name matches only one business, the app searches about 3 km around it and says so.
- To get contacts, the app searches the web for each place's own website (about 5 seconds per place, up to 80 per run, to stay within what search engines allow), then reads the emails and phone numbers from that site and its contact pages. The *Website Source* column says whether a website came from the map or from the search.
- The same place often appears twice on the map under different names ("griet college" and "GRIET COLLEGE"). These are merged automatically, and the other name is noted in *Other Details*.
- If a place shows no contacts, its website either wasn't found or doesn't publish them as text. Add a Brave Search key (`BRAVE_API_KEY`) if searches are being limited.
- For a wider list, search a larger or neighbouring area, or use **Search the web** with a query such as `engineering colleges in Bachupally Hyderabad contact email phone`.

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
