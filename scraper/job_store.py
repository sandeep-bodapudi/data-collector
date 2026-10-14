"""Durable finished-job store (Phase 4).

Finished (and cancelled-with-rows, and error) results persist as one gzip JSON file
per job under instance/jobs/. The database is untouched: accounts and settings stay
in SQLite, bulky payloads stay out of it. TTL 2 hours, then purged. The browser
collects the result and calls DELETE, which removes disk + memory immediately.

Secrets never touch disk: callers must pass `public_spec()` output, never the raw spec.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time

JOB_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")
RESULT_TTL = 2 * 3600  # seconds a finished job stays collectible
MAX_STORE_BYTES = 20 * 1024 * 1024  # raw JSON cap; beyond it rows are truncated
MAX_STORE_ROWS = 5000


def job_dir() -> str:
    base = os.environ.get("JOB_STORE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "jobs")
    os.makedirs(base, exist_ok=True)
    return base


def _path(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise ValueError(f"bad job id: {job_id!r}")
    return os.path.join(job_dir(), job_id + ".json.gz")


def save(job_id: str, payload: dict) -> bool:
    """Atomic gzip write. Truncates rows past caps with a note. Returns False when refused."""
    try:
        path = _path(job_id)
    except ValueError:
        return False
    raw = _encode(payload)
    if raw is None:
        return False
    try:
        tmp = path + ".tmp"
        with gzip.open(tmp, "wb", compresslevel=6) as f:
            f.write(raw)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _prepare(payload: dict) -> dict:
    """Copy + row caps shared by both backends. O(rows)."""
    payload = dict(payload)
    rows = payload.get("rows") or []
    if len(rows) > MAX_STORE_ROWS:
        payload["rows"] = rows[:MAX_STORE_ROWS]
        payload["truncated"] = f"kept first {MAX_STORE_ROWS} of {len(rows)} rows"
    return payload


def _encode(payload: dict) -> bytes | None:
    """Gzip-ready bytes, or None when the payload refuses to fit the cap."""
    payload = _prepare(payload)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_STORE_BYTES:
        payload["rows"] = (payload.get("rows") or [])[:500]
        payload["truncated"] = "rows truncated: payload exceeded store cap"
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_STORE_BYTES:
            return None
    return raw


def _decode(raw: bytes) -> dict | None:
    try:
        return json.loads(gzip.decompress(raw).decode("utf-8"))
    except (OSError, ValueError):
        return None


def load(job_id: str) -> dict | None:
    """Return the stored payload, or None when missing/corrupt (corrupt files are removed)."""
    try:
        path = _path(job_id)
    except ValueError:
        return None
    try:
        with gzip.open(path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def remove(job_id: str) -> None:
    try:
        os.remove(_path(job_id))
    except (OSError, ValueError):
        pass


def purge(ttl: int = RESULT_TTL, now: float | None = None) -> int:
    """Delete expired payloads. Returns count removed. O(n) files, n small by TTL."""
    now = time.time() if now is None else now
    removed = 0
    try:
        names = os.listdir(job_dir())
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".json.gz"):
            continue
        path = os.path.join(job_dir(), name)
        try:
            with gzip.open(path, "rb") as f:
                payload = json.loads(f.read().decode("utf-8"))
            finished = float(payload.get("finished_at") or 0)
            if finished:
                expired = (now - finished) > ttl
            else:
                # A running marker with no finish: use file age, so a thread that died
                # silently can never linger past TTL even if no restart rehydrates it.
                expired = (now - os.path.getmtime(path)) > ttl
        except (OSError, ValueError):
            expired = True  # unreadable: drop it rather than let it linger
        if expired:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    # Stop flags are tiny but would otherwise accumulate forever: drop any whose
    # payload is gone (job finished long ago or never existed on this host).
    try:
        payloads = {n[:-8] for n in os.listdir(job_dir()) if n.endswith(".json.gz")}
    except OSError:
        payloads = set()
    try:
        cancels = [n for n in os.listdir(job_dir()) if n.endswith(".cancel")]
    except OSError:
        cancels = []
    for name in cancels:
        if name[:-7] not in payloads:
            try:
                os.remove(os.path.join(job_dir(), name))
                removed += 1
            except OSError:
                pass
    return removed


def list_ids() -> list[str]:
    try:
        return [n[:-8] for n in os.listdir(job_dir()) if n.endswith(".json.gz")]
    except OSError:
        return []


def _cancel_path(job_id: str) -> str:
    if not JOB_ID_RE.match(job_id or ""):
        raise ValueError(f"bad job id: {job_id!r}")
    return os.path.join(job_dir(), job_id + ".cancel")


def set_cancelled(job_id: str) -> None:
    """Cross-process stop flag for worker-run jobs. Never raises."""
    try:
        with open(_cancel_path(job_id), "w") as f:
            f.write("1")
    except (OSError, ValueError):
        pass


def is_cancelled(job_id: str) -> bool:
    try:
        return os.path.exists(_cancel_path(job_id))
    except ValueError:
        return False


def clear_cancel(job_id: str) -> None:
    try:
        os.remove(_cancel_path(job_id))
    except (OSError, ValueError):
        pass
    if backend() != "disk":
        try:
            from models import JobCancel, db

            row = db.session.get(JobCancel, job_id)
            if row:
                db.session.delete(row)
                db.session.commit()
        except Exception:
            try:
                from models import db as _db

                _db.session.rollback()
            except Exception:
                pass


# ---------------------------------------------------------------- 5B shared store

def backend() -> str:
    """'disk' (default, single-host), 'dual' (transition: write both, read DB first),
    or 'db' (shared). Unknown values fall back to disk. Read at call time so tests
    and deploys can switch without a restart of the import graph."""
    return os.environ.get("JOB_STORE_BACKEND", "disk").lower() if \
        os.environ.get("JOB_STORE_BACKEND", "disk").lower() in ("disk", "dual", "db") else "disk"


def save_db(job_id: str, payload: dict) -> bool:
    """Write one payload row. Returns False (never raises) when the DB is unreachable."""
    if not JOB_ID_RE.match(job_id or ""):
        return False
    raw = _encode(payload)
    if raw is None:
        return False
    try:
        import gzip as _gzip
        from datetime import datetime as _dt

        from models import JobPayload, db

        blob = _gzip.compress(raw, compresslevel=6)
        row = db.session.get(JobPayload, job_id) or JobPayload(job_id=job_id)
        row.owner_id = payload.get("owner_id")
        row.mode = (payload.get("mode") or "web")[:20]
        row.status = (payload.get("status") or "done")[:20]
        row.data, row.size_bytes = blob, len(blob)
        finished = payload.get("finished_at") or 0
        row.finished_at = _dt.fromtimestamp(finished) if finished else None
        row.updated_at = _dt.now()  # every snapshot refreshes this: a live job's row
        # can never look expired to the TTL purge below
        db.session.add(row)
        db.session.commit()
        return True
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass
        return False


def load_db(job_id: str) -> dict | None:
    """Read one payload row. Corrupt rows are deleted. None when missing/unreachable."""
    if not JOB_ID_RE.match(job_id or ""):
        return None
    try:
        from models import JobPayload, db

        row = db.session.get(JobPayload, job_id)
        if not row or not row.data:
            return None
        payload = _decode(bytes(row.data))
        if not isinstance(payload, dict):
            db.session.delete(row)
            db.session.commit()
            return None
        return payload
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass
        return None


def remove_db(job_id: str) -> None:
    try:
        from models import JobPayload, db

        row = db.session.get(JobPayload, job_id)
        if row:
            db.session.delete(row)
            db.session.commit()
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass


def purge_db(ttl: int = RESULT_TTL, now: float | None = None) -> int:
    """Delete expired rows (by finished_at; unfinished rows by last update, so live
    snapshots are immune) plus stale stop flags. Returns total removed."""
    from datetime import datetime as _dt

    now = time.time() if now is None else now
    removed = 0
    try:
        from models import JobCancel, JobPayload, db

        cutoff = _dt.fromtimestamp(now - ttl)
        q = JobPayload.query.filter(
            ((JobPayload.finished_at.isnot(None)) & (JobPayload.finished_at < cutoff))
            | ((JobPayload.finished_at.is_(None)) & (JobPayload.updated_at < cutoff)))
        removed += q.count()
        q.delete(synchronize_session=False)
        old_flags = JobCancel.query.filter(JobCancel.created_at < cutoff)
        removed += old_flags.count()
        old_flags.delete(synchronize_session=False)
        db.session.commit()
        return removed
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass
        return 0


def list_ids_db() -> list[str]:
    try:
        from models import JobPayload

        return [r.job_id for r in JobPayload.query.with_entities(JobPayload.job_id).all()]
    except Exception:
        return []


def set_cancelled_db(job_id: str) -> bool:
    if not JOB_ID_RE.match(job_id or ""):
        return False
    try:
        from models import JobCancel, db

        if not db.session.get(JobCancel, job_id):
            db.session.add(JobCancel(job_id=job_id))
            db.session.commit()
        return True
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass
        return False


def is_cancelled_db(job_id: str) -> bool:
    try:
        from models import JobCancel, db

        return db.session.get(JobCancel, job_id) is not None
    except Exception:
        return False


# ---------------------------------------------------------------- backend-aware API

def persist(job_id: str, payload: dict) -> bool:
    """Write per backend(): disk writes the file; dual writes both (True if either
    lands); db writes the row. Never raises."""
    mode = backend()
    if mode == "db":
        return save_db(job_id, payload)
    ok_disk = save(job_id, payload)
    if mode == "dual":
        return save_db(job_id, payload) or ok_disk
    return ok_disk


def retrieve(job_id: str) -> dict | None:
    """Read per backend(): db prefers the row then the file; otherwise the file."""
    if backend() == "disk":
        return load(job_id)
    row = load_db(job_id)
    return row if isinstance(row, dict) else load(job_id)


def discard(job_id: str) -> None:
    """Remove from wherever it may live (both, in dual). Never raises."""
    remove(job_id)
    if backend() != "disk":
        remove_db(job_id)


def clean(ttl: int = RESULT_TTL) -> int:
    """Purge expired payloads across backends. Never raises."""
    n = purge(ttl)
    if backend() != "disk":
        try:
            n += purge_db(ttl)
        except Exception:
            pass
    return n


def all_ids() -> list[str]:
    """Every known payload id across backends (for rehydrate). Never raises."""
    ids = set(list_ids())
    if backend() != "disk":
        try:
            ids.update(list_ids_db())
        except Exception:
            pass
    return sorted(ids)


def cancel_request(job_id: str) -> None:
    """Shared stop flag: DB row when on a shared backend (with disk mirror for the
    local host), else the disk file. Never raises."""
    if backend() != "disk":
        if set_cancelled_db(job_id):
            set_cancelled(job_id)
            return
    set_cancelled(job_id)


def cancel_requested(job_id: str) -> bool:
    if backend() != "disk" and is_cancelled_db(job_id):
        return True
    return is_cancelled(job_id)


def cancel_clear(job_id: str) -> None:
    clear_cancel(job_id)


def migrate_disk_to_db() -> dict:
    """One-shot import of disk payloads into the DB. Skips ids already present.
    Returns {"moved": n, "skipped": m}. Never raises."""
    moved = skipped = 0
    try:
        from models import JobPayload, db

        have = {r.job_id for r in JobPayload.query.with_entities(JobPayload.job_id).all()}
    except Exception:
        return {"moved": 0, "skipped": 0}
    for jid in list_ids():
        if jid in have:
            skipped += 1
            continue
        payload = load(jid)
        if not isinstance(payload, dict):
            continue
        if save_db(jid, payload):
            moved += 1
        else:
            break  # DB unreachable: stop, disk stays authoritative
    return {"moved": moved, "skipped": skipped}
