# Data Collector

A web app that collects **public** information from the internet into **Excel**. No logins needed.

## Two ways to collect

| Mode | Use it for | Where data comes from |
|---|---|---|
| **🔎 Search the web** | Emails, phone numbers, addresses, social links, or *any detail you describe* (AI) | DuckDuckGo search results, then each website is visited |
| **📍 Find places** | Lists of temples, hospitals, schools, hotels, banks, offices… in any city | OpenStreetMap (free, open map data), optionally plus each place's website |

Every run creates an `.xlsx` file in the `outputs/` folder with:
- a **Data** sheet (filters, frozen header, clickable links)
- a **Run Info** sheet (what was searched, when, and with which settings)

## Starting it (Windows)

Double-click **`start.bat`**. On the first run it installs everything (Python 3.10+ is required) and then opens http://localhost:5000.

To let colleagues use it from their own PCs, run it on one machine and share `http://<that-pc's-IP>:5000`. The app listens on the whole network by default. Only do this on a trusted office network, because the app has no login.

## Turning on AI fields (optional)

"Other details (AI)" lets staff type *any* details they need, for example `main deity, darshan timings` or `services offered, founder name`. The AI reads each page and fills one column per detail.

1. Copy `.env.example` to `.env`
2. Put the company's Anthropic API key after `ANTHROPIC_API_KEY=`
3. Restart the app

Each page read with AI costs a small amount of API usage, so keep "Results per search" sensible.

## What it will and won't do

- Collects only pages anyone can open without logging in.
- Follows each site's `robots.txt` and waits between requests to the same site.
- Skips login-walled or social sites (Facebook, Instagram, LinkedIn, X, etc.). Their search result is kept, but the page isn't opened.
- Some sites block automated visitors (`HTTP 403` in the *Fetch Status* column). The tool does not try to get around this.
- Doesn't solve CAPTCHAs or bypass paywalls.

**Compliance:** emails and phone numbers of people are personal data under privacy laws such as India's DPDP Act and GDPR. Check with your compliance team before using collected contacts for marketing, and don't send unsolicited bulk email.

## For developers

```
app.py                 Flask web server + API
templates/index.html   The web page
scraper/fetch.py       Polite fetcher (robots.txt, per-site delay, skip list)
scraper/extract.py     Emails, phones, address, social links, title… from any page
scraper/ai_extract.py  Custom fields via Claude (structured JSON output)
scraper/places.py      OpenStreetMap categories (add new ones in CATEGORIES)
scraper/jobs.py        Background jobs, progress, Excel saving
scraper/excel.py       Workbook formatting
```

Manual setup: `python -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`, then `.venv\Scripts\python app.py`.
