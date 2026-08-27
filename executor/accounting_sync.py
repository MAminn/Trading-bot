"""Read-only Binance accounting synchroniser.

WHAT THIS IS FOR
----------------
The website has always been able to show what the STRATEGY did (user_trades,
whose `net_pnl_rate * capital_usd` is a modelled return) and what the EXECUTOR
sent (engine_orders). Neither is the client's money. This module produces the
third fact — what BINANCE actually charged and paid — so a customer can read
their real net result after commission and funding.

BINANCE FILLS ARE THE MONETARY TRUTH
------------------------------------
Trades are reconstructed from the FILLS, not from the app's order log. A running
signed position is walked across the user's fills and every flat-to-flat episode
is one completed trade, however it was closed.

This matters because a client can close a Helix position by hand in the Binance
app, or a stop can be triggered exchange-side, and in both cases real money moves
with no CLOSE row in `engine_orders`. An earlier draft of this file paired
`engine_orders` OPEN with `engine_orders` CLOSE, so those trades — real
commission, real realised P&L — were invisible to the customer forever.

`engine_orders` is still consulted, for ATTRIBUTION only:

  * a position is Helix's only if the order that OPENED it is a Helix OPEN
    order. An episode opened by an order we did not send is the client trading
    their own account, and is never recorded;
  * whether the closing orders were Helix's decides `close_source`
    (HELIX / EXTERNAL / MIXED), which the customer sees.

WHAT THIS IS NOT
----------------
It is not part of trading. It is deliberately NOT imported by main.py,
signal_consumer.py, user_session.py, risk_guard.py, reconciler.py, multi_tenant.py
or the engine. Running it, crashing it, or deleting it cannot open, close, resize
or re-price a position:

  * `BinanceAccountingClient` exposes exactly one verb, a signed GET. There is
    no post/put/delete method on it, so there is no code path from this file to
    an order, a leverage change, a margin-type change or a position close.
  * The only endpoints it may call are listed in `BINANCE_READ_ENDPOINTS`, all
    of them read-only, and the tests assert that list has not grown.
  * The only write it performs at all is a POST to the app's own accounting
    endpoint, which upserts a reporting row.

ACCOUNTING RULES
----------------
gross_pnl_usd   sum of Binance `realizedPnl` over every position-REDUCING fill
                of the episode. Binance reports it before commission and before
                funding.
commission_usd  sum of Binance `commission` over EVERY fill of the episode.
                Never a fee percentage. If any fill was charged in an asset
                other than USDT the trade is marked INCOMPLETE rather than
                converted at a rate we cannot prove.
funding_usd     Binance FUNDING_FEE income whose timestamp falls inside this
                trade's [entry_time, exit_time], in Binance's own sign
                convention (negative = the customer paid). Episodes are
                flat-to-flat so those windows never overlap; each income record
                is additionally consumed once per run, so no funding payment can
                be attributed to two trades even when one trade's exit and the
                next trade's entry land in the same millisecond.
net_pnl_usd     gross_pnl_usd - commission_usd + funding_usd, computed by the
                database as a generated column, not here and not in the UI.

Anything that cannot be established exactly is reported as INCOMPLETE with a
short non-secret reason. Nothing is estimated.

No API key, secret, or signature is ever logged.

Required env:
  APP_API_BASE
  ENGINE_SERVICE_TOKEN
  ENGINE_CREDENTIALS_TOKEN

Optional env:
  ACCOUNTING_USER_IDS       comma-separated UUIDs. A DEVELOPMENT OVERRIDE that
                            pins the run to specific users. Left unset — the
                            production default — every connected customer is
                            discovered from the app's existing roster endpoint.
  ACCOUNTING_SYMBOL         default ETHUSDT
  ACCOUNTING_LOOKBACK_DAYS  default 30
  ACCOUNTING_DRY_RUN        "1" (or the --dry-run flag) to reconstruct and print
                            completed trades without writing a single row
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
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

# The ACCOUNTING roster: customers with Binance credentials on file.
#
# Deliberately not /api/public/engine/users/active, which is the EXECUTION
# roster and is gated on execution_mode IN (LIVE_READ, LIVE_TRADE). That gate is
# right for deciding who to trade for and wrong for deciding whose money to
# account for: a customer who presses Stop, or switches execution off, still
# made the trades they already made, and the commission Binance charged on them
# does not stop being real. Accounting eligibility must outlive trading
# eligibility, so it has its own endpoint and its own rule.
#
# Read-only, service-token gated, and it returns user ids and a truncation flag
# — no key material of any kind.
ROSTER_PATH = "/api/public/engine/accounting/users"

# The complete set of Binance endpoints this process is permitted to touch.
# All three are GET-only reads. Adding a write endpoint here would be a change
# in what this process IS, and the test suite fails if this list changes shape.
BINANCE_READ_ENDPOINTS = ("/fapi/v1/time", "/fapi/v1/userTrades", "/fapi/v1/income")

USDT = "USDT"

# How a completed position came to be closed. All three are fully accounted —
# the fills are the truth either way — but a customer should be told when a
# position left their account by a route Helix did not take.
CLOSE_SOURCES = ("HELIX", "EXTERNAL", "MIXED")

# Short, stable, non-secret reason codes. These reach the customer's screen as
# "accounting incomplete", so they must never carry account detail.
REASON_MISSING_ENTRY_FILLS = "missing_entry_fills"
REASON_MISSING_EXIT_FILLS = "missing_exit_fills"
REASON_QTY_MISMATCH = "entry_exit_quantity_mismatch"
REASON_FILL_DIRECTION = "unexpected_fill_direction"
REASON_OPEN_REALIZED_PNL = "opening_order_realized_pnl"
REASON_ZERO_QTY = "zero_fill_quantity"
REASON_NON_USDT_COMMISSION = "non_usdt_commission_asset"
REASON_NON_USDT_FUNDING = "non_usdt_funding_asset"
REASON_MULTIPLE_OPENING_ORDERS = "multiple_opening_orders"
REASON_EXTERNAL_ENTRY_ORDER = "external_order_added_to_position"


class AccountingError(RuntimeError):
    """A failure that makes this user's whole sync untrustworthy.

    Deliberately NOT raised for a single trade we cannot price: that is an
    answer, recorded as an INCOMPLETE row, not an exception.
    """


class RosterUnavailable(Exception):
    """The customer roster could not be fetched. Transient — nothing is written."""


def dec_str(value: Decimal) -> str:
    """Plain decimal text, never scientific notation.

    The payload carries decimal strings rather than JSON numbers so that no
    float64 sits between Binance's figure and the `numeric` column.
    """
    return format(value, "f")


def _z(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class BinanceAccountingClient:
    """Minimal signed GET-only Binance USD-M client.

    One verb. No method on this class can place, cancel, amend or close
    anything, and `_get` refuses any path outside BINANCE_READ_ENDPOINTS.
    """

    def __init__(self, api_key: str, api_secret: str):
        self._secret = api_secret.encode("utf-8")
        self._offset_ms = 0
        self._session = requests.Session()
        self._session.headers.update({"X-MBX-APIKEY": api_key})

    def _timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._offset_ms

    def _get(self, path: str, params: dict | None = None, *, signed: bool = False):
        if path not in BINANCE_READ_ENDPOINTS:
            # Belt and braces around the "read-only" promise: even a future edit
            # that added a write call would stop here rather than reach Binance.
            raise AccountingError(f"refusing non-accounting Binance endpoint {path!r}")
        params = dict(params or {})
        if signed:
            params["recvWindow"] = 5000
            params["timestamp"] = self._timestamp_ms()
            qs = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self._secret, qs.encode("utf-8"), hashlib.sha256).hexdigest()
        r = self._session.get(f"{BINANCE_BASE}{path}", params=params, timeout=REQUEST_TIMEOUT)
        if not 200 <= r.status_code < 300:
            # r.text can echo the query string on some Binance errors, which
            # carries the signature. Only the status is reported.
            raise AccountingError(f"Binance GET {path} -> HTTP {r.status_code}")
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
            # A truncated page means we cannot know WHICH fills are missing, so
            # every commission total in this window would be understated, and
            # the position walk below would lose its place entirely. Refuse the
            # user rather than publish a quietly wrong number.
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


# --------------------------------------------------------------------------- #
# customer discovery
# --------------------------------------------------------------------------- #

class AccountingRoster:
    """Which customers this accounting run should account for.

    Deliberately a small local client rather than an import of
    `multi_tenant.ActiveUserRoster`: the accounting process must not depend on a
    trading module, so that nothing in the trading path can be reached — or
    changed — from here. The HTTP contract is the app's, and is shared.

    The roster grants ATTENTION, never capability. It carries user ids and
    nothing else: no key material, and deliberately not even a flag saying which
    customers have connected a wallet.
    """

    def __init__(self, app_api_base: str, engine_service_token: str):
        self._url = f"{app_api_base.rstrip('/')}{ROSTER_PATH}"
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {engine_service_token}"})

    def fetch(self) -> list[str]:
        try:
            resp = self._session.get(self._url, timeout=REQUEST_TIMEOUT)
        except OSError as exc:
            raise RosterUnavailable(f"roster fetch failed: {exc}") from exc
        if resp.status_code in (401, 403):
            raise RosterUnavailable(
                f"roster endpoint rejected this process (HTTP {resp.status_code}) "
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
        if isinstance(body, dict) and body.get("truncated"):
            # Some customer is not being accounted for. Silence here would mean
            # a client whose real P&L simply never appears.
            log.warning("roster truncated at %s users — some customers are unaccounted", body.get("max"))

        out: list[str] = []
        seen: set[str] = set()
        for entry in users:
            user_id = entry.get("user_id") if isinstance(entry, dict) else entry
            if not isinstance(user_id, str) or not user_id.strip():
                continue
            user_id = user_id.strip()
            # A duplicate would sync one account twice in a run, and the second
            # pass would re-consume funding events the first already attributed.
            if user_id in seen:
                log.warning("roster contained a user twice — ignoring the duplicate")
                continue
            seen.add(user_id)
            out.append(user_id)
        return out


def resolve_users(app_base: str, service_token: str, override: str) -> list[str]:
    """The customers to account for: the pinned override, else the app roster.

    `ACCOUNTING_USER_IDS` exists for development and for re-running one client's
    accounting in isolation. Production leaves it unset, so a customer who
    connects their keys today is picked up on the next run with nobody editing
    an environment file.
    """
    pinned = [x.strip() for x in override.split(",") if x.strip()]
    if pinned:
        log.info("accounting user discovery | source=ACCOUNTING_USER_IDS count=%d", len(pinned))
        return pinned
    users = AccountingRoster(app_base, service_token).fetch()
    log.info("accounting user discovery | source=roster count=%d", len(users))
    return users


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

def _windows(days: int):
    """Chronological [start_ms, end_ms] windows covering the lookback period."""
    now = datetime.now(timezone.utc)
    cur = now - timedelta(days=days)
    while cur < now:
        end = min(cur + timedelta(days=WINDOW_DAYS) - timedelta(milliseconds=1), now)
        yield int(cur.timestamp() * 1000), int(end.timestamp() * 1000)
        cur = end + timedelta(milliseconds=1)


def fetch_recent_fills(client: BinanceAccountingClient, symbol: str, days: int) -> list[dict]:
    """Every fill in the lookback window, deduplicated and in exchange order.

    Ordering is load-bearing here, not cosmetic: the position walk below depends
    on seeing fills in the order Binance applied them.
    """
    rows: dict[str, dict] = {}
    for start_ms, end_ms in _windows(days):
        for row in client.user_trades(symbol, start_ms, end_ms):
            rows[str(row["id"])] = row
    return sorted(rows.values(), key=lambda r: (int(r["time"]), str(r["id"])))


def fetch_funding_events(client: BinanceAccountingClient, symbol: str, days: int) -> list[dict]:
    """Every FUNDING_FEE record in the lookback window, fetched ONCE.

    Fetched once rather than per trade so that attribution is a partition of one
    known list: an event assigned to a trade is removed from the pool, which is
    what makes double counting structurally impossible rather than merely
    unlikely. Deduplicated on `tranId` because adjacent windows share an edge.
    """
    rows: dict[str, dict] = {}
    for start_ms, end_ms in _windows(days):
        for row in client.funding(symbol, start_ms, end_ms):
            key = str(row.get("tranId") or f"{row.get('time')}:{row.get('income')}")
            rows[key] = row
    return sorted(rows.values(), key=lambda r: int(r["time"]))


def _get_orders(app_base: str, token: str, user_id: str, symbol: str) -> list[dict]:
    r = requests.get(
        f"{app_base.rstrip('/')}/api/public/engine/accounting/orders",
        params={"user_id": user_id, "symbol": symbol, "limit": 2000},
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT,
    )
    if not 200 <= r.status_code < 300:
        raise AccountingError(f"accounting/orders -> HTTP {r.status_code}: {r.text[:300]}")
    return r.json().get("orders") or []


def helix_order_ids(orders: list[dict], symbol: str) -> tuple[set[str], set[str]]:
    """The Binance order ids Helix opened with, and closed with, for this symbol.

    This is the whole of what the app's order log is used for. It answers
    "was this position ours?" and "did we close it?" — it never decides what a
    trade earned, and it cannot cause a trade to be reported that the fills do
    not show.
    """
    opens: set[str] = set()
    closes: set[str] = set()
    for order in orders:
        if order.get("symbol") != symbol:
            continue
        oid = order.get("binance_order_id")
        if not oid:
            continue
        if order.get("intent") == "OPEN":
            opens.add(str(oid))
        elif order.get("intent") == "CLOSE":
            closes.add(str(oid))
    return opens, closes


# --------------------------------------------------------------------------- #
# position episodes, rebuilt from fills
# --------------------------------------------------------------------------- #

def signed_qty(fill: dict) -> Decimal:
    """Effect of one fill on the position: positive for a BUY, negative for a SELL."""
    qty = Decimal(str(fill["qty"]))
    return qty if fill.get("side") == "BUY" else -qty


class Episode:
    """One run of fills between flat and flat.

    `closed` is False for the episode still running at the end of the window —
    an open position, which is not a completed trade and is never reported.
    """

    __slots__ = ("fills", "closed")

    def __init__(self, fills: list[dict], closed: bool):
        self.fills = fills
        self.closed = closed

    def __repr__(self) -> str:
        return f"<Episode fills={len(self.fills)} closed={self.closed}>"


def reconstruct_episodes(fills: list[dict], helix_open_ids: set[str]) -> list[Episode]:
    """Flat-to-flat position episodes, rebuilt from Binance fills alone.

    A running signed position is walked across the fills; every return to zero
    ends an episode. This is what lets a position closed by hand in the Binance
    app — or by an exchange-side stop, or by liquidation — still be accounted
    for: the closing fills exist, so the episode closes, whatever the app's
    order log does or does not contain.

    ANCHORING. The walk starts at the first fill belonging to a Helix OPEN
    order, and everything before it is discarded. A window that opens midway
    through a position would otherwise seed the running total at the wrong
    value and put every later episode boundary in the wrong place — reporting
    confident, wrong numbers. Discarding the leading fills costs us at most one
    trade near the edge of the lookback window and cannot misprice one.
    """
    start = None
    for i, f in enumerate(fills):
        if str(f["orderId"]) in helix_open_ids:
            start = i
            break
    if start is None:
        return []

    episodes: list[Episode] = []
    running = Decimal(0)
    current: list[dict] = []
    for f in fills[start:]:
        before = running
        running += signed_qty(f)
        current.append(f)
        if running == 0:
            # A zero-quantity fill on a flat book is not a trade; it would
            # otherwise close an "episode" that never opened.
            episodes.append(Episode(current, closed=before != 0))
            current = []
    if current:
        episodes.append(Episode(current, closed=False))
    return episodes


def split_episode(fills: list[dict]) -> tuple[list[dict], list[dict], str]:
    """Entry fills, exit fills, and the side, for one flat-to-flat episode.

    The first fill sets the direction; fills that move the same way build the
    position and fills that move the other way reduce it. Partial closes,
    scale-outs and a manual close of the remainder all land correctly in the
    exit list, because "reduces the position" is the only test applied.
    """
    long_side = signed_qty(fills[0]) > 0
    entry = [f for f in fills if (signed_qty(f) > 0) == long_side]
    exits = [f for f in fills if (signed_qty(f) > 0) != long_side]
    return entry, exits, ("LONG" if long_side else "SHORT")


def total_qty(fills: list[dict]) -> Decimal:
    return sum((Decimal(str(x["qty"])) for x in fills), Decimal(0))


def weighted_avg_price(fills: list[dict]) -> Decimal | None:
    """Quantity-weighted average fill price, or None if there is no quantity.

    This is how a multi-fill MARKET order becomes one entry or exit price: every
    fill contributes in proportion to its size, so the average shown is the price
    the customer actually got, not the price of the first or largest fill.
    """
    qty = total_qty(fills)
    if qty <= 0:
        return None
    notional = sum((Decimal(str(x["qty"])) * Decimal(str(x["price"])) for x in fills), Decimal(0))
    return notional / qty


def commission_total(fills: list[dict]) -> tuple[Decimal | None, str | None]:
    """Exact commission across ALL fills of one side of a trade.

    Returns (total, None) when every fill was charged in USDT, and
    (None, reason) when any was not — a BNB-discounted fee is a real amount of
    BNB, and turning it into dollars needs a rate Binance did not give us here.
    Guessing one would understate or overstate the customer's costs.
    """
    total = Decimal(0)
    for x in fills:
        asset = str(x.get("commissionAsset") or "")
        if asset != USDT:
            return None, f"{REASON_NON_USDT_COMMISSION}:{asset or 'unknown'}"
        total += Decimal(str(x.get("commission") or 0))
    if total < 0:
        return None, "negative_aggregate_commission"
    return total, None


def attribute_funding(
    events: list[dict],
    entry_ms: int,
    exit_ms: int,
    consumed: set[str],
) -> tuple[Decimal | None, int, str | None]:
    """Funding belonging to one position, consuming each event exactly once.

    Rule: a FUNDING_FEE record belongs to the trade whose [entry, exit] window
    contains its timestamp, INCLUSIVE of both endpoints. Binance charges funding
    on an open position, so a payment stamped exactly at the entry or exit
    millisecond is this position's.

    Inclusive endpoints mean two touching trades — a close and an immediate
    re-entry in the same millisecond — could both claim one payment. `consumed`
    settles it: episodes are processed in chronological order and the earlier
    trade takes the event, so the payment is counted once and only once.

    Funding while the account is FLAT belongs to no trade and is left alone.
    """
    total = Decimal(0)
    count = 0
    for event in events:
        key = str(event.get("tranId") or f"{event.get('time')}:{event.get('income')}")
        if key in consumed:
            continue
        ts = int(event["time"])
        if ts < entry_ms or ts > exit_ms:
            continue
        asset = str(event.get("asset") or "")
        if asset != USDT:
            # Not consumed: an event we could not read must stay available, so a
            # later run with a conversion source can still find it.
            return None, 0, f"{REASON_NON_USDT_FUNDING}:{asset or 'unknown'}"
        consumed.add(key)
        total += Decimal(str(event.get("income") or 0))
        count += 1
    return total, count, None


# --------------------------------------------------------------------------- #
# pricing one episode
# --------------------------------------------------------------------------- #

def _ordered_unique(values):
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _base_payload(user_id, symbol, side, open_id, close_id, entry_fills, exit_fills,
                  close_source, exit_order_count) -> dict:
    entry_ms = min((int(x["time"]) for x in entry_fills), default=0)
    exit_ms = max((int(x["time"]) for x in exit_fills), default=entry_ms)
    return {
        "user_id": user_id,
        "symbol": symbol,
        "side": side,
        "open_binance_order_id": open_id,
        "close_binance_order_id": close_id,
        "entry_time": _z(entry_ms),
        "exit_time": _z(max(exit_ms, entry_ms)),
        "entry_fill_count": len(entry_fills),
        "exit_fill_count": len(exit_fills),
        "exit_order_count": exit_order_count,
        "close_source": close_source,
    }


# Everything that is a money figure. An INCOMPLETE row carries none of them, so
# there is no path by which a half-established number reaches a customer.
MONEY_FIELDS = (
    "qty", "entry_avg_price", "exit_avg_price", "gross_pnl_usd",
    "entry_commission_usd", "exit_commission_usd", "funding_usd",
)


def _incomplete(base: dict, reason: str) -> dict:
    """Mark a trade unpriceable, dropping any figures already computed for it.

    The drop matters: a trade can be priced and only then found to be
    unattributable, and shipping those numbers under an INCOMPLETE status would
    be exactly the "plausible-looking figure" this layer exists to refuse.
    """
    out = {k: v for k, v in base.items() if k not in MONEY_FIELDS}
    out["accounting_status"] = "INCOMPLETE"
    out["incomplete_reason"] = reason
    out.pop("funding_event_count", None)
    return out


def classify_close_source(close_ids: list[str], helix_close_ids: set[str]) -> str:
    """Who closed the position, from the orders that reduced it.

    All three answers are fully accounted — the fills are the monetary truth
    whoever sent the closing order — but "the bot closed this" and "you closed
    this yourself in the Binance app" are different facts, and a client reading
    their history is entitled to both.
    """
    helix_closes = sum(1 for cid in close_ids if cid in helix_close_ids)
    if helix_closes == len(close_ids):
        return "HELIX"
    return "EXTERNAL" if helix_closes == 0 else "MIXED"


def build_episode_payload(
    user_id: str,
    symbol: str,
    episode: Episode,
    helix_open_ids: set[str],
    helix_close_ids: set[str],
    funding_events: list[dict],
    consumed: set[str],
) -> dict | None:
    """One completed Binance trade, priced from its fills, or a reason it was not.

    Returns None when the episode is not ours to report at all: a position still
    open, or one the client opened themselves. Every other failure produces an
    INCOMPLETE row, so the customer is told the trade exists and that we will not
    put a number on it.
    """
    if not episode.closed or not episode.fills:
        # Still open. Not a completed trade, so not a reportable one.
        return None

    entry_fills, exit_fills, side = split_episode(episode.fills)
    if not entry_fills or not exit_fills:
        return None

    open_ids = _ordered_unique(str(f["orderId"]) for f in entry_fills)
    close_ids = _ordered_unique(str(f["orderId"]) for f in exit_fills)

    # ATTRIBUTION. Helix accounts for the positions Helix opened. A position the
    # client opened by hand is their own business and is never written to their
    # Helix trade history, whatever closed it.
    if open_ids[0] not in helix_open_ids:
        return None

    payload = price_trade(
        user_id, symbol, side, open_ids[0], close_ids[-1],
        entry_fills, exit_fills,
        classify_close_source(close_ids, helix_close_ids), len(close_ids),
        funding_events, consumed,
    )

    # Something added to a Helix position after it opened. The realised P&L of
    # the close then covers size Helix did not put on, so it is not ours to
    # report as one trade. Checked after pricing so the row still carries the
    # identity of the trade the client can see on Binance.
    if len(open_ids) > 1:
        external = [oid for oid in open_ids if oid not in helix_open_ids]
        return _incomplete(
            payload,
            REASON_EXTERNAL_ENTRY_ORDER if external else REASON_MULTIPLE_OPENING_ORDERS,
        )
    return payload


def price_trade(
    user_id: str,
    symbol: str,
    side: str,
    open_id: str,
    close_id: str,
    entry_fills: list[dict],
    exit_fills: list[dict],
    close_source: str,
    exit_order_count: int,
    funding_events: list[dict],
    consumed: set[str],
) -> dict:
    """Turn one trade's fills into money, or into the reason it could not be.

    Split out from episode reconstruction so the arithmetic can be exercised on
    fills directly, including shapes the episode walk would never produce.
    """
    base = _base_payload(user_id, symbol, side, open_id, close_id,
                         entry_fills, exit_fills, close_source, exit_order_count)

    if not entry_fills:
        return _incomplete(base, REASON_MISSING_ENTRY_FILLS)
    if not exit_fills:
        return _incomplete(base, REASON_MISSING_EXIT_FILLS)

    entry_qty = total_qty(entry_fills)
    exit_qty = total_qty(exit_fills)
    if entry_qty <= 0 or exit_qty <= 0:
        return _incomplete(base, REASON_ZERO_QTY)
    if entry_qty != exit_qty:
        # The episode walk makes this structurally impossible; kept because an
        # accounting invariant that is never checked is an accounting invariant
        # that quietly stops holding.
        return _incomplete(base, REASON_QTY_MISMATCH)

    expected_entry_side = "BUY" if side == "LONG" else "SELL"
    expected_exit_side = "SELL" if side == "LONG" else "BUY"
    if any(x.get("side") != expected_entry_side for x in entry_fills) or any(
        x.get("side") != expected_exit_side for x in exit_fills
    ):
        return _incomplete(base, REASON_FILL_DIRECTION)

    # An OPEN that realises old P&L was not a clean flat-to-open transition — a
    # position was already running when Helix opened. The closing fills' realised
    # P&L would then include a result this episode did not produce.
    entry_realized = sum((Decimal(str(x.get("realizedPnl") or 0)) for x in entry_fills), Decimal(0))
    if entry_realized != 0:
        return _incomplete(base, REASON_OPEN_REALIZED_PNL)

    entry_commission, reason = commission_total(entry_fills)
    if reason:
        return _incomplete(base, reason)
    exit_commission, reason = commission_total(exit_fills)
    if reason:
        return _incomplete(base, reason)

    entry_ms = min(int(x["time"]) for x in entry_fills)
    exit_ms = max(int(x["time"]) for x in exit_fills)
    funding, funding_count, reason = attribute_funding(funding_events, entry_ms, exit_ms, consumed)
    if reason:
        return _incomplete(base, reason)

    entry_avg = weighted_avg_price(entry_fills)
    exit_avg = weighted_avg_price(exit_fills)
    if entry_avg is None or exit_avg is None:
        return _incomplete(base, REASON_ZERO_QTY)

    gross = sum((Decimal(str(x.get("realizedPnl") or 0)) for x in exit_fills), Decimal(0))

    assert entry_commission is not None and exit_commission is not None and funding is not None
    return {
        **base,
        "accounting_status": "COMPLETE",
        "qty": dec_str(entry_qty),
        "entry_avg_price": dec_str(entry_avg),
        "exit_avg_price": dec_str(exit_avg),
        "gross_pnl_usd": dec_str(gross),
        "entry_commission_usd": dec_str(entry_commission),
        "exit_commission_usd": dec_str(exit_commission),
        "funding_usd": dec_str(funding),
        "funding_event_count": funding_count,
    }


def net_pnl_of(payload: dict) -> Decimal | None:
    """The same arithmetic the database performs, for logging and cross-check.

    The database's generated column is authoritative; this exists so a
    disagreement between the two is visible in the logs rather than silent.
    """
    if payload.get("accounting_status") != "COMPLETE":
        return None
    return (
        Decimal(payload["gross_pnl_usd"])
        - Decimal(payload["entry_commission_usd"])
        - Decimal(payload["exit_commission_usd"])
        + Decimal(payload["funding_usd"])
    )


def format_accounting_log(payload: dict) -> str:
    """The customer-auditable log line. Carries no credential of any kind."""
    head = (
        f"BINANCE ACCOUNTING | user={payload['user_id']} side={payload['side']} "
        f"close_source={payload.get('close_source')}"
    )
    if payload.get("accounting_status") != "COMPLETE":
        return (
            f"{head} status=INCOMPLETE reason={payload.get('incomplete_reason')} "
            f"close_order={payload['close_binance_order_id']}"
        )
    entry_c = Decimal(payload["entry_commission_usd"])
    exit_c = Decimal(payload["exit_commission_usd"])
    net = net_pnl_of(payload)
    assert net is not None
    return (
        f"{head} gross_pnl_usd={payload['gross_pnl_usd']} "
        f"entry_commission_usd={payload['entry_commission_usd']} "
        f"exit_commission_usd={payload['exit_commission_usd']} "
        f"commission_usd={dec_str(entry_c + exit_c)} "
        f"funding_usd={payload['funding_usd']} "
        f"net_pnl_usd={dec_str(net)}"
    )


# --------------------------------------------------------------------------- #
# writers
#
# The ONLY write in this process is the upsert of a reporting row, and it lives
# behind one of these two objects. Dry-run is not a branch that skips a POST —
# it is a different writer with no network code in it at all, so there is no
# reachable path from a dry run to a database write.
# --------------------------------------------------------------------------- #

class AppTradeWriter:
    """Upserts a completed trade through the app's accounting endpoint."""

    dry_run = False

    def __init__(self, app_base: str, token: str):
        self._url = f"{app_base.rstrip('/')}/api/public/engine/accounting/trade"
        self._token = token

    def write(self, payload: dict) -> dict:
        r = requests.post(
            self._url,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=REQUEST_TIMEOUT,
        )
        if not 200 <= r.status_code < 300:
            raise AccountingError(f"accounting/trade -> HTTP {r.status_code}: {r.text[:500]}")
        return r.json()


class DryRunWriter:
    """Prints what WOULD be recorded, and cannot write anything anywhere.

    There is no requests call, no URL and no token on this class. Validating a
    real client's Binance history against production therefore cannot create,
    amend or delete a single accounting row, whatever else goes wrong.
    """

    dry_run = True

    def __init__(self, stream=None):
        self._stream = stream
        self.written: list[dict] = []

    def write(self, payload: dict) -> dict:
        self.written.append(payload)
        print(format_dry_run_row(payload), file=self._stream)
        # No database was consulted, so no generated column comes back. Returning
        # nothing keeps the caller's cross-check from comparing against a value
        # this writer invented.
        return {}


def format_dry_run_row(payload: dict) -> str:
    """One completed trade, rendered for a human reading stdout.

    Carries no key, no secret and no token — every field is a Binance figure or
    an order id the client can see in their own Binance account.
    """
    head = (
        f"[DRY-RUN] {payload['side']:<5} "
        f"{payload['entry_time']} -> {payload['exit_time']} "
        f"close_source={payload.get('close_source'):<8} "
        f"status={payload['accounting_status']}"
    )
    if payload.get("accounting_status") != "COMPLETE":
        return f"{head} reason={payload.get('incomplete_reason')}"
    entry_c = Decimal(payload["entry_commission_usd"])
    exit_c = Decimal(payload["exit_commission_usd"])
    net = net_pnl_of(payload)
    assert net is not None
    return (
        f"{head}\n"
        f"          qty={payload['qty']} "
        f"entry_avg={payload['entry_avg_price']} exit_avg={payload['exit_avg_price']}\n"
        f"          gross_pnl_usd={payload['gross_pnl_usd']}\n"
        f"          entry_commission_usd={payload['entry_commission_usd']}\n"
        f"          exit_commission_usd={payload['exit_commission_usd']}\n"
        f"          commission_usd={dec_str(entry_c + exit_c)}\n"
        f"          funding_usd={payload['funding_usd']}\n"
        f"          net_pnl_usd={dec_str(net)}"
    )


def sync_user(
    app_base: str,
    service_token: str,
    credentials_token: str,
    user_id: str,
    symbol: str,
    days: int,
    writer=None,
) -> dict:
    """Account for one user's closed Binance trades. Returns a small summary.

    `writer` owns the only write this process performs. Passing a DryRunWriter
    is what makes a dry run write-free, rather than a flag checked at the last
    moment before a POST.
    """
    if writer is None:
        writer = AppTradeWriter(app_base, service_token)
    summary = {"episodes": 0, "complete": 0, "incomplete": 0, "external_close": 0,
               "not_ours": 0, "still_open": 0}

    cred = UserCredentialsClient(app_base, credentials_token, user_id).fetch()
    if not cred.present or not cred.credentials:
        # A state, not an error: this customer has not connected a wallet, so
        # there is no Binance account to account for. Skipped, never failed.
        log.info("skip user=%s | Binance credentials unavailable (%s)",
                 user_id, cred.blocked_reason)
        return summary

    client = BinanceAccountingClient(cred.credentials.api_key, cred.credentials.api_secret)
    client.sync_clock()

    orders = _get_orders(app_base, service_token, user_id, symbol)
    open_ids, close_ids = helix_order_ids(orders, symbol)
    fills = fetch_recent_fills(client, symbol, days)
    funding_events = fetch_funding_events(client, symbol, days)
    episodes = reconstruct_episodes(fills, open_ids)
    summary["episodes"] = len(episodes)

    # Consumed across this user's whole run, so one funding payment reaches at
    # most one of their trades. Scoped per user: it is keyed on Binance
    # transaction ids from THIS account and must never be shared between users.
    consumed: set[str] = set()

    for episode in episodes:
        if not episode.closed:
            summary["still_open"] += 1
            continue
        payload = build_episode_payload(
            user_id, symbol, episode, open_ids, close_ids, funding_events, consumed,
        )
        if payload is None:
            # Not a Helix position. The client's own trading, left alone.
            summary["not_ours"] += 1
            continue

        complete = payload["accounting_status"] == "COMPLETE"
        summary["complete" if complete else "incomplete"] += 1
        if payload.get("close_source") != "HELIX":
            summary["external_close"] += 1
        log.info("%s", format_accounting_log(payload))

        result = writer.write(payload)
        if complete:
            local = net_pnl_of(payload)
            stored = result.get("net_pnl_usd")
            if stored is not None and local is not None and Decimal(str(stored)) != local:
                # The database owns net_pnl_usd. If it ever disagrees with the
                # parts we sent, the discrepancy is loud rather than displayed.
                log.error(
                    "accounting mismatch | user=%s close_order=%s local_net=%s stored_net=%s",
                    user_id, payload["close_binance_order_id"], dec_str(local), stored,
                )

    log.info(
        "accounting sync complete | user=%s symbol=%s episodes=%d complete=%d incomplete=%d "
        "external_close=%d not_ours=%d still_open=%d funding_events_attributed=%d dry_run=%s",
        user_id, symbol, summary["episodes"], summary["complete"], summary["incomplete"],
        summary["external_close"], summary["not_ours"], summary["still_open"],
        len(consumed), getattr(writer, "dry_run", False),
    )
    return summary


def is_dry_run(env: dict, argv: list[str]) -> bool:
    """Dry run from either an explicit flag or the environment.

    Both are supported because the two callers differ: an operator validating a
    client's history types `--dry-run`, and a container sets an env var.
    """
    if "--dry-run" in argv:
        return True
    return env.get("ACCOUNTING_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def run_once(env: dict | None = None, argv: list[str] | None = None) -> int:
    """One full accounting pass over every eligible customer.

    Returns the number of customers that failed. Raises only when the run could
    not be started at all — a misconfiguration, or a roster we could not fetch.
    Never raises SystemExit, so the long-running loop can call it repeatedly.
    """
    env = os.environ if env is None else env
    argv = sys.argv[1:] if argv is None else argv

    app_base = env.get("APP_API_BASE", "").strip()
    service_token = env.get("ENGINE_SERVICE_TOKEN", "").strip()
    credentials_token = env.get("ENGINE_CREDENTIALS_TOKEN", "").strip()
    override = env.get("ACCOUNTING_USER_IDS", "").strip()
    symbol = env.get("ACCOUNTING_SYMBOL", "ETHUSDT").strip().upper()
    days = int(env.get("ACCOUNTING_LOOKBACK_DAYS", "30"))
    dry_run = is_dry_run(env, argv)

    if not app_base or not service_token or not credentials_token:
        raise AccountingError(
            "APP_API_BASE, ENGINE_SERVICE_TOKEN and ENGINE_CREDENTIALS_TOKEN are required"
        )
    if days <= 0:
        raise AccountingError("ACCOUNTING_LOOKBACK_DAYS must be positive")

    # Built once for the whole run. In dry run this is the object that makes a
    # write impossible rather than merely skipped.
    writer = DryRunWriter() if dry_run else AppTradeWriter(app_base, service_token)
    if dry_run:
        print("[DRY-RUN] no accounting row will be written; Binance access is read-only")

    # A roster we could not fetch is NOT an empty roster. Letting it through as
    # "no customers" would turn a database outage into a silent no-op run.
    user_ids = resolve_users(app_base, service_token, override)
    if not user_ids:
        log.info("no customers to account for")
        return 0

    failures = 0
    for user_id in user_ids:
        # One customer's Binance outage, revoked key or malformed history must
        # never stop the others being accounted for.
        try:
            sync_user(app_base, service_token, credentials_token,
                      user_id, symbol, days, writer)
        except Exception:
            failures += 1
            log.exception("accounting sync failed | user=%s", user_id)
    log.info(
        "accounting run finished | users=%d failures=%d dry_run=%s",
        len(user_ids), failures, dry_run,
    )
    if dry_run:
        print(f"[DRY-RUN] {len(writer.written)} completed trade(s) reconstructed; 0 written")
    return failures


def main() -> None:
    try:
        failures = run_once()
    except AccountingError as exc:
        raise SystemExit(str(exc))
    except RosterUnavailable as exc:
        raise SystemExit(f"accounting roster unavailable: {exc}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
