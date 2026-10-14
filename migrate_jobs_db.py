"""One-shot disk -> database job-store migration (Phase 5B).

Usage:  python migrate_jobs_db.py
Reads every payload in instance/jobs/ (or JOB_STORE_DIR) and copies the missing
ones into the job_payloads table. Disk files are left in place, so the migration
is safe to re-run and to roll back (just flip JOB_STORE_BACKEND back to "disk").
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as appmod  # noqa: E402
from scraper import job_store as JS  # noqa: E402


def main() -> int:
    if JS.backend() == "db":
        print("Note: JOB_STORE_BACKEND is already 'db'; migrating remaining disk files anyway.")
    with appmod.app.app_context():
        result = JS.migrate_disk_to_db()
    print(f"moved={result['moved']} skipped={result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
