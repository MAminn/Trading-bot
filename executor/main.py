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
from decimal import Decimal, InvalidOperation

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

# Strategy stop distance assumed purely for the liquidation-vs-stop warning.
SL_PCT_ASSUMED = 0.015

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

# LIVE_TRADE will not start unless a positive per-order notional cap is set.
# It is an absolute ceiling applied on top of, and independently of, every
# config-driven sizing rule — including full_capital, which has no internal
# notional ceiling of its own.
LIVE_ORDER_CAP_ENV = "LIVE_ORDER_CAP_USD"
# The cap itself may not exceed the guard's absolute allocation-mode backstop.
LIVE_ORDER_CAP_MAX_USD = Decimal("500")

SYMBOL = "ETHUSDT"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("executor")


class EnforcementError(Exception):
    """Account-config enforcement failed in a retryable way."""


class FatalConfigError(Exception):
    """Account config could not be verified — refuse to run trade-capable."""


def read_env_order_cap_for_telemetry():
    """This host's configured LIVE_ORDER_CAP_USD, for REPORTING only.

    live_preflight() deliberately parses the cap only in LIVE_TRADE — it is the
    mode where the cap is mandatory and where refusing to start on a bad value
    is correct. Every other mode leaves it None, which is right for enforcement
    (a mode that cannot place needs no cap) but wrong for telemetry: the
    dashboard then shows "host ceiling: —" on a host that plainly has one
    configured, and the operator cannot see the ceiling they would be bounded
    by until they are already trading.

    So this reads the same variable leniently and independently. It feeds the
    heartbeat and nothing else — it is never passed to the RiskGuard, the
    consumer, or any sizing path, so it cannot change what is traded. An
    unreadable or non-positive value reports None rather than guessing.
    """
    raw = os.environ.get(LIVE_ORDER_CAP_ENV, "").strip()
    if not raw:
        return None
    try:
        cap = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return cap if cap > 0 else None


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


def build_reporter():
    """Telemetry reporter, or None when the app API is not configured.

    Telemetry is optional in every mode: an executor with no APP_API_BASE runs
    exactly as it always has, silently."""
    app_api_base = os.environ.get("APP_API_BASE", "").strip()
    engine_service_token = os.environ.get("ENGINE_SERVICE_TOKEN", "").strip()
    engine_user_id = os.environ.get("ENGINE_USER_ID", "").strip()
    if not app_api_base or not engine_service_token or not engine_user_id:
        return None
    from executor_status import StatusReporter

    return StatusReporter(app_api_base, engine_service_token, engine_user_id)


def run_off() -> int:
    log.info("=" * 60)
    log.info("executor starting | mode=OFF")
    log.info("no Binance connectivity, no trading logic")
    log.info("=" * 60)

    # OFF deliberately reports no credential state: which key set would even be
    # meant is undefined here, and a keys_present of either value would mislead.
    reporter = build_reporter()

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


def _extract_filters(symbol_info: dict) -> tuple[str, str, str]:
    """Return (tick_size, step_size, min_notional) from a symbol's filters."""
    tick_size = step_size = min_notional = "?"
    for f in symbol_info.get("filters", []):
        ftype = f.get("filterType")
        if ftype == "PRICE_FILTER":
            tick_size = f.get("tickSize", "?")
        elif ftype == "LOT_SIZE":
            step_size = f.get("stepSize", "?")
        elif ftype == "MIN_NOTIONAL":
            min_notional = f.get("notional", f.get("minNotional", "?"))
    return tick_size, step_size, min_notional


def live_preflight(mode: str) -> tuple[int | None, Decimal | None]:
    """Gate the LIVE_* modes before any client is built.

    Returns (exit_code, live_order_cap_usd). A non-None exit_code means refuse
    to start; the caller returns it unchanged. The cap is None for every mode
    that is not LIVE_TRADE.

    Nothing here reads or echoes a credential — only its presence is checked,
    upstream in the caller.
    """
    if mode not in LIVE_MODES:
        return None, None

    # LIVE_READ needs no acknowledgement: it cannot place. LIVE_TRADE does.
    if mode != "LIVE_TRADE":
        log.warning(
            "LIVE_READ | mainnet read-only | placement is structurally "
            "unavailable in this mode"
        )
        return None, None

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
        return 1, None

    raw_cap = os.environ.get(LIVE_ORDER_CAP_ENV, "").strip()
    if not raw_cap:
        log.error(
            "REFUSING TO START | %s is mandatory in LIVE_TRADE — it is the "
            "absolute per-order notional ceiling and has no default",
            LIVE_ORDER_CAP_ENV,
        )
        return 1, None
    try:
        cap = Decimal(raw_cap)
    except (InvalidOperation, ValueError):
        log.error("REFUSING TO START | %s=%r is not a number", LIVE_ORDER_CAP_ENV, raw_cap)
        return 1, None
    if cap <= 0:
        log.error("REFUSING TO START | %s=%s must be positive", LIVE_ORDER_CAP_ENV, cap)
        return 1, None
    if cap > LIVE_ORDER_CAP_MAX_USD:
        log.error(
            "REFUSING TO START | %s=%s exceeds the %s ceiling this build permits",
            LIVE_ORDER_CAP_ENV,
            cap,
            LIVE_ORDER_CAP_MAX_USD,
        )
        return 1, None

    log.warning("=" * 60)
    log.warning("LIVE_TRADE | REAL MONEY | mainnet USD-M futures")
    log.warning("acknowledgement accepted via %s", LIVE_TRADING_ACK_ENV)
    log.warning("absolute per-order cap | %s=%s USD", LIVE_ORDER_CAP_ENV, cap)
    log.warning("=" * 60)
    return None, cap


def run_executor(mode: str) -> int:
    """Runs every non-OFF mode: TESTNET_READ / LIVE_READ (pure read) and
    TESTNET_TRADE / LIVE_TRADE (read + account enforcement + risk guard +
    order placement)."""
    from binance_client import (
        BinanceAPIError,
        BinanceFuturesClient,
        RateLimitError,
        ReadOnlyFuturesClient,
    )
    from executor_status import build_snapshot, permission_status_for_error
    from live_controls import (
        is_trade_capable,
        placement_block_reason,
        resolve_effective_mode,
    )
    from signal_consumer import SignalConsumer, SignalConsumerError
    from user_credentials import CredentialsUnavailable, UserCredentialsClient

    # What this HOST is permitted to do. The database may narrow it per cycle,
    # never widen it, so this stays the ceiling for the life of the process.
    trade_capable = mode in TRADE_CAPABLE_MODES
    base_url = MODE_BASE_URLS[mode]

    # Re-checked as a placement gate every cycle, not just at startup: the
    # acknowledgement is what distinguishes a host that may risk real funds.
    ack_present = (
        os.environ.get(LIVE_TRADING_ACK_ENV, "").strip() == LIVE_TRADING_ACK_VALUE
    )

    refusal, live_order_cap_usd = live_preflight(mode)
    if refusal is not None:
        return refusal

    # Validated before credentials, because on a live host the app IS the
    # credential source and there is nothing to resolve without it.
    app_api_base = os.environ.get("APP_API_BASE", "").strip()
    engine_service_token = os.environ.get("ENGINE_SERVICE_TOKEN", "").strip()
    engine_user_id = os.environ.get("ENGINE_USER_ID", "").strip()
    if not app_api_base or not engine_service_token or not engine_user_id:
        log.error(
            "APP_API_BASE, ENGINE_SERVICE_TOKEN and ENGINE_USER_ID must be set "
            "for %s mode",
            mode,
        )
        return 1

    # ---- credentials ------------------------------------------------------ #
    # Testnet signs with this host's own throwaway keys, exactly as before.
    # Mainnet signs with the CONNECTED USER's keys, fetched from the app. This
    # is the whole point of the split: a live order must move the client's
    # funds, and this host's environment is not where the client's keys live.
    api_key = api_secret = ""
    credentials_client = None
    if mode in LIVE_MODES:
        credentials_token = os.environ.get(ENGINE_CREDENTIALS_TOKEN_ENV, "").strip()
        if not credentials_token:
            log.error(
                "REFUSING TO START | %s must be set for %s mode — live "
                "credentials come from the connected user's account, and this "
                "is the token used to ask for them",
                ENGINE_CREDENTIALS_TOKEN_ENV,
                mode,
            )
            return 1
        if credentials_token == engine_service_token:
            # Reusing the service token would hand key material to every holder
            # of the token the ingest and config routes already accept — the
            # exact widening the separate token exists to prevent. Checked on
            # both sides: the app refuses to serve, and this refuses to ask.
            log.error(
                "REFUSING TO START | %s must differ from ENGINE_SERVICE_TOKEN — "
                "key material must not sit behind the token shared with the "
                "signal, config and telemetry routes",
                ENGINE_CREDENTIALS_TOKEN_ENV,
            )
            return 1
        credentials_client = UserCredentialsClient(
            app_api_base, credentials_token, engine_user_id
        )
        if _live_env_keys_present():
            log.warning(
                "IGNORING %s / %s | live credentials now come from the connected "
                "user's account; these environment values are legacy, are never "
                "used to sign a mainnet order, and should be removed",
                LEGACY_LIVE_KEY_ENV,
                LEGACY_LIVE_SECRET_ENV,
            )
        # Deliberately NOT fetched here. Resolution happens inside the cycle so
        # one code path covers every case that actually occurs in production:
        # a first start with nothing connected, a client who connects keys while
        # the executor is already running, a rotation, and a disconnection.
    else:
        key_env, secret_env = MODE_CREDENTIAL_ENV[mode]
        api_key = os.environ.get(key_env, "").strip()
        api_secret = os.environ.get(secret_env, "").strip()
        if not api_key or not api_secret:
            log.error("%s and %s must be set for %s mode", key_env, secret_env, mode)
            return 1

    start_after = os.environ.get("CONSUMER_START_AFTER", "").strip() or None
    if start_after is not None:
        try:
            datetime.fromisoformat(start_after.replace("Z", "+00:00"))
        except ValueError:
            log.error(
                "CONSUMER_START_AFTER=%r is not a valid ISO 8601 timestamp", start_after
            )
            return 1
        log.info("consumer cursor override: starting after %s", start_after)

    smoke_test = os.environ.get("LIVE_SMOKE_TEST", "").strip() == "1"
    if smoke_test and not trade_capable:
        log.error("LIVE_SMOKE_TEST=1 requires a trade-capable mode, not %s", mode)
        return 1
    smoke_side = os.environ.get("LIVE_SMOKE_SIDE", "").strip().upper() or "LONG"
    if smoke_test and smoke_side not in ("LONG", "SHORT"):
        log.error("LIVE_SMOKE_SIDE=%r must be LONG or SHORT", smoke_side)
        return 1

    log.info("=" * 60)
    log.info("executor starting | mode=%s | symbol=%s", mode, SYMBOL)
    log.info("base_url=%s (hardcoded for this mode)", base_url)
    if trade_capable:
        log.info("account enforcement + risk guard + order placement active")
    else:
        log.info("read-only mode: no write calls to Binance")
    if smoke_test:
        log.warning(
            "SMOKE TEST ARMED | one synthetic %s OPEN then CLOSE, then exit | "
            "natural ML signals are NOT processed this run",
            smoke_side,
        )
    log.info("=" * 60)

    # Read modes get a facade that does not define set_leverage, set_margin_type
    # or place_market_order at all. Placement is not merely gated off — the
    # method does not exist on the object, so no code path can reach it.
    def build_client(key: str, secret: str):
        return (
            BinanceFuturesClient(base_url, key, secret)
            if trade_capable
            else ReadOnlyFuturesClient(base_url, key, secret)
        )

    # Testnet binds its client once, here. A live host starts with NO client at
    # all: it has no credentials until the app has been asked for the user's,
    # and holding an unauthenticated mainnet client would not be safer than
    # holding none — it is the same object, one assignment away from signing.
    client = None if credentials_client is not None else build_client(api_key, api_secret)
    # The pair currently bound into `client`, so a rotation is detectable.
    active_credentials = None
    # Reported as keys_present. None until a live host has actually asked:
    # "not yet known" is not the same claim as "none connected".
    credentials_present = None if credentials_client is not None else True

    risk_guard = None
    if trade_capable:
        from risk_guard import RiskGuard

        risk_guard = RiskGuard(
            max_notional_usd=100,
            max_leverage=1,
            live_cap_usd=live_order_cap_usd,
        )

    consumer = SignalConsumer(
        app_api_base,
        engine_service_token,
        engine_user_id,
        mode,
        SYMBOL,
        start_after=start_after,
        risk_guard=risk_guard,
        # Starts detached. The trader is attached per cycle by set_trader(),
        # and only when the EFFECTIVE mode is trade-capable — so the process
        # begins in the closed position and stays there until the database has
        # actually been read.
        binance_trader=None,
        live_order_cap_usd=live_order_cap_usd,
    )

    # Telemetry only. The env vars it needs were validated above, so this is
    # non-None here in every non-OFF mode.
    reporter = build_reporter()

    # Reporting copy of the host cap. Never reaches an enforcement path — the
    # cap that binds orders is still the one live_preflight() returned.
    env_order_cap_reported = read_env_order_cap_for_telemetry()

    # The mode this cycle actually runs in: env capability AND database request.
    # Starts at OFF so anything that reads it before the first successful config
    # refresh sees the closed position.
    effective_mode = "OFF"
    orders_enabled = False
    block_reason_live: str | None = None

    def report(account=None, positions=None, message=None, permission=None):
        """Send one telemetry snapshot. Contains its own failures — a telemetry
        problem must never disturb a trading cycle."""
        if reporter is None:
            return
        reporter.report(
            build_snapshot(
                mode=effective_mode,
                # `mode` here is the .env value: the ceiling, not the outcome.
                env_mode_ceiling=mode,
                db_execution_mode=consumer.db_execution_mode,
                auto_execute_enabled=consumer.db_auto_execute_enabled,
                live_order_cap_usd=consumer.live_order_cap_usd,
                # Read from the environment directly rather than from the
                # consumer's ceiling: outside LIVE_TRADE the consumer has no
                # ceiling (preflight never parses one), and reporting None
                # there would hide a cap this host really does have configured.
                live_order_cap_env_max=env_order_cap_reported,
                orders_enabled=orders_enabled,
                blocked_reason=block_reason_live,
                account=account,
                positions=positions,
                reconcile=consumer.last_reconcile,
                keys_present=credentials_present,
                permission_status=permission,
                message=message,
            )
        )

    # Exchange-authoritative ceiling, filled by the one-time bracket probe.
    exchange_max_leverage: int | None = None

    def probe_leverage_brackets() -> None:
        """One-time trade-capable probe of the symbol's leverage brackets.

        The exchange is authoritative: no order may be sized or placed before
        the ceiling and the bracket ladder are known, so a failure here is
        retryable rather than ignorable.

        The FULL ladder is retained. brackets[0] is the highest-leverage tier
        and therefore carries the SMALLEST notionalCap — correct as the leverage
        ceiling, but wrong as the notional cap for any lower configured
        leverage. The applicable tier depends on configured leverage, which
        changes on config refresh, so the consumer re-selects it there."""
        nonlocal exchange_max_leverage
        try:
            brackets = client.get_leverage_brackets(SYMBOL)
        except (BinanceAPIError, OSError) as exc:
            raise EnforcementError(f"leverage bracket probe failed: {exc}") from exc
        if not brackets:
            raise EnforcementError(f"no leverage brackets returned for {SYMBOL}")

        ladder: list[dict] = []
        for entry in brackets:
            try:
                ladder.append(
                    {
                        "initialLeverage": int(entry["initialLeverage"]),
                        "notionalCap": Decimal(str(entry["notionalCap"])),
                    }
                )
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise EnforcementError(
                    f"unreadable leverage bracket: {entry!r}"
                ) from exc

        # Unchanged: the highest-leverage tier still supplies the ceiling.
        max_leverage = ladder[0]["initialLeverage"]

        log.info("BRACKET LADDER | %s | tiers=%d", SYMBOL, len(ladder))
        for i, tier in enumerate(ladder):
            log.info(
                "BRACKET | tier=%d | initialLeverage=%dx | notionalCap=%s",
                i,
                tier["initialLeverage"],
                tier["notionalCap"],
            )

        exchange_max_leverage = max_leverage
        consumer.set_leverage_limits(max_leverage, ladder)
        if risk_guard is not None:
            risk_guard.update_limits(max_leverage=max_leverage)

    def enforce_account_config(desired_leverage: int) -> bool:
        """Bring the account in line with the configured leverage on isolated
        margin. Returns True when the account matches, False when it does not
        and could not be fixed (the caller then blocks OPENs).

        A leverage mismatch NEVER raises: an already-open position must stay
        closable. FatalConfigError is reserved for isolated margin, which is
        non-negotiable.
        """
        desired = int(desired_leverage)
        if exchange_max_leverage is not None and desired > exchange_max_leverage:
            log.warning(
                "CLAMPED | configured leverage %dx exceeds %s exchange max %dx "
                "— using %dx",
                desired,
                SYMBOL,
                exchange_max_leverage,
                exchange_max_leverage,
            )
            desired = exchange_max_leverage

        positions = client.get_positions(SYMBOL)
        pos = positions[0] if positions else {}
        amt = float(pos.get("positionAmt", 0) or 0)
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()

        if leverage == str(desired) and margin_type == "isolated":
            log.info(
                "ENFORCED | %s | leverage=%dx | margin=ISOLATED | verified, "
                "no change needed",
                SYMBOL,
                desired,
            )
            return True

        if amt != 0:
            log.warning(
                "config change deferred: position open | %s | account leverage=%s "
                "margin=%s | desired=%dx — OPENs blocked, CLOSE still allowed",
                SYMBOL,
                leverage,
                margin_type,
                desired,
            )
            return False

        # Flat: margin type first, then leverage.
        try:
            client.set_margin_type(SYMBOL, "ISOLATED")
        except BinanceAPIError as exc:
            log.error(
                "HALT | %s isolated margin could not be set while flat: %s",
                SYMBOL,
                exc,
            )
            raise FatalConfigError("isolated margin could not be set") from exc

        try:
            client.set_leverage(SYMBOL, desired)
        except BinanceAPIError as exc:
            if exc.code == -4028:
                log.error(
                    "leverage %dx rejected by exchange (-4028) | %s max=%s "
                    "— OPENs blocked",
                    desired,
                    SYMBOL,
                    exchange_max_leverage if exchange_max_leverage is not None else "unknown",
                )
                return False
            raise

        positions = client.get_positions(SYMBOL)
        pos = positions[0] if positions else {}
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()
        if margin_type != "isolated":
            log.error(
                "HALT | %s margin=%s (want isolated) after write", SYMBOL, margin_type
            )
            raise FatalConfigError("isolated margin could not be set")
        if leverage != str(desired):
            log.warning(
                "leverage verification failed | %s leverage=%s (want %dx) "
                "— OPENs blocked",
                SYMBOL,
                leverage,
                desired,
            )
            return False
        log.info(
            "ENFORCED | %s | leverage=%dx | margin=ISOLATED | applied", SYMBOL, desired
        )
        return True

    probed = False
    # Startup work (clock sync, symbol filters, first account log) is tracked
    # separately from the failure counter: a cycle that idles at effective OFF
    # completes successfully without doing any of it, and must not be mistaken
    # for a cycle that did.
    startup_done = False
    liquidation_warning_logged = False
    # Set the instant the smoke test may have sent its first order. From that
    # point a failed cycle must NOT be retried: a retry could place a second
    # synthetic order against a position the first one already opened.
    smoke_started = False

    def cycle(first_success: bool) -> int | None:
        """One unified fetch cycle. Startup-only work runs until the first
        success. Returns None normally; in smoke-test mode it returns the
        process exit code once the synthetic OPEN/CLOSE pair has run."""
        nonlocal probed, startup_done, liquidation_warning_logged, smoke_started
        nonlocal effective_mode, orders_enabled, block_reason_live
        nonlocal client, active_credentials, credentials_present

        # The config drives the mode decision, so it is refreshed before any
        # exchange call. A failed refresh raises and fails the cycle, which
        # leaves the previous (already-computed) state untouched and places
        # nothing — the same fail-closed retry every other fetch failure gets.
        consumer.ensure_config()

        previous_mode = effective_mode
        effective_mode = resolve_effective_mode(mode, consumer.db_execution_mode)
        effective_trade_capable = is_trade_capable(effective_mode)
        if effective_mode != previous_mode:
            log.warning(
                "EFFECTIVE MODE | %s -> %s | env_ceiling=%s | db_request=%s",
                previous_mode,
                effective_mode,
                mode,
                consumer.db_execution_mode,
            )

        # The connected user's live credentials, re-resolved every cycle. Doing
        # it here rather than once at startup is what lets a client connect
        # their keys, rotate them, or disconnect them on the website and have
        # this process follow within one cycle instead of at the next restart.
        #
        # Skipped entirely at effective OFF: that mode makes no exchange call,
        # so it needs no key and should not be asking the app for one.
        credentials_reason = None
        if credentials_client is not None and effective_mode != "OFF":
            # A transport failure raises CredentialsUnavailable and fails the
            # cycle, which is the same fail-closed retry every other fetch
            # failure gets. Only a definitive answer reaches the branches below.
            result = credentials_client.fetch()
            if result.present:
                credentials_present = True
                if result.credentials != active_credentials:
                    rotated = active_credentials is not None
                    active_credentials = result.credentials
                    client = build_client(
                        active_credentials.api_key, active_credentials.api_secret
                    )
                    # A new key pair is a different account as far as this
                    # process is concerned. The clock offset, symbol filters,
                    # bracket ladder and leverage enforcement were all
                    # established against the old one, so they are redone before
                    # anything is sized against the new one.
                    probed = False
                    startup_done = False
                    log.warning(
                        "LIVE CREDENTIALS %s | user=%s | key ...%s",
                        "ROTATED" if rotated else "BOUND",
                        engine_user_id,
                        active_credentials.last4,
                    )
            else:
                credentials_present = False
                credentials_reason = result.blocked_reason
                # The client is dropped, not merely bypassed. Once the user's
                # keys are gone this process must hold no means of signing on
                # their behalf — a live client sitting behind a boolean is one
                # refactor away from being used.
                active_credentials = None
                client = None
                probed = False
                startup_done = False

        # Why an OPEN may not be placed. Evaluated every cycle, from the values
        # just read, so switching auto-execute off or pressing Stop takes effect
        # on the next cycle rather than at the next restart.
        block_reason_live = placement_block_reason(
            effective_mode=effective_mode,
            db_execution_mode=consumer.db_execution_mode,
            auto_execute_enabled=consumer.db_auto_execute_enabled,
            is_running=consumer.db_is_running,
            live_order_cap_usd=consumer.live_order_cap_usd,
            ack_present=ack_present,
        )
        orders_enabled = block_reason_live is None

        if credentials_reason is not None:
            # Outranks every other reason. Without the user's keys there is no
            # authenticated path to the exchange at all, so this is both the
            # most fundamental gate and the most actionable thing to report:
            # every other reason is fixed by an operator, this one by the
            # client pressing Connect.
            block_reason_live = credentials_reason
            orders_enabled = False

        # Attach the placement client only when the effective mode is
        # trade-capable. Detaching removes the placement path itself rather than
        # setting a flag beside it: the consumer reaches _place_order only via
        # `self._binance_trader is not None`.
        consumer.set_trader(
            client if (effective_trade_capable and client is not None) else None
        )

        # A database request the environment refuses is the single most
        # important thing to see in a log, so it is stated plainly every cycle
        # it holds rather than only on transition.
        if consumer.db_execution_mode != effective_mode and consumer.db_execution_mode != "OFF":
            log.warning(
                "DB REQUEST DEGRADED | database asks for %s, host .env permits at "
                "most %s — running %s",
                consumer.db_execution_mode,
                mode,
                effective_mode,
            )

        if effective_mode == "OFF":
            # No exchange call of any kind, exactly like run_off — but the
            # process stays alive so a later database change can bring it back
            # without a restart.
            log.info(
                "executor idle | effective=OFF | env_ceiling=%s | db_request=%s",
                mode,
                consumer.db_execution_mode,
            )
            report(
                message=(
                    f"idle: effective OFF (env {mode}, database "
                    f"{consumer.db_execution_mode})"
                ),
            )
            # poll_once is what normally reopens the cycle. Returning here
            # without it would strand the flag and stop every later refresh —
            # the executor would stay OFF even after the database changed back.
            consumer.end_cycle()
            return None

        if credentials_reason is not None:
            # Fail closed on mainnet. No client was built, so no signed call is
            # made, no balance is read and no order can be placed — the refusal
            # is structural, not a flag checked further down.
            #
            # The process stays alive and keeps heartbeating rather than
            # exiting, because a dead executor and one waiting on a key look
            # identical from outside, and only one of them is fixed by the
            # client connecting their account. The website shows the reason.
            log.error(
                "LIVE ACCESS BLOCKED | %s | effective=%s | no mainnet call will "
                "be made and no order can be placed until the connected user's "
                "Binance keys are available",
                credentials_reason,
                effective_mode,
            )
            report(message=f"blocked: {credentials_reason}")
            consumer.end_cycle()
            return None

        if not startup_done:
            offset = client.sync_clock()
            log.info("clock synced | offset=%dms", offset)

            symbol_info = client.get_exchange_info(SYMBOL)
            tick_size, step_size, min_notional = _extract_filters(symbol_info)
            log.info(
                "%s filters | tick_size=%s | step_size=%s | min_notional=%s | "
                "price_precision=%s | quantity_precision=%s",
                SYMBOL,
                tick_size,
                step_size,
                min_notional,
                symbol_info.get("pricePrecision"),
                symbol_info.get("quantityPrecision"),
            )

        # Trade-capable only: learn the exchange's authoritative leverage
        # ceiling and notional cap before any enforcement or sizing happens.
        # Gated on the EFFECTIVE mode: a host whose .env permits trading but
        # whose database asks for read-only must not probe or enforce.
        if effective_trade_capable and not probed:
            probe_leverage_brackets()
            probed = True

        positions = client.get_positions(SYMBOL)
        account = client.get_account()
        consumer.set_available_balance(Decimal(str(account["availableBalance"])))

        if not startup_done:
            startup_done = True
            log.info(
                "account | total_wallet_balance=%s | available_balance=%s",
                account.get("totalWalletBalance"),
                account.get("availableBalance"),
            )
            for pos in positions:
                log.info(
                    "%s position | amt=%s | entry_price=%s | leverage=%s | margin_type=%s",
                    SYMBOL,
                    pos.get("positionAmt"),
                    pos.get("entryPrice"),
                    pos.get("leverage"),
                    pos.get("marginType"),
                )

        pos_amt = positions[0].get("positionAmt") if positions else "0"
        log.info(
            "executor alive | mode=%s | %s pos=%s | bal=%s | clock_offset=%dms",
            mode,
            SYMBOL,
            pos_amt,
            account.get("availableBalance"),
            client.clock_offset_ms,
        )

        # Consumer poll: log order intents for any pending signals.
        try:
            position_amt = float(pos_amt or 0)
        except (TypeError, ValueError):
            position_amt = 0.0

        # Config was refreshed at the top of the cycle; ensure_config() is
        # idempotent within a cycle, so the desired leverage below is the value
        # the mode decision was made from.
        leverage_blocked = False
        desired = consumer.desired_leverage
        # set_leverage / set_margin_type are writes. Gating them on the
        # EFFECTIVE mode is what makes a degraded LIVE_READ genuinely read-only
        # even on a host whose .env would permit trading.
        if effective_trade_capable and desired is not None:
            if not liquidation_warning_logged:
                liquidation_warning_logged = True
                if desired > 0 and 1 / desired < SL_PCT_ASSUMED:
                    log.warning(
                        "WARNING | leverage %dx puts liquidation at ~%.2f%% which is "
                        "inside the assumed %.2f%% stop — the exchange will liquidate "
                        "before the strategy stop can fire",
                        desired,
                        100.0 / desired,
                        SL_PCT_ASSUMED * 100.0,
                    )
            if not enforce_account_config(desired):
                leverage_blocked = True

        # Reconcile at cycle start, before signal processing. In a trade-capable
        # mode a fetch failure raises SignalConsumerError here (failed cycle) so
        # no OPEN is placed this cycle; in a read mode it is log-only.
        opens_blocked, block_reason = consumer.reconcile(position_amt)
        if leverage_blocked:
            # OPENs are blocked, CLOSEs stay allowed — the same asymmetric
            # kill-switch behaviour reconcile() already relies on.
            opens_blocked = True
            if block_reason is None:
                block_reason = "leverage_config_mismatch"
        if block_reason_live is not None:
            # The live-control gates (mode, auto-execute, kill switch, cap, ack)
            # block OPENs only. A position that is already open must stay
            # closable when auto-execute is switched off or Stop is pressed —
            # stranding real exposure behind a control flag would be worse than
            # the exposure itself. CLOSE remains reachable while the effective
            # mode is trade-capable; below that there is no trader at all.
            opens_blocked = True
            if block_reason is None:
                block_reason = block_reason_live

        # Report once per cycle, here: reconcile has just run, and every branch
        # below (smoke, blocked, normal) is reached through this point, so a
        # heartbeat is sent whatever happens next. The account/position snapshot
        # is the cycle-start read — a fill placed later this cycle shows up on
        # the next one. get_account() above is a signed call, so reaching this
        # line is itself the proof that the credential works.
        report(
            account=account,
            positions=positions,
            permission="verified_futures",
            message=(
                f"opens blocked: {block_reason}" if opens_blocked else "cycle ok"
            ),
        )

        if smoke_test:
            # The smoke test replaces signal processing for this run: no pending
            # ML signal is fetched, sized, or placed. It runs only after the
            # full startup path above (clock, filters, bracket probe, account
            # enforcement, reconcile) has succeeded, so it is gated by exactly
            # the same preconditions a natural OPEN would face.
            if not effective_trade_capable:
                log.error(
                    "SMOKE ABORTED | effective mode is %s (env %s, database %s) "
                    "— the smoke test never runs outside a trade-capable "
                    "effective mode",
                    effective_mode,
                    mode,
                    consumer.db_execution_mode,
                )
                return 1
            if opens_blocked:
                log.error(
                    "SMOKE ABORTED | OPENs are blocked (%s) — refusing to place "
                    "a synthetic order while the executor would reject a real one",
                    block_reason,
                )
                return 1
            smoke_started = True
            return consumer.run_smoke_test(smoke_side, position_amt)

        consumer.poll_once(
            position_amt=position_amt,
            opens_blocked=opens_blocked,
            block_reason=block_reason,
        )
        return None

    # Unified cycle loop: startup and recurring fetches share one failure counter.
    first_success = False
    consecutive_failures = 0
    while True:
        try:
            exit_code = cycle(first_success)
            if exit_code is not None:
                return exit_code
            first_success = True
            consecutive_failures = 0
        except FatalConfigError as exc:
            report(message=f"halted: {exc}", permission=permission_status_for_error(exc))
            return 1
        except RateLimitError as exc:
            if smoke_started:
                log.error(
                    "SMOKE RATE LIMITED MID-FLIGHT | %s | NOT retrying — verify "
                    "the position on Binance and close it manually if it is open",
                    exc,
                )
                return 1
            backoff = max(exc.retry_after, 60)
            log.error("RATE LIMITED | backing off %ds", backoff)
            report(message=f"rate limited, backing off {backoff}s")
            time.sleep(backoff)
            continue
        except (
            BinanceAPIError,
            SignalConsumerError,
            EnforcementError,
            CredentialsUnavailable,
            OSError,
        ) as exc:
            if smoke_started:
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
            # stale by definition here, and the dashboard must never present a
            # stale reading as a current one.
            report(
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
