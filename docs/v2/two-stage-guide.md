# Two-Stage Company Discovery — User Guide

## The idea in one paragraph

Collecting company data happens in two short stages. First you gather **only company
names** into a sheet — fast, and safe to interrupt. Later (or right away) you press
one button to **fill in the missing details** — websites, public company emails,
phones and addresses — for the rows that still need them.

## Stage 1 — Get company names

1. Start a run: **Web search** or **Places**, as usual.
2. Tick **“Collect company names only (recommended for large searches)”**.
   *“We will save the names first. You can add websites and contact details later.”*
3. While it runs you'll see *“Saving company names as we find them… You can stop
   anytime and continue later.”*
4. The sheet appears immediately with four columns:
   **Company Name | Source | Location / Search | Status** (every row starts as
   `Name only`). Export it as CSV/Excel at any time — stopping early keeps
   everything found so far.

## Stage 2 — Fill missing details

1. Open any sheet with a **Company Name** column (discovered, imported, or your own).
   A names-only sheet tells you:
   *“This sheet has company names. Click ‘Fill missing details’ to find websites
   and contact information.”*
2. Click **“Fill missing details”**, choose all rows or only the ones matching your
   current search filter (handy for small batches), and confirm:
   *“We will search for the missing website, email and phone numbers for these
   companies. This may take some time. You can stop and continue later.”*
3. Watch it work: *“Looking for websites and contacts… X companies still need
   details”*. Stopping keeps every row already filled.
4. Each attempted row ends with a Status: **Enriched** (website + contact),
   **Partial** (only one of the two), or **No website found**. Existing details are
   never overwritten, and running it again only retries the rows still missing.

## A note on contact details

Details come only from each company's **own website** — never invented, never taken
from directories or personal pages. A small business whose site publishes only a
Gmail address keeps that address, and the row's Source says so plainly.
