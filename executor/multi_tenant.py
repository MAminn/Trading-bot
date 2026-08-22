"""The multi-tenant executor loop: every active client, one process, no operator.

Why this exists
---------------
The executor previously took a single `ENGINE_USER_ID` from its environment, so
onboarding a client meant an operator editing `.env` and restarting. A client
could sign up, connect their keys and press Start, and nothing would happen
until someone noticed. This loop removes the operator from the path entirely:
it asks the app which users are asking to be executed, and runs each one.

Isolation
---------
Every user gets their own `UserSession`, which owns their credentials, their
Binance client, their consumer (cursor, sizing, reconciler) and their risk
guard. Nothing is shared between sessions except read-only host limits. The
guarantees this loop adds on top:

  * One user's failure is caught, counted against that user, and reported on
    that user's telemetry. It never propagates into another user's cycle and
    never ends the loop.
  * A user who disappears from the roster is retired: their session is dropped
    and their client along with it, so the process stops holding a means of
    signing for someone it is no longer executing.
  * A roster fetch failure changes nothing. The existing sessions keep running
    on what was last known; a loop that emptied its roster on a network blip
    would silently stop trading for everyone.

Fairness and blast radius
-------------------------
Users are processed in roster order, one at a time. A slow or failing user costs
the others latency but never correctness — every gate is re-evaluated from
values read in that user's own cycle.
"""

import logging
import time

import requests

log = logging.getLogger("executor.multi")

REQUEST_TIMEOUT_SECONDS = 15
ROSTER_PATH = "/api/public/engine/users/active"

# Per-user failure budget. A user whose cycles keep failing is parked rather
# than retried forever, and — unlike the single-user executor, which exits —
# parking one user leaves every other user running.
MAX_CONSECUTIVE_FAILURES = 10


class RosterUnavailable(Exception):
    """The active-user roster could not be fetched. Transient: keep what we have."""


class ActiveUserRoster:
    """Fetches the set of users this executor should be running."""

    def __init__(self, app_api_base: str, engine_service_token: str):
        self._url = f"{app_api_base.rstrip('/')}{ROSTER_PATH}"
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {engine_service_token}"}
        )

    def fetch(self) -> list:
        """User ids the app says are asking to be executed.

        The app decides eligibility; this executor does not second-guess it,
        because the alternative is two definitions of "active" that can disagree.
        What the executor still decides, per user and every cycle, is whether
        that user may actually trade — the roster grants attention, never
        capability.
        """
        try:
            resp = self._session.get(self._url, timeout=REQUEST_TIMEOUT_SECONDS)
        except OSError as exc:
            raise RosterUnavailable(f"roster fetch failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise RosterUnavailable(
                f"roster endpoint rejected this executor (HTTP {resp.status_code}) "
                "— check ENGINE_SERVICE_TOKEN"
            )
        if not 200 <= resp.status_code < 300:
            raise RosterUnavailable(f"roster endpoint returned HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise RosterUnavailable("roster endpoint returned a non-JSON body") from exc

        users = body.get("users") if isinstance(body, dict) else body
        if not isinstance(users, list):
            raise RosterUnavailable("roster response had no user list")

        out = []
        seen = set()
        for entry in users:
            user_id = entry.get("user_id") if isinstance(entry, dict) else entry
            if not isinstance(user_id, str) or not user_id.strip():
                continue
            user_id = user_id.strip()
            # A duplicated id would build two sessions for one account, each
            # unaware of the other's position. Refuse rather than dedupe silently
            # further down where it would be invisible.
            if user_id in seen:
                log.warning("roster contained %s twice — ignoring the duplicate", user_id)
                continue
            seen.add(user_id)
            out.append(user_id)
        return out


class MultiTenantExecutor:
    """Runs one cycle per active user, forever."""

    def __init__(
        self,
        roster,
        session_factory,
        heartbeat_interval_seconds: int,
        sleeper=time.sleep,
    ):
        self._roster = roster
        # Builds a UserSession for a user id. Injected so the loop can be tested
        # without a network, and so this class never learns how credentials are
        # obtained.
        self._session_factory = session_factory
        self._interval = heartbeat_interval_seconds
        self._sleep = sleeper
        self._sessions: dict = {}
        # Users parked after exhausting their failure budget. Kept out of the
        # active set but remembered, so the log does not re-announce them every
        # cycle and a roster change can revive them.
        self._parked: set = set()

    @property
    def sessions(self) -> dict:
        return self._sessions

    @property
    def parked(self) -> set:
        return self._parked

    def sync_roster(self) -> None:
        """Bring the session set in line with the roster.

        A fetch failure is swallowed after logging: the sessions already built
        keep running. Emptying the roster on a transient error would stop trading
        for every client at once, which is far worse than acting on a slightly
        stale list — every per-user gate is still re-read from the database on
        that user's own cycle.
        """
        try:
            user_ids = self._roster.fetch()
        except RosterUnavailable as exc:
            log.warning(
                "roster unavailable (%s) | continuing with %d known session(s)",
                exc,
                len(self._sessions),
            )
            return

        wanted = set(user_ids)

        for user_id in user_ids:
            if user_id in self._sessions or user_id in self._parked:
                continue
            try:
                self._sessions[user_id] = self._session_factory(user_id)
            except Exception as exc:  # noqa: BLE001
                # A user whose session cannot even be constructed must not stop
                # the others from being built.
                log.error("could not start a session for user=%s: %s", user_id, exc)
                continue
            log.warning("USER ADDED | user=%s | now executing %d user(s)", user_id, len(self._sessions))

        for user_id in list(self._sessions):
            if user_id not in wanted:
                # Dropping the session drops its Binance client with it. The
                # process must not keep a means of signing for a user it is no
                # longer executing.
                self._sessions.pop(user_id, None)
                log.warning("USER REMOVED | user=%s | no longer on the roster", user_id)

        for user_id in list(self._parked):
            if user_id not in wanted:
                self._parked.discard(user_id)

    def run_user_cycle(self, session) -> None:
        """One user's cycle, with every failure contained to that user.

        This is the isolation boundary. Nothing raised by one user's cycle may
        reach the loop, because the loop serves everyone else.
        """
        from binance_client import BinanceAPIError, RateLimitError
        from signal_consumer import SignalConsumerError
        from user_credentials import CredentialsUnavailable
        from user_session import EnforcementError, FatalConfigError

        try:
            session.run_cycle()
            session.consecutive_failures = 0
            return
        except FatalConfigError as exc:
            # Fatal for this user, not for the host. The single-user executor
            # exited here; parking is the multi-tenant equivalent and leaves
            # every other client trading.
            log.error("HALTED | user=%s | %s", session.user_id, exc)
            session.report(message=f"halted: {exc}")
            self._park(session.user_id, str(exc))
            return
        except RateLimitError as exc:
            # Binance rate limits are per API key, so this bounds one user.
            # Counted as a failure without a sleep: the loop's own interval and
            # the other users' cycles already provide the backoff.
            log.error("RATE LIMITED | user=%s | %s", session.user_id, exc)
            session.report(message=f"rate limited: {exc}")
        except (
            BinanceAPIError,
            SignalConsumerError,
            EnforcementError,
            CredentialsUnavailable,
            OSError,
        ) as exc:
            log.error("cycle failed | user=%s | %s", session.user_id, exc)
            self._report_failure(session, exc)
        except Exception as exc:  # noqa: BLE001
            # The catch-all is deliberate and is the whole point of this method.
            # An unforeseen error in one user's cycle must degrade that user, not
            # take down a process that is executing other people's money.
            log.exception("unexpected error | user=%s | %s", session.user_id, exc)
            self._report_failure(session, exc)

        session.consecutive_failures += 1
        if session.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._park(
                session.user_id,
                f"{session.consecutive_failures} consecutive failed cycles",
            )

    def _report_failure(self, session, exc) -> None:
        """Report a failed cycle for one user.

        Balances and positions are reported as null rather than left at the last
        good reading: those numbers are stale by definition here, and a dashboard
        must never present a stale reading as a current one.
        """
        from executor_status import permission_status_for_error

        session.report(
            message=(
                f"cycle failed ({session.consecutive_failures + 1}/"
                f"{MAX_CONSECUTIVE_FAILURES}): {exc}"
            ),
            permission=permission_status_for_error(exc),
        )

    def _park(self, user_id: str, reason: str) -> None:
        self._parked.add(user_id)
        self._sessions.pop(user_id, None)
        log.error(
            "USER PARKED | user=%s | %s | this user is no longer being executed; "
            "every other user continues",
            user_id,
            reason,
        )

    def run_forever(self, max_iterations=None) -> int:
        """The loop. `max_iterations` exists for the tests and is None in production."""
        iterations = 0
        log.info("=" * 60)
        log.info("multi-tenant executor | one session per active user")
        log.info("=" * 60)
        while True:
            self.sync_roster()

            if not self._sessions:
                log.info(
                    "no active users | %d parked | waiting for a client to enable "
                    "execution",
                    len(self._parked),
                )

            # A copy: sync_roster runs at the top of the next iteration, but a
            # session factory or a park during this pass must not mutate the
            # sequence being walked.
            for user_id in list(self._sessions):
                session = self._sessions.get(user_id)
                if session is None:
                    continue
                self.run_user_cycle(session)

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return 0
            self._sleep(self._interval)
