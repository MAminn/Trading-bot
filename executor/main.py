"""Executor service entrypoint.

Binance USD-M Futures execution service.
Implemented modes: OFF, TESTNET_READ (read-only), TESTNET_TRADE (account
enforcement + risk guard + order placement). LIVE_* modes refuse to start.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

ALLOWED_MODES = ("OFF", "TESTNET_READ", "TESTNET_TRADE", "LIVE_READ", "LIVE_DRYRUN", "LIVE")
IMPLEMENTED_MODES = ("OFF", "TESTNET_READ", "TESTNET_TRADE")

HEARTBEAT_INTERVAL_SECONDS = 60
MAX_CONSECUTIVE_FAILURES = 10

# The mode determines the base URL — never read from env, so a config mistake
# can never point testnet mode at live.
TESTNET_BASE_URL = "https://testnet.binancefuture.com"
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


def run_testnet(mode: str) -> int:
    """Runs TESTNET_READ (pure read) and TESTNET_TRADE (read + account
    enforcement + risk guard + order placement)."""
    from binance_client import BinanceAPIError, BinanceFuturesClient, RateLimitError
    from signal_consumer import SignalConsumer, SignalConsumerError

    api_key = os.environ.get("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        log.error(
            "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET must be set "
            "for %s mode",
            mode,
        )
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

    log.info("=" * 60)
    log.info("executor starting | mode=%s | symbol=%s", mode, SYMBOL)
    log.info("base_url=%s (hardcoded for this mode)", TESTNET_BASE_URL)
    if mode == "TESTNET_TRADE":
        log.info("account enforcement + risk guard + order placement active")
    else:
        log.info("read-only mode: no write calls to Binance")
    log.info("=" * 60)

    client = BinanceFuturesClient(TESTNET_BASE_URL, api_key, api_secret)

    risk_guard = None
    if mode == "TESTNET_TRADE":
        from risk_guard import RiskGuard

        risk_guard = RiskGuard()

    consumer = SignalConsumer(
        app_api_base,
        engine_service_token,
        engine_user_id,
        mode,
        SYMBOL,
        start_after=start_after,
        risk_guard=risk_guard,
        # Placement is enabled only in TESTNET_TRADE; read modes get no trader.
        binance_trader=client if mode == "TESTNET_TRADE" else None,
    )

    def enforce_account_config() -> None:
        """One-time TESTNET_TRADE prerequisite: isolated margin, leverage 1.

        Verify-first: if the account is already correct, make zero write calls
        (works even with an open position). Only reconfigure when the account is
        wrong and no position is open; refuse to trade otherwise.
        """
        positions = client.get_positions(SYMBOL)
        pos = positions[0] if positions else {}
        amt = float(pos.get("positionAmt", 0) or 0)
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()

        if leverage == "1" and margin_type == "isolated":
            log.info(
                "ENFORCED | %s | leverage=1 | margin=ISOLATED | verified, no change needed",
                SYMBOL,
            )
            return

        if amt != 0:
            log.error(
                "HALT | %s position open with wrong account config "
                "(leverage=%s, margin=%s) — refusing to trade",
                SYMBOL,
                leverage,
                margin_type,
            )
            raise FatalConfigError("position open with wrong account config")

        client.set_margin_type(SYMBOL, "ISOLATED")
        client.set_leverage(SYMBOL, 1)

        positions = client.get_positions(SYMBOL)
        pos = positions[0] if positions else {}
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()
        if leverage != "1" or margin_type != "isolated":
            log.error(
                "account config verification failed | leverage=%s (want 1) | "
                "margin=%s (want isolated)",
                leverage,
                margin_type,
            )
            raise FatalConfigError("account configuration verification failed")
        log.info("ENFORCED | %s | leverage=1 | margin=ISOLATED | applied", SYMBOL)

    enforced = False

    def cycle(first_success: bool) -> None:
        """One unified fetch cycle. Startup-only work runs until the first success."""
        nonlocal enforced
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

        # TESTNET_TRADE only: one-time account enforcement, before anything
        # else in the cycle. Must succeed before the mode does any other work.
        if mode == "TESTNET_TRADE" and not enforced:
            enforce_account_config()
            enforced = True

        positions = client.get_positions(SYMBOL)
        account = client.get_account()

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
        # Reconcile at cycle start, before signal processing. In TESTNET_TRADE a
        # fetch failure raises SignalConsumerError here (failed cycle) so no OPEN
        # is placed this cycle; in TESTNET_READ it is log-only.
        opens_blocked, block_reason = consumer.reconcile(position_amt)
        consumer.poll_once(
            position_amt=position_amt,
            opens_blocked=opens_blocked,
            block_reason=block_reason,
        )

    # Unified cycle loop: startup and recurring fetches share one failure counter.
    first_success = False
    consecutive_failures = 0
    while True:
        try:
            cycle(first_success)
            first_success = True
            consecutive_failures = 0
        except FatalConfigError:
            return 1
        except RateLimitError as exc:
            backoff = max(exc.retry_after, 60)
            log.error("RATE LIMITED | backing off %ds", backoff)
            time.sleep(backoff)
            continue
        except (BinanceAPIError, SignalConsumerError, EnforcementError, OSError) as exc:
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
    return run_testnet(mode)


if __name__ == "__main__":
    sys.exit(main())
