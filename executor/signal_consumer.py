"""Signal consumer: polls the app for pending signals and logs order *intents*.

Binance access is limited to one unsigned, read-only mark-price fetch
(/fapi/v1/premiumIndex) used for sizing. Everything else goes to the app's API
(signals/pending, config, ingest/order). Orders are computed and persisted as
intents; nothing is placed, cancelled, or modified on any exchange.

The config endpoint response contains decrypted Binance credentials, so the
raw response is never logged; only max_position_size_usd is extracted.
"""

import logging
from decimal import ROUND_DOWN, Decimal

import requests

from binance_client import BinanceAPIError, BinanceFuturesClient

log = logging.getLogger("executor.consumer")

REQUEST_TIMEOUT_SECONDS = 10
CONFIG_REFRESH_EVERY_CYCLES = 10

STEP_SIZE = Decimal("0.001")
MIN_NOTIONAL_USD = Decimal("20")
NOTIONAL_CAP_USD = Decimal("100")

# The execution mode determines the Binance base URL used for the unsigned
# mark-price read — never configurable via env.
MODE_BINANCE_BASE_URLS = {
    "TESTNET_READ": "https://testnet.binancefuture.com",
}


class SignalConsumerError(Exception):
    """Raised on any app-API failure so the caller's retry rules apply."""


class SignalConsumer:
    def __init__(
        self,
        app_api_base: str,
        engine_service_token: str,
        user_id: str,
        execution_mode: str,
        symbol: str = "ETHUSDT",
        start_after: str | None = None,
    ):
        self._base = app_api_base.rstrip("/")
        self._user_id = user_id
        self._execution_mode = execution_mode
        self._symbol = symbol
        # created_at of last processed signal; optionally seeded via CONSUMER_START_AFTER
        self._cursor: str | None = start_after
        self._cycles = 0
        self._max_position_size_usd: Decimal | None = None
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {engine_service_token}"})
        # Credential-less client used exclusively for the unsigned mark-price read.
        self._binance = BinanceFuturesClient(
            MODE_BINANCE_BASE_URLS[execution_mode], "", ""
        )

    # ------------------------------------------------------------------ #
    # app API helpers
    # ------------------------------------------------------------------ #

    def _get(self, path: str, params: dict):
        try:
            resp = self._session.get(
                f"{self._base}{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
        except OSError as exc:
            raise SignalConsumerError(f"GET {path} failed: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise SignalConsumerError(f"GET {path} -> HTTP {resp.status_code}")
        return resp.json()

    def _refresh_config(self) -> None:
        body = self._get("/api/public/engine/config", {"user_id": self._user_id})
        # Response includes decrypted secrets — never log it. Extract sizing only.
        raw = (body.get("config") or {}).get("max_position_size_usd")
        if raw is None:
            raise SignalConsumerError("config missing max_position_size_usd")
        self._max_position_size_usd = Decimal(str(raw))
        log.info(
            "config refreshed | max_position_size_usd=%s", self._max_position_size_usd
        )

    def _post_order(self, order: dict) -> None:
        try:
            resp = self._session.post(
                f"{self._base}/api/public/engine/ingest/order",
                json=order,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            raise SignalConsumerError(f"POST ingest/order failed: {exc}") from exc
        if resp.status_code == 409:
            log.info("duplicate intent, skipping | key=%s", order["idempotency_key"])
            return
        if not 200 <= resp.status_code < 300:
            raise SignalConsumerError(f"POST ingest/order -> HTTP {resp.status_code}")

    # ------------------------------------------------------------------ #
    # intent computation
    # ------------------------------------------------------------------ #

    def _build_intent(self, signal: dict, ref_price: Decimal) -> dict:
        side = "LONG" if signal.get("rule_side") == 1 else "SHORT"
        bar_time = signal["bar_time"]
        idempotency_key = f"{self._user_id}:{bar_time}:{side}:OPEN"

        notional_target = min(self._max_position_size_usd, NOTIONAL_CAP_USD)
        qty = (notional_target / ref_price).quantize(STEP_SIZE, rounding=ROUND_DOWN)
        notional = qty * ref_price

        order = {
            "user_id": self._user_id,
            "signal_bar_time": bar_time,
            "symbol": self._symbol,
            "side": side,
            "intent": "OPEN",
            "qty": float(qty),
            "ref_price": float(ref_price),
            "notional_usd": float(notional),
            "execution_mode": self._execution_mode,
            "status": "INTENT_LOGGED",
            "idempotency_key": idempotency_key,
        }
        if notional < MIN_NOTIONAL_USD:
            order["status"] = "SKIPPED"
            order["error"] = "below min notional"
        return order

    # ------------------------------------------------------------------ #
    # public entrypoint
    # ------------------------------------------------------------------ #

    def poll_once(self, mark_price=None) -> None:
        """One poll cycle: refresh config as scheduled, fetch pending signals,
        log each as an order intent. Raises SignalConsumerError on failure.

        The mark_price argument is deprecated and ignored — the price is
        fetched from /fapi/v1/premiumIndex once per cycle."""
        if self._max_position_size_usd is None or self._cycles % CONFIG_REFRESH_EVERY_CYCLES == 0:
            self._refresh_config()
        self._cycles += 1

        params = {"user_id": self._user_id}
        if self._cursor:
            params["after"] = self._cursor
        signals = self._get("/api/public/engine/signals/pending", params)
        if not signals:
            return

        # One mark-price fetch per cycle, before sizing anything. If it is
        # unavailable, fail the whole cycle: nothing is posted, no signal is
        # SKIPPED, and the cursor does not move — the unified retry logic in
        # main.py will re-run the cycle.
        try:
            ref_price = Decimal(str(self._binance.get_mark_price(self._symbol)))
        except (BinanceAPIError, OSError) as exc:
            log.error("mark price unavailable, retrying cycle")
            raise SignalConsumerError(f"mark price fetch failed: {exc}") from exc

        for signal in signals:
            order = self._build_intent(signal, ref_price)
            log.info(
                "INTENT | %s OPEN | qty=%s | ref_price=%s | notional=%s | key=%s",
                order["side"],
                order["qty"],
                order["ref_price"],
                order["notional_usd"],
                order["idempotency_key"],
            )
            self._post_order(order)
            # Advance cursor only after the intent is persisted (or confirmed duplicate)
            # so a mid-batch failure resumes from the right place next cycle.
            self._cursor = signal["created_at"]
