# Social Enrichment — User & Admin Notes

## For users

- New **Social** tab under New run. Paste LinkedIn / Instagram / Facebook URLs or names, one per line.
- Keep **free lookup first** ON: paid enrichment only fills what free lookup missed.
- **Free lookup only** checkbox forces ₹0 spend for that run.
- Output columns: Name, Headline, Current Company, Location, About, Experience summary, Website, Profile URL, Platform.
- Honest coverage: the run log says what was free, what was paid, what was skipped and why. Public fields only; no guaranteed contacts.

## For admins

- Connections table now includes **Social (master)**, **Bright Data**, **Apify**. All default to enabled-flag ON but do nothing without keys and budget.
- Save provider keys in **Settings** (masked, `__keep__` semantics). Paid enrichment stays
  off until a monthly budget is set; budget inputs live under the Connections card and
  prefill from saved limits.
- Suggested start: ₹2,000–₹3,000/month. 1,000 profiles ≈ $1.40–$3.50 ≈ ₹135–₹340.
- At 100% spend, jobs automatically go free-only and say so. Rollback is one toggle: disable **Social**.
- Restricted cookie scraping stays off by default with its legal warning; this phase does not expand it.
