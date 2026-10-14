# Secret Rotation Checklist

Applies to `SECRET_KEY` ( Flask sessions) and `VAULT_KEY` (credential vault encryption,
defaults to `SECRET_KEY`). Rotate rarely; each rotation has a cost. Read all steps before starting.

## What breaks on rotation

- **Sessions:** all users are signed out (new `SECRET_KEY` invalidates old session cookies).
- **Vault:** every saved AI/Brave/social key and share passcode becomes unreadable
  (`decrypt` fails → treated as not set). Users must re-enter keys; customer-link
  senders must re-share links that had passcodes.
- **Durable jobs:** unaffected (payloads hold public spec only, no secrets).
- **Database:** untouched; only the env var changes.

## Steps

1. Announce a maintenance window; ask the team to finish collections (finished results
   persist on disk, running jobs do not survive a restart anyway).
2. Back up the database file (`instance/data.db`) or snapshot Postgres.
3. Set the new `SECRET_KEY` (and `VAULT_KEY` if used) in the environment.
4. Restart the app. Confirm boot log shows no errors and `/api/admin/settings` loads.
5. Each user re-enters keys in Settings → AI provider / Search connector. Verify with
   the on-page Test buttons and one small run per connector.
6. Re-share any customer links whose passcode lookup now shows "cannot be read".
7. Delete the pre-rotation database backup only after a week of clean operation.

## Rollback

Restore the old env vars + database backup together (never mix an old DB with a new
key or vice versa). Restart. Everything returns, including sessions issued before expiry.
