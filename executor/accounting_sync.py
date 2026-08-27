"""Read-only Binance accounting synchronizer.

This module is deliberately NOT imported by the executor, signal consumer,
risk guard, reconciler, or engine. Running it cannot place, cancel, resize,
close, or otherwise influence a trade. It only:

1. reads this user's completed engine-order metadata from the app,
2. reads this user's Binance USD-M fills and funding income,
3. computes gross realized P&L, exact commissions, funding and net P&L,
4. posts those reporting facts back to the app's accounting endpoint.

Required env:
  APP_API_BASE
  ENGINE_SERVICE_TOKEN
  ENGINE_CREDENTIALS_TOKEN
  ACCOUNTING_USER_IDS       comma-separated UUIDs

Optional env:
  ACCOUNTING_SYMBOL         default ETHUSDT
  ACCOUNTING_LOOKBACK_DAYS  default 30
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests

from user_credentials import UserCredentialsClient


log = logging.getLogger("executor.accounting")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

BINANCE_BASE = "https://fapi.binance.com"
REQUEST_TIMEOUT = 15
MAX_ROWS_PER_WINDOW = 1000
WINDOW_DAYS = 7


class AccountingError(RuntimeError):
    pass


def _z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class BinanceAccountingClient:
    """Minimal signed GET-only Binance USD-M client."""

    def __init__(self, api_key: str, api_secret: str):
        self._secret = api_secret.encode("utf-8")
        self._offset_ms = 0
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": api_key})

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None, *, signed: bool = False):
        params = dict(params or {})
        if signed:
            params["recvWindow"] = 5000
            params["timestamp"] = self._timestamp_ms()
            qs = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self._secret, qs.encode("utf-8"), hashlib.sha256).hexdigest()
        r = self._session.get(f"{BINANCE_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
        if not 200 <= r.status_code < 300:
            raise AccountingError(f"Binance GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def sync_clock(self) -> None:
        before = int(time.time() * 1000)
        server = int(self._get("/fapi/v1/time")["serverTime"])
        after = int(time.time() * 1000)
        self._offset_ms = server - ((before + after) // 2)

    def user_trades(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        rows = self._get(
            "/fapi/v1/userTrades",
            {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": MAX_ROWS_PER_WINDOW},
            signed=True,
        )
        if len(rows) >= MAX_ROWS_PER_WINDOW:
            raise AccountingError(
                f"userTrades hit {MAX_ROWS_PER_WINDOW} rows in one window; refusing incomplete accounting"
            )
        return rows

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        rows = self._get(
            "/fapi/v1/income",
            {
                "symbol": symbol,
                "incomeType": "FUNDING_FEE",
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": MAX_ROWS_PER_WINDOW,
            },
            signed=True,
        )
        if len(rows) >= MAX_ROWS_PER_WINDOW:
            raise AccountingError(
                f"funding income hit {MAX_ROWS_PER_WINDOW} rows; refusing incomplete accounting"
            )
        return rows


def _fetch_recent_trades(client: BinanceAccountingClient, symbol: str, days: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    rows: dict[str, dict] = {}
    cur = start
    while cur < now:
        end = min(cur + timedelta(days=WINDOW_DAYS) - timedelta(milliseconds=1), now)
        batch = client.user_trades(symbol, int(cur.timestamp() * 1000), int(end.timestamp() * 1000))
        for row in batch:
            rows[str(row["id"])] = row
        cur = end + timedelta(milliseconds=1)
    return sorted(rows.values(), key=lambda r: (int(r["time"]), int(r["id"])))


def _get_orders(app_base: str, token: str, user_id: str) -> list[dict]:
    r = requests.get(
        f"{app_base.rstrip('/')}/api/public/engine/accounting/orders",
        params={"user_id": user_id, "limit": 2000},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if not 200 <= r.status_code < 300:
        raise AccountingError(f"accounting/orders -> HTTP {r.status_code}: {r.text[:300]}")
    return r.json().get("orders") or []


def _pair_orders(orders: list[dict], symbol: str) -> list[tuple[dict, dict]]:
    """Pair each app OPEN with its following CLOSE without changing any state."""
    pairs: list[tuple[dict, dict]] = []
    pending: dict | None = None
    for order in orders:
        if order.get("symbol") != symbol or not order.get("binance_order_id"):
            continue
        intent = order.get("intent")
        if intent == "OPEN":
            # A second OPEN before a CLOSE means the order history cannot be
            # interpreted safely as one flat-to-flat trade. Start from the
            # newest OPEN and let the missing prior pair remain unreported.
            pending = order
        elif intent == "CLOSE" and pending is not None:
            if order.get("side") != pending.get("side"):
                log.warning(
                    "skip mismatched order pair | open_side=%s close_side=%s",
                    pending.get("side"), order.get("side"),
                )
                pending = None
                continue
            pairs.append((pending, order))
            pending = None
    return pairs


def _weighted_avg(fills: list[dict]) -> Decimal:
    qty = sum((Decimal(str(x["qty"])) for x in fills), Decimal(0))
    if qty <= 0:
        raise AccountingError("fill quantity is zero")
    notional = sum((Decimal(str(x["qty"])) * Decimal(str(x["price"])) for x in fills), Decimal(0))
    return notional / qty


def _commission_usdt(fills: list[dict]) -> Decimal:
    total = Decimal(0)
    for x in fills:
        asset = str(x.get("commissionAsset") or "")
        if asset != "USDT":
            raise AccountingError(
                f"commission asset {asset!r} is not USDT; refusing to estimate FX conversion"
            )
        total += Decimal(str(x.get("commission") or 0))
    if total < 0:
        raise AccountingError("negative aggregate commission is unsupported")
    return total


def _funding_usdt(client: BinanceAccountingClient, symbol: str, start_ms: int, end_ms: int) -> Decimal:
    total = Decimal(0)
    for x in client.funding(symbol, start_ms, end_ms):
        asset = str(x.get("asset") or "")
        if asset != "USDT":
            raise AccountingError(
                f"funding asset {asset!r} is not USDT; refusing to estimate FX conversion"
            )
        total += Decimal(str(x.get("income") or 0))
    return total


def _post_trade(app_base: str, token: str, payload: dict) -> dict:
    r = requests.post(
        f"{app_base.rstrip('/')}/api/public/engine/accounting/trade",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if not 200 <= r.status_code < 300:
        raise AccountingError(f"accounting/trade -> HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def sync_user(app_base: str, service_token: str, credentials_token: str, user_id: str, symbol: str, days: int) -> None:
    cred = UserCredentialsClient(app_base, credentials_token, user_id).fetch()
    if not cred.present or not cred.credentials:
        log.info("skip user=%s | Binance credentials unavailable", user_id)
        return

    client = BinanceAccountingClient(cred.credentials.api_key, cred.credentials.api_secret)
    client.sync_clock()

    orders = _get_orders(app_base, service_token, user_id)
    pairs = _pair_orders(orders, symbol)
    fills = _fetch_recent_trades(client, symbol, days)
    by_order: dict[str, list[dict]] = {}
    for fill in fills:
        by_order.setdefault(str(fill["orderId"]), []).append(fill)

    posted = 0
    for opened, closed in pairs:
        open_id = str(opened["binance_order_id"])
        close_id = str(closed["binance_order_id"])
        entry_fills = by_order.get(open_id, [])
        exit_fills = by_order.get(close_id, [])
        if not entry_fills or not exit_fills:
            # The pair may simply predate ACCOUNTING_LOOKBACK_DAYS.
            continue

        entry_qty = sum((Decimal(str(x["qty"])) for x in entry_fills), Decimal(0))
        exit_qty = sum((Decimal(str(x["qty"])) for x in exit_fills), Decimal(0))
        if entry_qty != exit_qty:
            log.warning(
                "skip partial/mismatched fills | user=%s open=%s close=%s entry_qty=%s exit_qty=%s",
                user_id, open_id, close_id, entry_qty, exit_qty,
            )
            continue

        expected_entry_side = "BUY" if opened["side"] == "LONG" else "SELL"
        expected_exit_side = "SELL" if opened["side"] == "LONG" else "BUY"
        if any(x.get("side") != expected_entry_side for x in entry_fills) or any(
            x.get("side") != expected_exit_side for x in exit_fills
        ):
            log.warning("skip unexpected fill direction | user=%s open=%s close=%s", user_id, open_id, close_id)
            continue

        # An OPEN that realizes old P&L means it was not a clean flat-to-open
        # transition (for example a manual flip). Do not fabricate a result.
        entry_realized = sum((Decimal(str(x.get("realizedPnl") or 0)) for x in entry_fills), Decimal(0))
        if entry_realized != 0:
            log.warning(
                "skip non-flat OPEN | user=%s open=%s entry_realized=%s",
                user_id, open_id, entry_realized,
            )
            continue

        gross = sum((Decimal(str(x.get("realizedPnl") or 0)) for x in exit_fills), Decimal(0))
        entry_commission = _commission_usdt(entry_fills)
        exit_commission = _commission_usdt(exit_fills)
        entry_ms = min(int(x["time"]) for x in entry_fills)
        exit_ms = max(int(x["time"]) for x in exit_fills)
        funding = _funding_usdt(client, symbol, entry_ms, exit_ms)

        payload = {
            "user_id": user_id,
            "symbol": symbol,
            "side": opened["side"],
            "open_binance_order_id": open_id,
            "close_binance_order_id": close_id,
            "entry_time": _z(entry_ms),
            "exit_time": _z(exit_ms),
            "qty": float(entry_qty),
            "entry_avg_price": float(_weighted_avg(entry_fills)),
            "exit_avg_price": float(_weighted_avg(exit_fills)),
            "entry_fill_count": len(entry_fills),
            "exit_fill_count": len(exit_fills),
            "gross_pnl_usd": float(gross),
            "entry_commission_usd": float(entry_commission),
            "exit_commission_usd": float(exit_commission),
            "funding_usd": float(funding),
        }
        result = _post_trade(app_base, service_token, payload)
        posted += 1
        net = Decimal(str(result.get("net_pnl_usd") or 0))
        commission = Decimal(str(result.get("commission_usd") or 0))
        log.info(
            "BINANCE ACCOUNTING | user=%s side=%s close_order=%s gross=%s commission=%s funding=%s net=%s",
            user_id, opened["side"], close_id, gross, commission, funding, net,
        )

    log.info("accounting sync complete | user=%s pairs=%d posted=%d", user_id, len(pairs), posted)


def main() -> None:
    app_base = os.environ.get("APP_API_BASE", "").strip()
    service_token = os.environ.get("ENGINE_SERVICE_TOKEN", "").strip()
    credentials_token = os.environ.get("ENGINE_CREDENTIALS_TOKEN", "").strip()
    raw_users = os.environ.get("ACCOUNTING_USER_IDS", "").strip()
    symbol = os.environ.get("ACCOUNTING_SYMBOL", "ETHUSDT").strip().upper()
    days = int(os.environ.get("ACCOUNTING_LOOKBACK_DAYS", "30"))

    if not app_base or not service_token or not credentials_token:
        raise SystemExit("APP_API_BASE, ENGINE_SERVICE_TOKEN and ENGINE_CREDENTIALS_TOKEN are required")
    user_ids = [x.strip() for x in raw_users.split(",") if x.strip()]
    if not user_ids:
        raise SystemExit("ACCOUNTING_USER_IDS is required; no implicit trading-user discovery")
    if days <= 0:
        raise SystemExit("ACCOUNTING_LOOKBACK_DAYS must be positive")

    failures = 0
    for user_id in user_ids:
        try:
            sync_user(app_base, service_token, credentials_token, user_id, symbol, days)
        except Exception:
            failures += 1
            log.exception("accounting sync failed | user=%s", user_id)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
