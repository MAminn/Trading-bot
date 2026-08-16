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

# Live and testnet credentials live in separate variables so a stale testnet
# key can never authenticate against mainnet, nor a live key against testnet.
MODE_CREDENTIAL_ENV = {
    "TESTNET_READ": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
    "TESTNET_TRADE": ("BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"),
    "LIVE_READ": ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"),
    "LIVE_TRADE": ("BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET"),
}

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


def run_off() -> int:
    log.info("=" * 60)
    log.info("executor starting | mode=OFF")
    log.info("no Binance connectivity, no trading logic")
    log.info("=" * 60)

    while True:
        now = datetime.now(timezone.utc).isoformat()
        log.info("executor alive | mode=OFF | %s", now)
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
    from signal_consumer import SignalConsumer, SignalConsumerError

    trade_capable = mode in TRADE_CAPABLE_MODES
    base_url = MODE_BASE_URLS[mode]

    refusal, live_order_cap_usd = live_preflight(mode)
    if refusal is not None:
        return refusal

    key_env, secret_env = MODE_CREDENTIAL_ENV[mode]
    api_key = os.environ.get(key_env, "").strip()
    api_secret = os.environ.get(secret_env, "").strip()
    if not api_key or not api_secret:
        log.error("%s and %s must be set for %s mode", key_env, secret_env, mode)
        return 1

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
    client = (
        BinanceFuturesClient(base_url, api_key, api_secret)
        if trade_capable
        else ReadOnlyFuturesClient(base_url, api_key, api_secret)
    )

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
        # Placement is enabled only in trade-capable modes; read modes get no
        # trader, and the object they do hold has no write methods anyway.
        binance_trader=client if trade_capable else None,
        live_order_cap_usd=live_order_cap_usd,
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
    liquidation_warning_logged = False
    # Set the instant the smoke test may have sent its first order. From that
    # point a failed cycle must NOT be retried: a retry could place a second
    # synthetic order against a position the first one already opened.
    smoke_started = False

    def cycle(first_success: bool) -> int | None:
        """One unified fetch cycle. Startup-only work runs until the first
        success. Returns None normally; in smoke-test mode it returns the
        process exit code once the synthetic OPEN/CLOSE pair has run."""
        nonlocal probed, liquidation_warning_logged, smoke_started
        if not first_success:
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
        if trade_capable and not probed:
            probe_leverage_brackets()
            probed = True

        positions = client.get_positions(SYMBOL)
        account = client.get_account()
        consumer.set_available_balance(Decimal(str(account["availableBalance"])))

        if not first_success:
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

        # Refresh the engine config first: the desired leverage drives both
        # account enforcement and order sizing this cycle.
        consumer.ensure_config()
        leverage_blocked = False
        desired = consumer.desired_leverage
        if trade_capable and desired is not None:
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

        if smoke_test:
            # The smoke test replaces signal processing for this run: no pending
            # ML signal is fetched, sized, or placed. It runs only after the
            # full startup path above (clock, filters, bracket probe, account
            # enforcement, reconcile) has succeeded, so it is gated by exactly
            # the same preconditions a natural OPEN would face.
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
        except FatalConfigError:
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
            time.sleep(backoff)
            continue
        except (BinanceAPIError, SignalConsumerError, EnforcementError, OSError) as exc:
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
