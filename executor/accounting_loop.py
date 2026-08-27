"""Production runner for the read-only Binance accounting sync.

WHY THIS FILE EXISTS
--------------------
`accounting_sync.py` is imported by nothing in the trading executor, which is
what keeps accounting incapable of touching a position. The cost of that
isolation is that nothing was running it. This is the something.

It is a separate process, in a separate container, with its own command. It does
not import `main.py`, `signal_consumer.py`, `user_session.py` or any other
trading module, and it cannot start, stop, or influence one:

  * the only Binance access underneath it is a signed GET against three
    read-only endpoints;
  * the only write is an upsert of a reporting row, which is idempotent on
    (user_id, close_binance_order_id), so a retry after any failure restates a
    trade rather than duplicating it;
  * a crash here leaves the executor and the engine exactly as they were, and a
    crash there leaves accounting running.

Accounting deliberately keeps running when trading is stopped. A trade that has
already happened still needs its commission and net P&L synchronised, and a
client who pressed Stop is precisely the client most likely to be looking at
their final numbers.

SINGLE INSTANCE
---------------
Two accounting passes over one account at the same time would each fetch the
same fills and each attribute the same funding events. The upsert makes that
harmless for the totals, but it doubles the Binance rate-limit cost for no
benefit, so a lock file keeps one pass at a time. A stale lock — from a
container killed mid-pass — is taken over after LOCK_STALE_SECONDS rather than
wedging accounting forever.

Env:
  ACCOUNTING_INTERVAL_SECONDS  seconds between passes (default 300, min 60)
  ACCOUNTING_LOCK_PATH         lock file (default /tmp/helix-accounting.lock)
  ACCOUNTING_RUN_ONCE          "1" to make a single pass and exit
  plus everything accounting_sync.py reads.

No credential or token is read, held, or logged by this file.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import accounting_sync

log = logging.getLogger("executor.accounting.loop")

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 60
DEFAULT_LOCK_PATH = "/tmp/helix-accounting.lock"
# Longer than any plausible pass, so a running pass is never stolen from.
LOCK_STALE_SECONDS = 3600


class AlreadyRunning(Exception):
    """Another accounting pass holds the lock."""


class RunLock:
    """One accounting pass at a time, per lock path.

    Uses an atomic O_CREAT|O_EXCL create rather than a check-then-write, so two
    processes starting in the same instant cannot both believe they won.
    """

    def __init__(self, path: str, stale_seconds: int = LOCK_STALE_SECONDS):
        self.path = path
        self.stale_seconds = stale_seconds
        self._held = False

    def _age(self) -> float | None:
        try:
            return time.time() - os.path.getmtime(self.path)
        except OSError:
            return None

    def acquire(self) -> None:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = self._age()
            if age is not None and age > self.stale_seconds:
                # The holder died mid-pass. Taking over is safe: the upsert is
                # idempotent, so the worst case is a trade restated.
                log.warning("taking over a stale accounting lock (age %.0fs)", age)
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
                return self.acquire()
            raise AlreadyRunning(f"another accounting pass holds {self.path}")
        # The pid is for a human reading the file, never for a permission check.
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def resolve_interval(env: dict) -> int:
    """Seconds between passes, floored so a misconfiguration cannot hammer Binance."""
    raw = (env.get("ACCOUNTING_INTERVAL_SECONDS") or "").strip()
    try:
        interval = int(raw) if raw else DEFAULT_INTERVAL_SECONDS
    except ValueError:
        log.warning("unreadable ACCOUNTING_INTERVAL_SECONDS; using %ds", DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    if interval < MIN_INTERVAL_SECONDS:
        log.warning("interval %ds is below the %ds floor; using the floor",
                    interval, MIN_INTERVAL_SECONDS)
        return MIN_INTERVAL_SECONDS
    return interval


class Stopper:
    """Ends the loop on SIGTERM/SIGINT so `docker stop` is a clean exit."""

    def __init__(self):
        self.stop = False

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                # Not the main thread, or a platform without the signal. The
                # loop still exits on its own terms.
                pass

    def _handle(self, *_):
        log.info("stop requested; finishing after this pass")
        self.stop = True


def run_forever(env: dict | None = None, sleeper=time.sleep, stopper: Stopper | None = None) -> int:
    """Sync, sleep, repeat. Returns an exit code."""
    env = os.environ if env is None else env
    interval = resolve_interval(env)
    lock_path = (env.get("ACCOUNTING_LOCK_PATH") or DEFAULT_LOCK_PATH).strip()
    once = (env.get("ACCOUNTING_RUN_ONCE") or "").strip().lower() in ("1", "true", "yes", "on")
    stopper = stopper or Stopper()

    log.info(
        "accounting loop starting | interval=%ds run_once=%s lock=%s",
        interval, once, lock_path,
    )

    while True:
        try:
            with RunLock(lock_path):
                accounting_sync.run_once(env)
        except AlreadyRunning as exc:
            # Not an error: the previous pass is simply still going.
            log.info("skipping this tick | %s", exc)
        except Exception:
            # Every pass is independent and the writes are idempotent, so a
            # failure is retried on the next tick rather than ending the loop.
            # Accounting must not need a human to restart it.
            log.exception("accounting pass failed; retrying at the next interval")
        if once or stopper.stop:
            return 0
        sleeper(interval)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    stopper = Stopper()
    stopper.install()
    sys.exit(run_forever(stopper=stopper))


if __name__ == "__main__":
    main()
