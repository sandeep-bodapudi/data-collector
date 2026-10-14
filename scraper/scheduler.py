"""Cron matching + schedule triggering (Phase 5C). No new dependencies.

Schedules fire inside the web process (SCHEDULER_ENABLED, default on) by calling
start_job, so they inherit WORKER_MODE, preflight, budgets and caps. On multi-web
deployments enable the scheduler on exactly one replica.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime

FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
PRESETS = {"hourly": "0 * * * *", "daily": "0 9 * * *", "weekly": "0 9 * * 1", "monthly": "0 9 1 * *"}


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int) -> set[int]:
    """Parse one cron field into a value set. Supports *, */n, a, a,b, a-b, a-b/n."""
    text = text.strip()
    if not text:
        raise CronError("empty cron field")
    out: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\*|\d+)(?:-(\d+))?(?:/(\d+))?", part)
        if not m:
            raise CronError(f"bad cron part: {part!r}")
        start_s, end_s, step_s = m.groups()
        step = int(step_s) if step_s else 1
        if step < 1:
            raise CronError(f"bad cron step: {part!r}")
        if start_s == "*":
            start, end = lo, hi
        else:
            start = int(start_s)
            end = int(end_s) if end_s else start
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise CronError(f"cron value out of range: {part!r}")
        out.update(range(start, end + 1, step))
    if not out:
        raise CronError(f"empty cron field: {text!r}")
    return out


def parse_cron(cron: str) -> tuple[set[int], ...]:
    """Parse 'm h dom mon dow' (or a preset name). Raises CronError with a plain message."""
    cron = PRESETS.get((cron or "").strip().lower(), cron or "")
    parts = cron.strip().split()
    if len(parts) != 5:
        raise CronError("cron needs 5 fields: minute hour day month weekday (e.g. '0 9 * * 1')")
    parsed = tuple(_parse_field(p, lo, hi) for p, (lo, hi) in zip(parts, FIELD_RANGES))
    minute, hour, dom, month, dow = parsed
    if 7 in dow:  # cron tradition: 7 is a second name for Sunday
        dow = (dow - {7}) | {0}
    return (minute, hour, dom, month, dow)


def cron_due(cron: str, now: datetime | None = None) -> bool:
    """True when `now` (default: local time) matches. Standard dom/dow rule: when
    both are restricted, a match on either fires; otherwise the restricted one rules."""
    minute, hour, dom, month, dow = parse_cron(cron)
    now = now or datetime.now()
    if now.minute not in minute or now.hour not in hour or now.month not in month:
        return False
    dom_star = dom == set(range(1, 32))
    dow_star = dow == set(range(0, 7))
    if dom_star and dow_star:
        return True
    if dom_star:
        return ((now.weekday() + 1) % 7) in dow
    if dow_star:
        return now.day in dom
    return now.day in dom or ((now.weekday() + 1) % 7) in dow


def describe_cron(cron: str) -> str:
    """One plain line saying when it fires, for the schedule list."""
    try:
        minute, hour, dom, month, dow = parse_cron(cron)
    except CronError as e:
        return str(e)
    if len(minute) == 1 and len(hour) == 1 and len(dom) == 31 and len(month) == 12 and dow == set(range(0, 7)):
        return f"Daily at {next(iter(hour)):02d}:{next(iter(minute)):02d}"
    if len(dow) == 1 and len(dom) == 31:
        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return f"Weekly on {days[sorted(dow)[0] % 7]}"
    return f"cron '{cron}'"


def check_schedules(now: datetime | None = None) -> dict:
    """Fire due schedules once per minute. Returns {"fired": n, "skipped": m}.
    Never raises; every failure is recorded on the schedule row."""
    from models import Schedule, db

    now = now or datetime.now()
    minute_start = now.replace(second=0, microsecond=0)
    fired = skipped = 0
    try:
        due = Schedule.query.filter_by(enabled=True).all()
    except Exception:
        return {"fired": 0, "skipped": 0}
    for row in due:
        try:
            if row.last_triggered_at and row.last_triggered_at >= minute_start:
                continue  # already handled this minute
            if not cron_due(row.cron, now):
                continue
        except CronError as e:
            row.last_status = f"skipped: bad schedule ({e})"
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            skipped += 1
            continue
        except Exception:
            continue
        row.last_triggered_at = now
        try:
            import app as appmod

            from models import User as _ScheduleUser

            owner = _ScheduleUser.query.get(row.owner_id)
            if not owner or not owner.is_active:
                row.last_status = "skipped: account deactivated"
                skipped += 1
                db.session.commit()
                continue
            role = owner.role
            vault = appmod._effective_vault(row.owner_id, role)
            from scraper.jobs import start_job

            spec, err, _status = appmod.build_job_spec(row.body(), vault, row.owner_id)
            if err:
                row.last_status = f"skipped: {err}"
                skipped += 1
                from models import log_audit as _audit

                _audit(row.owner_id, "schedule.skipped", f"{row.name}: {err}")
            else:
                spec["schedule_id"], spec["schedule_name"] = row.id, row.name
                job = start_job(spec, row.owner_id)
                row.last_status = f"started run {job.id}"
                fired += 1
                from models import log_audit as _audit2

                _audit2(row.owner_id, "schedule.fired", f"{row.name} started run {job.id}")
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            try:
                row.last_status = f"skipped: {type(e).__name__}"
                db.session.commit()
            except Exception:
                db.session.rollback()
            skipped += 1
    return {"fired": fired, "skipped": skipped}


def scheduler_enabled() -> bool:
    return (__import__("os").environ.get("SCHEDULER_ENABLED", "1") or "1") != "0"


def start_scheduler_thread(app) -> threading.Thread | None:
    """Sixty-second loop in the web process. Single-host default; on multi-web
    deployments set SCHEDULER_ENABLED=0 on every replica but one."""

    def _loop():
        with app.app_context():
            while True:
                try:
                    if scheduler_enabled():
                        check_schedules()
                except Exception:
                    pass
                time.sleep(60)

    if not scheduler_enabled():
        return None
    t = threading.Thread(target=_loop, daemon=True, name="scheduler")
    t.start()
    return t
