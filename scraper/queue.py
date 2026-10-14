"""Background job queue (Phase 5A).

Jobs run in RQ workers when WORKER_MODE=rq and a Redis server is reachable;
otherwise everything runs in-process exactly as before. The flag-off path is
byte-identical to Phases 1-4: same functions, same payloads, same REST shapes.

Redis/rq are imported lazily so deployments without them (and this repo's test
suite) never pay for them. Add `rq` + `redis` to requirements for worker hosts.
"""
from __future__ import annotations

import os

WORKER_MODE = os.environ.get("WORKER_MODE", "thread").lower()  # "thread" (default) or "rq"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.environ.get("RQ_QUEUE", "onebridge")

_backend = None  # injectable for tests: an object with .enqueue(fn_path, job_id, spec, user_id)


def set_backend(backend) -> None:
    """Tests inject a FakeQueue here; production leaves it None (real RQ)."""
    global _backend
    _backend = backend


def worker_mode() -> str:
    if _backend is not None:
        return "rq"
    return WORKER_MODE


def enqueue_job(job_id: str, spec: dict, user_id: int):
    """Enqueue (job_id, spec, user_id) for a worker. Returns the RQ job, or None
    when queuing is unavailable (caller falls back to a thread). Never raises."""
    if _backend is not None:
        try:
            return _backend.enqueue("scraper.jobs.run_queued_job", job_id, spec, user_id)
        except Exception:
            return None
    try:
        import rq
        from redis import Redis
    except ImportError:
        return None
    try:
        q = rq.Queue(QUEUE_NAME, connection=Redis.from_url(REDIS_URL, socket_timeout=5))
        return q.enqueue("scraper.jobs.run_queued_job", job_id, spec, user_id,
                         job_timeout="3h", result_ttl=0, failure_ttl=0)
    except Exception:
        return None  # Redis down: caller falls back to a thread, job still runs


def request_cancel(job_id: str) -> None:
    """Cross-process stop: memory event for threads, shared flag for workers."""
    try:
        from . import job_store

        job_store.cancel_request(job_id)
    except Exception:
        pass
