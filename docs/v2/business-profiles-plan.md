# Business profiles: accurate rows, clear sheets

Status: in progress (2026-10-19). Owner asked for "a proper and clear scraping application to find business profiles more
accurately; sheets should be clear and the data useful", large code changes accepted.

## Problems seen in real runs (2026-10-18/19)

| Problem | Example |
|---|---|
| Each mode makes a different sheet | Web: `Name, Emails, Phone Numbers, Address, Website`; Places: `Name, Phone, Email, Website, Address, Google Maps Link`; Telangana file had to be merged by hand |
| Several emails/phones crammed into one cell, best one not first | `talent@visionias.in; enquiry@...; pune@...; jaipur@...` |
| Wrong website attached to a business | "A.V College of Arts..." -> arts.com |
| Contacts that only appear after JavaScript runs are missed | Naresh IT, IIDE, Lifeboat: emails found only by a real browser (trial: 6 -> 12 of 46 sites) |
| Three slightly different website readers (web, Places, enrich) | fixes made in one were missing in the others |
| Empty columns in every sheet | Description, Contact Page... columns with nothing in them |
| No way to tell a solid row from a guess | a directory row and a verified official site look the same |

## Design

1. **One site reader** (`jobs._read_site`): home page + up to 3 contact/about pages, own-domain emails first, phones de-duplicated.
   Used by web, Places and Fill-missing-details alike.
2. **Browser fallback** (`scraper/browser.py`): when the plain reader finds no email, or is refused (403/401/429/connection),
   the site is read again in headless Chromium (JavaScript on; images, fonts, video off), following up to 2 contact links.
   Opt-in with `BROWSER_FALLBACK=1`, only where Playwright is installed; capped per run (`BROWSER_MAX_SITES`, default 40);
   one browser per process on its own thread. Ignored on the small hosted server.
3. **One business-profile sheet** (`scraper/profile.py`) for web and Places runs, in this order:
   `Business Name, Category, Phone, Other Phones, Email, Other Emails, Email Quality, Website, Address, Area, City, State,
   Pincode, Opening Hours, Google Maps Link, Social Links, Match Confidence, Source, Last Checked` + any AI columns.
   - Primary phone: a mobile first, then landline; the rest in Other Phones.
   - Primary email: the best-rated one (company's own domain, mail server found); the rest in Other Emails.
   - Pincode/City pulled from the address when the source did not give them; Area = the searched place.
   - A column with nothing in it is left out (core columns always stay).
4. **Match Confidence** (High / Medium / Low + reason): does the website match the business name, are the contacts on that
   website's own domain, is the place on a map. Low rows are kept but sorted last and say why.
5. Stage-1 name lists and Fill-missing-details keep their own columns (they are the person's own sheet); they gain the
   quality columns only.

## Not in scope here
Paid Google Maps (Apify) stays as built; Brave key setup is the owner's next step for the hosted app.

## Status (2026-10-19): implemented

- `scraper/browser.py` (new), `jobs._read_site` (one reader for web, Places and Fill-missing-details; replaces three
  copies), `scraper/profile.py` (new). Names accepted as "Business Name" everywhere a name column is looked up.
- Extra fixes found on the real runs: Indian mobiles written "+91 98480 12345" / "98480 12345" were only found next to a
  "Phone:" label (PHONE_RE needs three digit groups) - new MOBILE_RE; "Call:" and "WhatsApp" labels; a page title like
  "Digital Marketing Landing Page -" becomes the site's name; skillfloor.in mail on skillfloor.com counts as the
  company's own domain.
- Real local run, "digital marketing institute in Ameerpet, Hyderabad": 6 businesses, 5 High, 6 with email and phone.
- Tests: tests/test_profile.py (new); test_api, test_phase4_durable, test_phase5_queue, test_quality updated to the new
  sheet. Full suite passes.
