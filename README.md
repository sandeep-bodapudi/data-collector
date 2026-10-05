# OneBridge Data Collector

An internal web app that collects **public** information from the web into spreadsheets, then lets you merge, dedupe and export it. No logins to the target websites are needed.

- **Web search:** emails, phones, addresses, social links and page details from websites matching your searches. Optional AI columns fill any detail you describe, using **each person's own AI key** (OpenAI, Gemini, Claude, OpenRouter, Groq or any OpenAI-compatible service).
- **Places:** lists of temples, hospitals, schools, hotels, banks… in any city, from OpenStreetMap.
- **Sheets:** every run saves a sheet. You can search it, open single rows, and export it as Excel, CSV or JSON. Excel, CSV and JSON files can also be imported.
- **Merge & Dedupe:** combine sheets and remove duplicates using smart matching for phones, emails and websites.
- **Admin:** users with Admin, Member and Viewer roles, plus the connector policy.

Product requirements: [docs/OneBridge Scraper_ Product Requirements Document.md](docs/OneBridge%20Scraper_%20Product%20Requirements%20Document.md). What's built so far: [docs/PRD-status.md](docs/PRD-status.md).

## Run it locally (Windows)

Double-click **`start.bat`** and open http://localhost:5000.

On first start you create the admin account. Add your team under **Admin**.

**Database.** Set `DATABASE_URL` in `.env`. The XAMPP MySQL example is in `.env.example`. Without it, the app uses `instance/data.db` (SQLite).

## Run it on a server (Render)

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app --workers 1 --threads 8 --timeout 180 --bind 0.0.0.0:$PORT`

Keep `--workers 1`: live runs are tracked in memory.

Environment variables:

| Variable | Why |
|---|---|
| `SECRET_KEY` | A long random value. **Never change it**, because it encrypts saved AI keys. |
| `APP_USER`, `APP_PASSWORD` | The first admin, created when the database has no users. |
| `DATABASE_URL` | PostgreSQL or MySQL. **Without it, users and sheets are lost on every redeploy.** |
| `BRAVE_API_KEY` | Optional, but recommended. Free search engines often block cloud servers. |

## Responsible use

- **Robots.txt.** The app follows each site's robots.txt and waits between requests to the same site.
- **Social sites.** LinkedIn, Facebook, Instagram and X are never opened by default; only their public search results are used. Logged-in collection is a restricted connector: an admin must enable it, and only after legal review.
- **Personal data.** Emails and phone numbers of people are personal data under India's DPDP Act. Use them in line with company policy, and don't send spam.

## Code map

```
app.py                 Flask app: login, roles, APIs (runs, sheets, merge, settings, admin)
models.py              Database models + AES-256-GCM vault encryption
templates/             index.html (app shell), login.html
static/app.css         Design system (light/dark tokens, components)
static/app.js          Single-page front end (Home, New run, Runs, Sheets, Merge, Settings, Admin)
scraper/search.py      Web search (Brave API, else free engines with fallbacks)
scraper/fetch.py       Polite fetcher (robots.txt, per-site delay, restricted-site rules)
scraper/extract.py     Email / phone / address / social extraction
scraper/places.py      OpenStreetMap places (categories, geocoding, mirrors)
scraper/ai_extract.py  Bring-your-own-key AI providers
scraper/jobs.py        Background runs, progress, saving results
scraper/sheets.py      Sheet storage, import, Merge & Dedupe
scraper/excel.py       Excel formatting (no secrets, no formula injection)
```
