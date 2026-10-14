"""Back up what a redeploy must not lose (Phase 5E). Run:  python backup.py [out_dir]

Copies the SQLite database file (when that's the configured backend) and every
durable-job payload into a timestamped folder, plus a manifest.json describing
what was taken. For Postgres backends it tries pg_dump and otherwise prints the
exact command to run instead of failing silently. Nothing here needs the app
running; pass --check to only report what *would* be backed up.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    here = os.path.dirname(os.path.abspath(__file__))
    return "sqlite:///" + os.path.join(here, "instance", "data.db")


def _sqlite_path(url: str) -> str | None:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return None


def _job_files() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    store = os.environ.get("JOB_STORE_DIR") or os.path.join(here, "instance", "jobs")
    try:
        return sorted(os.path.join(store, n) for n in os.listdir(store) if n.endswith(".json.gz"))
    except OSError:
        return []


def plan_backup(out_dir: str) -> dict:
    """Describe the backup without taking it. Never raises."""
    url = _database_url()
    sq_path = _sqlite_path(url)
    files = _job_files()
    steps = []
    if sq_path and os.path.exists(sq_path):
        steps.append({"kind": "sqlite_copy", "src": sq_path,
                      "dst": os.path.join(out_dir, "data.db")})
    elif sq_path:
        steps.append({"kind": "missing", "detail": f"SQLite file not found: {sq_path}"})
    else:
        pg = shutil.which("pg_dump")
        steps.append({"kind": "pg_dump" if pg else "pg_dump_missing",
                      "detail": "pg_dump " + ("available" if pg else "not found - install postgres-client")})
    steps.append({"kind": "job_files", "count": len(files), "dst": os.path.join(out_dir, "jobs")})
    return {"url_kind": "sqlite" if sq_path else "postgres", "steps": steps}


def run_backup(out_dir: str | None = None) -> dict:
    """Take the backup. Returns a manifest dict (also written as manifest.json)."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = out_dir or os.path.join(here, "outputs", "backups", stamp)
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"taken_at": stamp, "copied": [], "notes": []}
    url = _database_url()
    sq_path = _sqlite_path(url)
    if sq_path:
        if os.path.exists(sq_path):
            shutil.copy2(sq_path, os.path.join(out_dir, "data.db"))
            manifest["copied"].append("data.db")
        else:
            manifest["notes"].append(f"SQLite file not found: {sq_path}")
    else:
        pg = shutil.which("pg_dump")
        dest = os.path.join(out_dir, "db.sql")
        if pg:
            r = subprocess.run([pg, url], capture_output=True, timeout=300)
            if r.returncode == 0:
                with open(dest, "wb") as f:
                    f.write(r.stdout)
                manifest["copied"].append("db.sql")
            else:
                manifest["notes"].append(f"pg_dump failed: {r.stderr.decode()[-300:]}")
        else:
            manifest["notes"].append("pg_dump not found - back up Postgres with your host's tools")
    jobs_out = os.path.join(out_dir, "jobs")
    os.makedirs(jobs_out, exist_ok=True)
    for src in _job_files():
        try:
            shutil.copy2(src, os.path.join(jobs_out, os.path.basename(src)))
            manifest["copied"].append("jobs/" + os.path.basename(src))
        except OSError as e:
            manifest["notes"].append(f"skipped {os.path.basename(src)}: {e}")
    manifest["job_files"] = len(_job_files())
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest


if __name__ == "__main__":
    check_only = "--check" in sys.argv
    dest = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if check_only:
        print(json.dumps(plan_backup(dest or "<default>"), indent=2))
    else:
        manifest = run_backup(dest)
        print(json.dumps(manifest, indent=2))
