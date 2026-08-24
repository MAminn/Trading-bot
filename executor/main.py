"""Executor service entrypoint.

Binance USD-M Futures execution service.
Implemented modes:
  OFF           - no connectivity, no trading logic.
  TESTNET_READ  - testnet, read-only.
  TESTNET_TRADE - testnet, account enforcement + risk guard + placement.
  LIVE_READ     - mainnet, read-only. Structurally incapable of placement:
                  the client is a read-only facade with no write methods.
  LIVE_TRADE    - mainnet, REAL MONEY. Requires an explicit env acknowledgement
                  and a mandatory per-order USD cap, or it refuses to start.

Default is OFF. No mode is inferred; EXECUTION_MODE must name one explicitly.

Both LIVE modes sign with the CONNECTED USER's Binance keys — the pair they
entered on the website, stored encrypted and served decrypted to this process
by the app. They are never read from this host's environment. A live order must
move the client's funds; signing one with the server operator's keys would be
the wrong wallet, not merely the wrong configuration. If the user has connected
no keys, both LIVE modes refuse every mainnet call and report why.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

ALLOWED_MODES = (
    "OFF",
    "TESTNET_READ",
    "TESTNET_TRADE",
    "LIVE_READ",
    "LIVE_TRADE",
)
IMPLEMENTED_MODES = ALLOWED_MODES

# Modes that may place orders. Membership here — never a string literal — is
# what enables the bracket probe, account enforcement, and the trader client.
TRADE_CAPABLE_MODES = frozenset({"TESTNET_TRADE", "LIVE_TRADE"})
# Modes that touch the real exchange with real funds.
LIVE_MODES = frozenset({"LIVE_READ", "LIVE_TRADE"})

HEARTBEAT_INTERVAL_SECONDS = 60
MAX_CONSECUTIVE_FAILURES = 10

# The mode determines the base URL — never read from env, so a config mistake
# can never point testnet mode at live, or live mode at testnet.
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
# Mainnet USD-M Futures. Not the COIN-M (dapi) or spot host.
LIVE_BASE_URL = "https://fapi.binance.com"

MODE_BASE_URLS = {
    "TESTNET_READ": TESTNET_BASE_URL,
    "TESTNET_TRADE": TESTNET_BASE_URL,
    "LIVE_READ": LIVE_BASE_URL,
    "LIVE_TRADE": LIVE_BASE_URL,
}

# TESTNET ONLY. Testnet keys are throwaway host credentials and stay in the
# environment where they have always been.
#
# The LIVE entries are gone, and their absence is the fix: mainnet credentials
# now come from the connected user's account via the app, so no expression in
# this process can turn an environment variable into a mainnet signing key any
# more. A live mode that reached in here would raise a KeyError rather than
# quietly sign with the wrong wallet.
MODE_CREDENTIAL_ENV = {
    "TESTNET_READ": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
    "TESTNET_TRADE": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
}

# Token for the app's credentials endpoint. Separate from ENGINE_SERVICE_TOKEN
# by design — that one is shared with the signal, config and telemetry routes,
# and key material must not sit behind a token so many endpoints accept.
ENGINE_CREDENTIALS_TOKEN_ENV = "ENGINE_CREDENTIALS_TOKEN"

# Legacy server-wide live keys. Read for exactly one purpose: to warn that they
# are present and ignored. They are never passed to a client, and nothing in
# this process will sign with them.
LEGACY_LIVE_KEY_ENV = "BINANCE_LIVE_API_KEY"
LEGACY_LIVE_SECRET_ENV = "BINANCE_LIVE_API_SECRET"

# LIVE_TRADE will not start unless this env var carries this exact value.
LIVE_TRADING_ACK_ENV = "LIVE_TRADING_ACK"
LIVE_TRADING_ACK_VALUE = "I_UNDERSTAND_REAL_MONEY"

# RETIRED. The per-order dollar cap is gone: it could silently reduce a
# correctly-sized order, which made it a second sizing model rather than a
# safety control. The name is kept ONLY so a host that still has the variable
# set is told, at startup, that it does nothing.
RETIRED_ORDER_CAP_ENV = "LIVE_ORDER_CAP_USD"

SYMBOL = "ETHUSDT"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("executor")


# Re-exported, NOT redefined. These are raised inside UserSession and caught in
# the loops here; a second class of the same name would be a different type, and
# an `except FatalConfigError` against the wrong one would silently stop
# matching — turning a refusal-to-trade into an unhandled crash.
from user_session import EnforcementError, FatalConfigError  # noqa: E402,F401


def _live_env_keys_present() -> bool:
    """Whether the deprecated server-wide live keys are still in the env.

    Presence only. The values are never returned, logged or used — this exists
    so the operator is told the variables are dead weight, not so anything can
    fall back to them.
    """
    return bool(
        os.environ.get(LEGACY_LIVE_KEY_ENV, "").strip()
        or os.environ.get(LEGACY_LIVE_SECRET_ENV, "").strip()
    )


def build_reporter(default_user_id=None):
    """Telemetry reporter, or None when the app API is not configured.

    Telemetry is optional in every mode: an executor with no APP_API_BASE runs
    exactly as it always has, silently.

    `default_user_id` is the single-user executor's recipient. A multi-tenant
    loop passes None and names the user on every report instead, so one reporter
    serves many sessions without ever holding one user's state — a snapshot
    posted to the wrong user would show a client someone else's balance.
    """
    app_api_base = os.environ.get("APP_API_BASE", "").strip()
    engine_service_token = os.environ.get("ENGINE_SERVICE_TOKEN", "").strip()
    if not app_api_base or not engine_service_token:
        return None
    from executor_status import StatusReporter

    return StatusReporter(app_api_base, engine_service_token, default_user_id)


def run_off() -> int:
    log.info("=" * 60)
    log.info("executor starting | mode=OFF")
    log.info("no Binance connectivity, no trading logic")
    log.info("=" * 60)

    # OFF deliberately reports no credential state: which key set would even be
    # meant is undefined here, and a keys_present of either value would mislead.
    #
    # Telemetry is attributed to ENGINE_USER_ID when one is pinned. A host with
    # no pinned user reports nothing here rather than guessing a recipient: OFF
    # has no sessions, so there is no user this snapshot could belong to.
    reporter = build_reporter(
        default_user_id=os.environ.get("ENGINE_USER_ID", "").strip() or None
    )

    while True:
        now = datetime.now(timezone.utc).isoformat()
        log.info("executor alive | mode=OFF | %s", now)
        if reporter is not None:
            from executor_status import build_snapshot

            reporter.report(
                build_snapshot(
                    mode="OFF",
                    env_mode_ceiling="OFF",
                    account=None,
                    positions=None,
                    reconcile=None,
                    keys_present=None,
                    permission_status=None,
                    message="executor idle (mode=OFF)",
                )
            )
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def live_preflight(mode: str) -> int | None:
    """Gate the LIVE_* modes before any client is built.

    Returns None to proceed, or an exit code meaning refuse to start; the caller
    returns it unchanged.

    There is deliberately no per-order dollar ceiling here any more. Order size
    is the user's own configuration — wallet balance x allocation x leverage —
    and an environment variable that could quietly reduce it was a second sizing
    model wearing the name of a safety control. The controls that DO stop a live
    host are unchanged and are all binary: EXECUTION_MODE, LIVE_TRADING_ACK,
    the database's execution_mode, auto_execute_enabled, and the is_running kill
    switch. Each of them stops trading outright rather than silently trading a
    different size than the client asked for.

    Nothing here reads or echoes a credential — only its presence is checked,
    upstream in the caller.
    """
    if mode not in LIVE_MODES:
        return None

    # LIVE_READ needs no acknowledgement: it cannot place. LIVE_TRADE does.
    if mode != "LIVE_TRADE":
        log.warning(
            "LIVE_READ | mainnet read-only | placement is structurally "
            "unavailable in this mode"
        )
        return None

    ack = os.environ.get(LIVE_TRADING_ACK_ENV, "").strip()
    if ack != LIVE_TRADING_ACK_VALUE:
        log.error(
            "REFUSING TO START | %s=%s requires %s=%s (got %s) — real funds are "
            "at risk in this mode",
            "EXECUTION_MODE",
            mode,
            LIVE_TRADING_ACK_ENV,
            LIVE_TRADING_ACK_VALUE,
            "an empty value" if not ack else "a different value",
        )
        return 1

    if os.environ.get(RETIRED_ORDER_CAP_ENV, "").strip():
        # Retired, and loudly: an operator who still has it set would otherwise
        # believe orders were bounded by it.
        log.warning(
            "IGNORING %s | the per-order dollar cap has been removed. Order size "
            "is the client's own wallet balance x allocation x leverage, and "
            "nothing on this host reduces it. Use EXECUTION_MODE, the database "
            "execution_mode, auto-execute or the Stop switch to stop trading.",
            RETIRED_ORDER_CAP_ENV,
        )

    log.warning("=" * 60)
    log.warning("LIVE_TRADE | REAL MONEY | mainnet USD-M futures")
    log.warning("acknowledgement accepted via %s", LIVE_TRADING_ACK_ENV)
    log.warning(
        "order size = the client's Binance totalWalletBalance x their "
        "allocation %% x their leverage, bounded only by Binance's own limits"
    )
    log.warning("=" * 60)
    return None


def build_host_limits(mode: str, app_api_base: str, engine_service_token: str):
    """What this host permits, for every user on it.

    Assembled once and passed to every session. A session never reads the
    environment itself, so no user can acquire capability the host was not
    started with, and the .env remains the ceiling exactly as before.
    """
    from user_session import HostLimits

    return HostLimits(
        env_mode=mode,
        base_url=MODE_BASE_URLS[mode],
        trade_capable=mode in TRADE_CAPABLE_MODES,
        # Re-read as a placement gate rather than trusted from startup: the
        # acknowledgement is what distinguishes a host that may risk real funds.
        ack_present=(
            os.environ.get(LIVE_TRADING_ACK_ENV, "").strip() == LIVE_TRADING_ACK_VALUE
        ),
        app_api_base=app_api_base,
        engine_service_token=engine_service_token,
        is_live=mode in LIVE_MODES,
    )


class EnvCredentials:
    """A testnet host's own key pair, shaped like a fetched credential.

    Exists so UserSession has ONE credential type to hold rather than a pair of
    branches. Renders opaquely for the same reason UserCredentials does.
    """

    __slots__ = ("api_key", "api_secret", "last4")

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.last4 = api_key[-4:] if api_key else ""

    def __repr__(self) -> str:
        return f"<EnvCredentials last4={self.last4!r}>"

    __str__ = __repr__


def run_executor(mode: str) -> int:
    """Runs every non-OFF mode.

    Two shapes, one trading cycle. `ENGINE_USER_ID` pins the executor to a single
    user (the original behaviour, kept for smoke tests and single-client hosts);
    leaving it unset runs the multi-tenant loop, which asks the app which clients
    are active and runs a session for each.

    Both drive `UserSession.run_cycle`. There is deliberately no second copy of
    the gate logic: a safety check enforced in one loop and forgotten in the
    other would be worse than no check at all.
    """
    from user_credentials import CredentialsUnavailable, UserCredentialsClient
    from user_session import UserSession

    refusal = live_preflight(mode)
    if refusal is not None:
        return refusal

    app_api_base = os.environ.get("APP_API_BASE", "").strip()
    engine_service_token = os.environ.get("ENGINE_SERVICE_TOKEN", "").strip()
    if not app_api_base or not engine_service_token:
        log.error(
            "APP_API_BASE and ENGINE_SERVICE_TOKEN must be set for %s mode", mode
        )
        return 1

    # Optional. Present pins this process to one user; absent means multi-tenant.
    pinned_user_id = os.environ.get("ENGINE_USER_ID", "").strip() or None

    # ---- credentials ------------------------------------------------------ #
    # Testnet signs with this host's own throwaway keys, exactly as before.
    # Mainnet signs with each CONNECTED USER's keys, fetched from the app per
    # user. This host's environment is not where a client's keys live.
    credentials_token = ""
    env_credentials = None
    if mode in LIVE_MODES:
        credentials_token = os.environ.get(ENGINE_CREDENTIALS_TOKEN_ENV, "").strip()
        if not credentials_token:
            log.error(
                "REFUSING TO START | %s must be set for %s mode — live credentials "
                "come from each connected user's account, and this is the token "
                "used to ask for them",
                ENGINE_CREDENTIALS_TOKEN_ENV,
                mode,
            )
            return 1
        if credentials_token == engine_service_token:
            # Reusing the service token would hand key material to every holder
            # of the token the ingest, config and roster routes already accept.
            # Checked on both sides: the app refuses to serve, this refuses to ask.
            log.error(
                "REFUSING TO START | %s must differ from ENGINE_SERVICE_TOKEN — key "
                "material must not sit behind the token shared with the signal, "
                "config, roster and telemetry routes",
                ENGINE_CREDENTIALS_TOKEN_ENV,
            )
            return 1
        if _live_env_keys_present():
            log.warning(
                "IGNORING %s / %s | live credentials come from each connected "
                "user's account; these environment values are legacy, are never "
                "used to sign a mainnet order, and should be removed",
                LEGACY_LIVE_KEY_ENV,
                LEGACY_LIVE_SECRET_ENV,
            )
    else:
        key_env, secret_env = MODE_CREDENTIAL_ENV[mode]
        api_key = os.environ.get(key_env, "").strip()
        api_secret = os.environ.get(secret_env, "").strip()
        if not api_key or not api_secret:
            log.error("%s and %s must be set for %s mode", key_env, secret_env, mode)
            return 1
        env_credentials = EnvCredentials(api_key, api_secret)

    start_after = os.environ.get("CONSUMER_START_AFTER", "").strip() or None
    if start_after is not None:
        try:
            datetime.fromisoformat(start_after.replace("Z", "+00:00"))
        except ValueError:
            log.error(
                "CONSUMER_START_AFTER=%r is not a valid ISO 8601 timestamp", start_after
            )
            return 1

    smoke_test = os.environ.get("LIVE_SMOKE_TEST", "").strip() == "1"
    smoke_side = os.environ.get("LIVE_SMOKE_SIDE", "").strip().upper() or "LONG"
    if smoke_test:
        if mode not in TRADE_CAPABLE_MODES:
            log.error("LIVE_SMOKE_TEST=1 requires a trade-capable mode, not %s", mode)
            return 1
        if smoke_side not in ("LONG", "SHORT"):
            log.error("LIVE_SMOKE_SIDE=%r must be LONG or SHORT", smoke_side)
            return 1
        if pinned_user_id is None:
            # "Place one synthetic order and exit" is not a thing to do to a
            # process holding several clients' funds, and there would be no
            # defensible way to choose whose wallet to do it with.
            log.error(
                "REFUSING TO START | LIVE_SMOKE_TEST=1 requires ENGINE_USER_ID — a "
                "smoke test must name the account it will trade"
            )
            return 1

    limits = build_host_limits(mode, app_api_base, engine_service_token)

    log.info("=" * 60)
    log.info("executor starting | mode=%s | symbol=%s", mode, SYMBOL)
    log.info("base_url=%s (hardcoded for this mode)", limits.base_url)
    if limits.trade_capable:
        log.info("account enforcement + risk guard + order placement active")
    else:
        log.info("read-only mode: no write calls to Binance")
    log.info("=" * 60)

    reporter = build_reporter(default_user_id=pinned_user_id)

    def make_session(user_id: str):
        """Build one user's isolated session.

        Each live session gets its OWN credentials client, bound to its own user
        id. That binding is what makes it impossible for one user's cycle to
        fetch, or sign with, another user's keys.
        """
        credentials_client = None
        if mode in LIVE_MODES:
            credentials_client = UserCredentialsClient(
                app_api_base, credentials_token, user_id
            )
        return UserSession(
            user_id=user_id,
            limits=limits,
            credentials_client=credentials_client,
            env_credentials=env_credentials,
            reporter=reporter,
            start_after=start_after,
            symbol=SYMBOL,
            smoke_side=smoke_side if smoke_test else None,
        )

    if pinned_user_id is not None:
        log.info("single-user mode | ENGINE_USER_ID=%s", pinned_user_id)
        return _run_single_user(make_session(pinned_user_id), smoke_test)

    log.info(
        "multi-tenant mode | ENGINE_USER_ID is unset | this host executes every "
        "active client, and onboarding needs no operator action"
    )
    from multi_tenant import ActiveUserRoster, MultiTenantExecutor

    loop = MultiTenantExecutor(
        roster=ActiveUserRoster(app_api_base, engine_service_token),
        session_factory=make_session,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    return loop.run_forever()


def _run_single_user(session, smoke_test: bool) -> int:
    """The original one-user loop, unchanged in behaviour.

    Kept for smoke tests and single-client hosts. Failures retire the process
    here, which is right when the process serves exactly one account — and is
    precisely why the multi-tenant loop parks a user instead.
    """
    from binance_client import BinanceAPIError, RateLimitError
    from executor_status import permission_status_for_error
    from signal_consumer import SignalConsumerError
    from user_credentials import CredentialsUnavailable
    from user_session import EnforcementError, FatalConfigError

    consecutive_failures = 0
    while True:
        try:
            exit_code = session.run_cycle()
            if exit_code is not None:
                return exit_code
            consecutive_failures = 0
        except FatalConfigError as exc:
            session.report(
                message=f"halted: {exc}", permission=permission_status_for_error(exc)
            )
            return 1
        except RateLimitError as exc:
            if session.smoke_started:
                log.error(
                    "SMOKE RATE LIMITED MID-FLIGHT | %s | NOT retrying — verify the "
                    "position on Binance and close it manually if it is open",
                    exc,
                )
                return 1
            backoff = max(exc.retry_after, 60)
            log.error("RATE LIMITED | backing off %ds", backoff)
            session.report(message=f"rate limited, backing off {backoff}s")
            time.sleep(backoff)
            continue
        except (
            BinanceAPIError,
            SignalConsumerError,
            EnforcementError,
            CredentialsUnavailable,
            OSError,
        ) as exc:
            if session.smoke_started:
                log.error(
                    "SMOKE FAILED MID-FLIGHT | %s | NOT retrying — verify the "
                    "position on Binance and close it manually if it is open",
                    exc,
                )
                return 1
            consecutive_failures += 1
            log.error(
                "cycle failed (%d/%d consecutive): %s",
                consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
                exc,
            )
            # A failed cycle clears balances and positions back to null rather
            # than leaving the last good reading in place: those numbers are
            # stale by definition, and a dashboard must never present a stale
            # reading as a current one.
            session.report(
                message=f"cycle failed ({consecutive_failures}/"
                f"{MAX_CONSECUTIVE_FAILURES}): {exc}",
                permission=permission_status_for_error(exc),
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error("10 consecutive failed cycles — exiting")
                return 1
        time.sleep(HEARTBEAT_INTERVAL_SECONDS)


def main() -> int:
    mode = os.environ.get("EXECUTION_MODE", "").strip() or "OFF"

    if mode not in ALLOWED_MODES:
        log.error(
            "Invalid EXECUTION_MODE=%r — allowed values: %s", mode, ", ".join(ALLOWED_MODES)
        )
        return 1

    if mode not in IMPLEMENTED_MODES:
        log.error(
            "EXECUTION_MODE=%s requested but this build supports %s only — refusing to start",
            mode,
            "/".join(IMPLEMENTED_MODES),
        )
        return 1

    if mode == "OFF":
        return run_off()
    return run_executor(mode)


if __name__ == "__main__":
    sys.exit(main())
