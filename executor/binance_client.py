"""Binance USD-M Futures REST client.

Self-contained: uses only `requests`. Reads plus exactly two account-
configuration writes (leverage, margin type) and a single MARKET order
placement. No cancel, batch, or other order endpoints exist in this client.

The api_secret is used exclusively for HMAC signing and is never logged or
included in exception messages.
"""

import hashlib
import hmac
import logging
import time
import urllib.parse

import requests

log = logging.getLogger("executor.binance")

RECV_WINDOW_MS = 5000
REQUEST_TIMEOUT_SECONDS = 10
CLOCK_DESYNC_ERROR_CODE = -1021


class BinanceAPIError(Exception):
    """Raised on any non-2xx response from Binance."""

    def __init__(self, http_status: int, code: int | None, message: str):
        self.http_status = http_status
        self.code = code
        self.message = message
        super().__init__(
            f"Binance API error (HTTP {http_status}, code={code}): {message}"
        )


class BinanceFuturesClient:
    """USD-M Futures client: reads + config writes + one MARKET order path."""

    def __init__(self, base_url: str, api_key: str, api_secret: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret.encode("utf-8")
        self._clock_offset_ms = 0
        self._session = requests.Session()
        if self._api_key:
            self._session.headers.update({"X-MBX-APIKEY": self._api_key})

    @property
    def clock_offset_ms(self) -> int:
        return self._clock_offset_ms

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self._api_secret, query_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _parse_error(response: requests.Response) -> "BinanceAPIError":
        code = None
        message = response.text[:500]
        try:
            body = response.json()
            code = body.get("code")
            message = body.get("msg", message)
        except ValueError:
            pass
        return BinanceAPIError(response.status_code, code, message)

    def _get(self, path: str, params: dict | None = None, signed: bool = False):
        return self._request("GET", path, params, signed)

    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False):
        """Perform a request. Retries once after a clock re-sync on -1021."""
        try:
            return self._do_request(method, path, params, signed)
        except BinanceAPIError as exc:
            if signed and exc.code == CLOCK_DESYNC_ERROR_CODE:
                log.warning("clock desync (-1021) detected, re-syncing and retrying")
                self.sync_clock()
                return self._do_request(method, path, params, signed)
            raise

    def _do_request(self, method: str, path: str, params: dict | None, signed: bool):
        params = dict(params or {})
        if signed:
            params["recvWindow"] = RECV_WINDOW_MS
            params["timestamp"] = self._timestamp_ms()
            query_string = urllib.parse.urlencode(params)
            params["signature"] = self._sign(query_string)

        response = self._session.request(
            method,
            f"{self._base_url}{path}",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not 200 <= response.status_code < 300:
            raise self._parse_error(response)
        return response.json()

    # ------------------------------------------------------------------ #
    # public read methods
    # ------------------------------------------------------------------ #

    def sync_clock(self) -> int:
        """Sync local clock offset against server time. Returns offset in ms."""
        local_before = int(time.time() * 1000)
        server_time = self._do_request("GET", "/fapi/v1/time", None, signed=False)["serverTime"]
        local_after = int(time.time() * 1000)
        local_mid = (local_before + local_after) // 2
        self._clock_offset_ms = server_time - local_mid
        return self._clock_offset_ms

    def get_server_time(self) -> dict:
        """GET /fapi/v1/time (unsigned)."""
        return self._get("/fapi/v1/time")

    def get_exchange_info(self, symbol: str) -> dict:
        """GET /fapi/v1/exchangeInfo (unsigned), returning only the given symbol's entry."""
        info = self._get("/fapi/v1/exchangeInfo")
        for entry in info.get("symbols", []):
            if entry.get("symbol") == symbol:
                return entry
        raise BinanceAPIError(200, None, f"symbol {symbol} not found in exchangeInfo")

    def get_mark_price(self, symbol: str) -> float:
        """GET /fapi/v1/premiumIndex (unsigned) — returns the symbol's markPrice as float."""
        data = self._get("/fapi/v1/premiumIndex", {"symbol": symbol})
        raw = data.get("markPrice")
        try:
            price = float(raw)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            raise BinanceAPIError(
                200, None, f"missing or non-positive markPrice {raw!r} for {symbol}"
            )
        return price

    def get_account(self) -> dict:
        """GET /fapi/v2/account (signed)."""
        return self._get("/fapi/v2/account", signed=True)

    def get_positions(self, symbol: str) -> list[dict]:
        """GET /fapi/v2/positionRisk (signed), filtered to the given symbol."""
        positions = self._get("/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        return [p for p in positions if p.get("symbol") == symbol]

    # ------------------------------------------------------------------ #
    # account-configuration writes plus a single MARKET order placement.
    # No cancel, batch, or other order endpoints exist in this client.
    # ------------------------------------------------------------------ #

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        """POST /fapi/v1/leverage (signed)."""
        return self._request(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True
        )

    def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """POST /fapi/v1/marginType (signed). Error -4046 (no change needed)
        is treated as success."""
        try:
            return self._request(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
                signed=True,
            )
        except BinanceAPIError as exc:
            if exc.code == -4046:
                return {"code": -4046, "msg": "No need to change margin type"}
            raise

    def place_market_order(
        self, symbol: str, side: str, qty, client_order_id: str
    ) -> dict:
        """Place a signed MARKET order. side is BUY or SELL."""
        return self._request(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quantity": qty,
                "newClientOrderId": client_order_id,
            },
            signed=True,
        )
