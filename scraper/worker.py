"""RQ worker entrypoint (Phase 5A). Run:  python -m scraper.worker

Needs a Redis server (REDIS_URL) and the `rq` + `redis` packages. The worker runs
the same `scraper.jobs.run_queued_job` the web process enqueues, with the same
budgets, caps and guardrails — progress snapshots and the final payload land in the
job store, which is how the web process reports on remote runs.
"""
import os
import sys


def main() -> int:
    try:
        import rq
        from redis import Redis
    except ImportError:
        print("The worker needs the 'rq' and 'redis' packages:  pip install rq redis")
        return 2
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    queue_name = os.environ.get("RQ_QUEUE", "onebridge")
    try:
        conn = Redis.from_url(url, socket_timeout=10)
        conn.ping()
    except Exception as e:
        print(f"Cannot reach Redis at {url}: {type(e).__name__}: {e}")
        return 1
    # Import here so a worker crash shows the real error, and so `python -m
    # scraper.worker --help`-style misuse fails loudly instead of idling.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import scraper.jobs  # noqa: F401  (registers run_queued_job for RQ)
    print(f"Listening on queue '{queue_name}' at {url} (Ctrl+C to stop)")
    try:
        # rq >= 1 API (pinned in requirements for worker hosts).
        rq.Worker([queue_name], connection=conn).work()
    except TypeError:
        # Very old rq: connection passed via context manager instead.
        with rq.Connection(conn):
            rq.Worker([queue_name]).work()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
