"""Signal consumer: polls the app for pending signals and records order intents.

Binance access is limited to one unsigned, read-only mark-price fetch
(/fapi/v1/premiumIndex) used for sizing plus the TESTNET_TRADE placement call.
Everything else goes to the app's API (signals/pending, config, ingest/order,
ingest/order_update). TESTNET_READ stays pure-read; TESTNET_TRADE records
intents, places allowed MARKET orders, and persists the outcome.

The config endpoint response contains decrypted Binance credentials, so the
raw response is never logged; only max_position_size_usd is extracted.
"""

import hashlib
import logging
import time
from datetime import datetime, timezone
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
    "TESTNET_TRADE": "https://testnet.binancefuture.com",
}


class SignalConsumerError(Exception):
    """Raised on any app-API failure so the caller's retry rules apply."""


def to_z_iso(ts) -> str:
    """Return a canonical UTC ISO 8601 string with a Z suffix.

    Accepts a datetime or an ISO string (with Z or +00:00-style offset).
    The app's Zod validator (z.string().datetime()) rejects +00:00 offsets.
    """
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


class SignalConsumer:
    def __init__(
        self,
        app_api_base: str,
        engine_service_token: str,
        user_id: str,
        execution_mode: str,
        symbol: str = "ETHUSDT",
        start_after: str | None = None,
        risk_guard=None,
        binance_trader=None,
    ):
        self._base = app_api_base.rstrip("/")
        self._user_id = user_id
        self._execution_mode = execution_mode
        self._symbol = symbol
        # Optional RiskGuard (trade-capable modes only); None keeps read modes pure.
        self._risk_guard = risk_guard
        # Authenticated client for order placement; supplied ONLY in TESTNET_TRADE.
        # None in TESTNET_READ/OFF keeps those paths free of any write calls.
        self._binance_trader = binance_trader
        self._last_order_time: float | None = None
        # created_at of last processed signal; optionally seeded via CONSUMER_START_AFTER.
        # Always stored in canonical Z-form — the pending route rejects +00:00 offsets.
        self._cursor: str | None = to_z_iso(start_after) if start_after else None
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
        if resp.status_code == 400:
            log.error("GET %s validation error (HTTP 400): %s", path, resp.text[:2000])
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

    def _post_order(self, order: dict) -> bool:
        """POST the intent. Returns True if newly created (201), False if the
        app reports it as a duplicate (409)."""
        payload = {k: v for k, v in order.items() if v is not None}
        try:
            resp = self._session.post(
                f"{self._base}/api/public/engine/ingest/order",
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            raise SignalConsumerError(f"POST ingest/order failed: {exc}") from exc
        if resp.status_code == 409:
            log.info("duplicate intent, skipping | key=%s", order["idempotency_key"])
            return False
        if resp.status_code == 400:
            log.error(
                "ingest/order validation error (HTTP 400): %s", resp.text[:2000]
            )
        if not 200 <= resp.status_code < 300:
            raise SignalConsumerError(f"POST ingest/order -> HTTP {resp.status_code}")
        return True

    def _post_order_update(self, update: dict) -> None:
        """POST to ingest/order_update. Failures are logged and swallowed — the
        Binance order state is authoritative; reconciliation is a later step."""
        payload = {k: v for k, v in update.items() if v is not None}
        try:
            resp = self._session.post(
                f"{self._base}/api/public/engine/ingest/order_update",
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            log.error("order_update POST failed: %s", exc)
            return
        if resp.status_code == 400:
            log.error(
                "order_update validation error (HTTP 400): %s", resp.text[:2000]
            )
        if not 200 <= resp.status_code < 300:
            log.error("order_update POST -> HTTP %d", resp.status_code)

    # ------------------------------------------------------------------ #
    # intent computation
    # ------------------------------------------------------------------ #

    def _build_intent(self, signal: dict, ref_price: Decimal) -> dict:
        side = "LONG" if signal.get("rule_side") == 1 else "SHORT"
        bar_time = to_z_iso(signal["bar_time"])
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
    # order placement (TESTNET_TRADE only)
    # ------------------------------------------------------------------ #

    def _place_order(self, order: dict) -> None:
        """Place a MARKET order for an allowed, newly-created intent and record
        the result via ingest/order_update. TESTNET_TRADE only. Never retries a
        given key (the client_order_id makes retries rejectable anyway)."""
        idempotency_key = order["idempotency_key"]
        # 35 chars, alphanumeric: "x" + 32 hex chars of sha256(idempotency_key).
        client_order_id = "x" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        mapped_side = "BUY" if order["side"] == "LONG" else "SELL"

        try:
            result = self._binance_trader.place_market_order(
                self._symbol, mapped_side, order["qty"], client_order_id
            )
        except (BinanceAPIError, OSError) as exc:
            log.error("order placement failed | key=%s | %s", idempotency_key, exc)
            self._post_order_update(
                {
                    "user_id": self._user_id,
                    "idempotency_key": idempotency_key,
                    "status": "FAILED",
                    "error": str(exc)[:1000],
                }
            )
            return

        binance_id = result.get("orderId")
        binance_status = result.get("status")
        log.info(
            "ORDER SENT | %s | qty=%s | binance_id=%s | status=%s",
            order["side"],
            order["qty"],
            binance_id,
            binance_status,
        )
        self._post_order_update(
            {
                "user_id": self._user_id,
                "idempotency_key": idempotency_key,
                "status": "FILLED" if binance_status == "FILLED" else "SENT",
                "binance_order_id": str(binance_id) if binance_id is not None else None,
            }
        )

    # ------------------------------------------------------------------ #
    # public entrypoint
    # ------------------------------------------------------------------ #

    def poll_once(self, mark_price=None, position_amt=None) -> None:
        """One poll cycle: refresh config as scheduled, fetch pending signals,
        log each as an order intent. Raises SignalConsumerError on failure.

        position_amt: current position amount for the symbol, used by the
        risk guard in trade-capable modes. The mark_price argument is
        deprecated and ignored — the price is fetched from
        /fapi/v1/premiumIndex once per cycle."""
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
            if self._risk_guard is not None and order["status"] == "INTENT_LOGGED":
                allowed, reason = self._risk_guard.evaluate(
                    order, position_amt, self._last_order_time
                )
                if allowed:
                    log.info("RISK | ALLOWED | %s", order["idempotency_key"])
                else:
                    log.info(
                        "RISK | REJECTED | %s | %s", reason, order["idempotency_key"]
                    )
                    order["status"] = "SKIPPED"
                    order["error"] = reason
            log.info(
                "INTENT | %s OPEN | qty=%s | ref_price=%s | notional=%s | key=%s",
                order["side"],
                order["qty"],
                order["ref_price"],
                order["notional_usd"],
                order["idempotency_key"],
            )
            created = self._post_order(order)
            if created and order["status"] == "INTENT_LOGGED":
                if self._risk_guard is not None:
                    # Feed the guard's min-interval check from allowed intents only.
                    self._last_order_time = time.time()
                # Placement is enabled only in TESTNET_TRADE (trader supplied there).
                if self._binance_trader is not None:
                    self._place_order(order)
            # Advance cursor only after the intent is persisted (or confirmed duplicate)
            # so a mid-batch failure resumes from the right place next cycle.
            # Normalized to Z-form: the pending route rejects +00:00 offsets.
            self._cursor = to_z_iso(signal["created_at"])
