"""Managed proxy with a monthly MB cap (Phase 5E).

The proxy URL itself stays in the environment (HTTP_PROXY/HTTPS_PROXY for page
fetching, DDGS_PROXY for search engines) — never in the database. What the admin
controls here is *whether* each connector may use it and how many megabytes it may
burn per month. That keeps the only large recurring infra cost behind an explicit,
capped switch. Default: everything off.

Metering is honest about its limits: page-fetch bytes are counted exactly;
search-engine reply bytes are counted as received payload; robots.txt fetches are
negligible and unmetered. Usage is kept in memory and flushed to AppSetting at most
once a minute, so a crash can undercount by <60s of traffic — fail-cheap, never
fail-expensive (over-counting is impossible: only received bytes are added).
"""
from __future__ import annotations

import os
import threading
import time

_lock = threading.Lock()
_used_mb = 0.0
_month = ""
_last_flush = 0.0


def _settings() -> tuple[bool, bool, bool, float]:
    """(master_enabled, search_enabled, fetch_enabled, cap_mb). Never raises."""
    try:
        from models import AppSetting

        on = AppSetting.get("proxy_enabled", "0") == "1"
        search = AppSetting.get("proxy_search", "0") == "1"
        fetch = AppSetting.get("proxy_fetch", "0") == "1"
        cap = float(AppSetting.get("proxy_cap_mb", "0") or 0)
        return on, search, fetch, cap
    except Exception:
        return False, False, False, 0.0


def _rollover_if_needed() -> None:
    """Reset the monthly counter. Called with the lock held."""
    global _used_mb, _month
    month = time.strftime("%Y-%m")
    if _month != month:
        _month = month
        _used_mb = 0.0
        try:
            from models import AppSetting

            AppSetting.set("proxy_month", month)
            AppSetting.set("proxy_used_mb", "0")
            from models import db

            db.session.commit()
        except Exception:
            try:
                from models import db as _db

                _db.session.rollback()
            except Exception:
                pass


def used_mb() -> float:
    """Month-to-date proxied megabytes (this process's view). Never raises."""
    try:
        with _lock:
            _rollover_if_needed()
            return _used_mb
    except Exception:
        return 0.0


def proxy_for(connector: str) -> str:
    """Proxy URL for "search" or "fetch", or "" when it must go direct.

    Direct when: master off, connector off, no cap set, cap reached, or no URL
    configured. A missing cap means *off*, same posture as social budgets.
    """
    if connector not in ("search", "fetch"):
        return ""
    on, search, fetch, cap = _settings()
    if not on or not {"search": search, "fetch": fetch}[connector] or cap <= 0:
        return ""
    try:
        with _lock:
            _rollover_if_needed()
            if _used_mb >= cap:
                return ""
    except Exception:
        return ""
    url = (os.environ.get("DDGS_PROXY", "") if connector == "search"
           else os.environ.get("HTTPS_PROXY", "") or os.environ.get("HTTP_PROXY", ""))
    return url.strip()


def account(n_bytes: int) -> None:
    """Add received bytes to the meter. Flushes to AppSetting at most once a minute."""
    global _used_mb, _last_flush
    if not n_bytes or n_bytes < 0:
        return
    try:
        with _lock:
            _rollover_if_needed()
            _used_mb += n_bytes / (1024 * 1024)
            now = time.monotonic()
            if now - _last_flush >= 60:
                _last_flush = now
                from models import AppSetting, db

                AppSetting.set("proxy_used_mb", f"{_used_mb:.3f}")
                db.session.commit()
    except Exception:
        try:
            from models import db as _db

            _db.session.rollback()
        except Exception:
            pass


def status() -> dict:
    """Member-safe summary for the status endpoint and admin UI."""
    on, search, fetch, cap = _settings()
    used = used_mb()
    return {"enabled": on, "search": search, "fetch": fetch, "cap_mb": cap,
            "used_mb": round(used, 2),
            "pct": round(used / cap * 100) if cap > 0 else 0,
            "capped": cap > 0 and used >= cap}
