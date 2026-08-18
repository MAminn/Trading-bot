"""Signal consumer: polls the app for pending signals and records order intents.

Binance access is limited to one unsigned, read-only mark-price fetch
(/fapi/v1/premiumIndex) used for sizing plus the trade-capable placement call.
Everything else goes to the app's API (signals/pending, config, ingest/order,
ingest/order_update). Read modes (TESTNET_READ / LIVE_READ) stay pure-read and
are refused a trader outright; trade-capable modes (TESTNET_TRADE / LIVE_TRADE)
record intents, place allowed MARKET orders, and persist the outcome.

The config endpoint response contains decrypted Binance credentials, so the
raw response is never logged; only the sizing fields are extracted.
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

import requests

from binance_client import BinanceAPIError, BinanceFuturesClient
from live_controls import allow_full_capital, resolve_live_order_cap
from reconciler import Reconciler, ReconcilerError

log = logging.getLogger("executor.consumer")

REQUEST_TIMEOUT_SECONDS = 10
# The live controls are read from this config, so it is refreshed every cycle:
# a stop, a mode change or a cap change must take effect on the next cycle, not
# up to ten minutes later.
CONFIG_REFRESH_EVERY_CYCLES = 1

STEP_SIZE = Decimal("0.001")
MIN_NOTIONAL_USD = Decimal("20")
# Code ceiling for the first soak. Not reachable from the UI.
# Applies in allocation mode only; full_capital is bounded by available margin
# and the exchange bracket cap instead.
HARD_CAP_USD = Decimal("500")
# Never commit more than this fraction of available balance as margin.
MARGIN_SAFETY_FRACTION = Decimal("0.90")

# The only two recognised sizing modes. Anything else blocks OPENs.
SIZING_MODES = frozenset({"allocation", "full_capital"})

# Transient keys carried on the intent dict for the risk guard only. They are
# stripped before POSTing so they never reach the app's Zod schema.
NON_PERSISTED_KEYS = {"leverage"}

OPENED_TO_RULE_SIDE = {"LONG": 1, "SHORT": -1}


def is_accepted_open_signal(signal: dict) -> bool:
    """True only when ml_accept is True, opened is LONG/SHORT, and rule_side agrees."""
    if signal.get("ml_accept") is not True:
        return False
    expected = OPENED_TO_RULE_SIDE.get(signal.get("opened"))
    if expected is None:
        return False
    return signal.get("rule_side") == expected

# After placing an order, poll the position until Binance reflects the fill so a
# same-cycle follow-up (e.g. a CLOSE after an OPEN) sizes against the real state.
SETTLE_MAX_POLLS = 10
SETTLE_POLL_INTERVAL_SECONDS = 2

# The execution mode determines the Binance base URL used for the unsigned
# mark-price read — never configurable via env.
MODE_BINANCE_BASE_URLS = {
    "TESTNET_READ": "https://testnet.binancefuture.com",
    "TESTNET_TRADE": "https://testnet.binancefuture.com",
    # Mainnet USD-M Futures for both live modes.
    "LIVE_READ": "https://fapi.binance.com",
    "LIVE_TRADE": "https://fapi.binance.com",
}

# Modes that may place orders, and modes that touch real funds. Kept in sync
# with main.py; asserted against the constructor arguments below.
TRADE_CAPABLE_MODES = frozenset({"TESTNET_TRADE", "LIVE_TRADE"})
LIVE_MODES = frozenset({"LIVE_READ", "LIVE_TRADE"})

# full_capital is not permitted on mainnet unless this env var is explicitly
# set to "1". The first live runs must size through the capped allocation path.
LIVE_ALLOW_FULL_CAPITAL_ENV = "LIVE_ALLOW_FULL_CAPITAL"


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
        live_order_cap_usd=None,
    ):
        if execution_mode not in MODE_BINANCE_BASE_URLS:
            raise SignalConsumerError(
                f"unsupported execution_mode {execution_mode!r} "
                f"(known: {', '.join(sorted(MODE_BINANCE_BASE_URLS))})"
            )
        # A read mode holding a trader would be a placement path in a mode that
        # must not have one. Refuse to construct rather than trust the caller.
        if execution_mode not in TRADE_CAPABLE_MODES and binance_trader is not None:
            raise SignalConsumerError(
                f"{execution_mode} is read-only and must not be given a "
                "binance_trader"
            )
        self._base = app_api_base.rstrip("/")
        self._user_id = user_id
        self._execution_mode = execution_mode
        self._symbol = symbol
        self._is_live = execution_mode in LIVE_MODES
        self._live_order_cap_usd = (
            None if live_order_cap_usd is None else Decimal(str(live_order_cap_usd))
        )
        # Read once at construction: a mid-run env change must not widen sizing.
        # This is the HOST's consent to full-capital sizing; the database carries
        # the operator's, and both are required.
        self._live_allow_full_capital_env = (
            os.environ.get(LIVE_ALLOW_FULL_CAPITAL_ENV, "").strip() == "1"
        )
        self._live_allow_full_capital_db = False
        # The env cap is this host's hard ceiling. A database cap may lower the
        # effective cap but can never raise it past this value.
        self._live_order_cap_ceiling = self._live_order_cap_usd
        # Live controls read from the database on every refresh. Every one
        # defaults to the closed position, so a missing column, an unreadable
        # value or a failed refresh can never enable anything.
        self._db_execution_mode = "OFF"
        self._db_auto_execute_enabled = False
        self._db_is_running = False
        # Optional RiskGuard (trade-capable modes only); None keeps read modes pure.
        self._risk_guard = risk_guard
        # Authenticated client for order placement; supplied ONLY in TESTNET_TRADE.
        # None in TESTNET_READ/OFF keeps those paths free of any write calls.
        self._binance_trader = binance_trader
        self._last_order_time: float | None = None
        # Last successful reconcile, for telemetry only. Never read by any
        # decision path; a stale or absent value cannot affect placement.
        self.last_reconcile: dict | None = None
        # created_at of last processed signal; optionally seeded via CONSUMER_START_AFTER.
        # Always stored in canonical Z-form — the pending route rejects +00:00 offsets.
        self._cursor: str | None = to_z_iso(start_after) if start_after else None
        self._cycles = 0
        # True between the first ensure_config() of a cycle and the end of
        # poll_once, so repeated calls in one cycle cannot double-count.
        self._cycle_counted = False
        self._leverage: Decimal | None = None
        self._capital_usd: Decimal | None = None
        self._alloc_pct: Decimal | None = None
        self._max_notional_usd: Decimal | None = None
        self._sizing_mode: str = "allocation"
        self._account_size_usd: Decimal | None = None
        # Set by any refresh that cannot be trusted to size an order. Blocks
        # OPENs only — CLOSEs are never gated on it.
        self._config_invalid: bool = False
        self._available_balance: Decimal | None = None
        self._exchange_max_leverage: int | None = None
        # Full exchange bracket ladder; the applicable cap is selected from it
        # per configured leverage on every refresh.
        self._bracket_ladder: list[dict] | None = None
        self._bracket_notional_cap: Decimal | None = None
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {engine_service_token}"})
        # Credential-less client used exclusively for the unsigned mark-price read.
        self._binance = BinanceFuturesClient(
            MODE_BINANCE_BASE_URLS[execution_mode], "", ""
        )
        # Reconciler shares the authenticated session; it only reads orders/state.
        self._reconciler = Reconciler(self._base, self._session, user_id)

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
        config = body.get("config") or {}

        # A refresh re-derives validity from scratch, so a corrected config
        # recovers on the next cycle without a restart.
        self._config_invalid = False

        for field, attr in (
            ("leverage", "_leverage"),
            ("capital_allocation_pct", "_alloc_pct"),
            ("capital_usd", "_capital_usd"),
        ):
            value = config.get(field)
            if value is None:
                raise SignalConsumerError(f"config missing {field}")
            setattr(self, attr, Decimal(str(value)))

        raw_max_notional = config.get("max_notional_usd")
        if raw_max_notional is None:
            # Pre-migration rows have no column; fall back to the code ceiling.
            self._max_notional_usd = HARD_CAP_USD
            log.info(
                "config missing max_notional_usd, defaulting to HARD_CAP_USD=%s",
                HARD_CAP_USD,
            )
        else:
            self._max_notional_usd = Decimal(str(raw_max_notional))

        # --- sizing mode -------------------------------------------------- #
        if "sizing_mode" not in config:
            # Pre-migration rows have no column. Same precedent as the
            # max_notional_usd fallback: default to the narrower mode.
            self._sizing_mode = "allocation"
            log.info("config missing sizing_mode, defaulting to allocation")
        else:
            raw_mode = config.get("sizing_mode")
            if raw_mode in SIZING_MODES:
                self._sizing_mode = raw_mode
            else:
                # Never guess: an unrecognised mode blocks OPENs outright.
                self._sizing_mode = "allocation"
                self._config_invalid = True
                log.error(
                    "invalid sizing_mode=%r (allowed: %s) — OPENs blocked",
                    raw_mode,
                    "/".join(sorted(SIZING_MODES)),
                )

        # --- account size -------------------------------------------------- #
        raw_account_size = config.get("account_size_usd")
        if raw_account_size is None:
            self._account_size_usd = None
        else:
            try:
                self._account_size_usd = Decimal(str(raw_account_size))
            except (ArithmeticError, TypeError, ValueError):
                self._account_size_usd = None
                self._config_invalid = True
                log.error(
                    "unreadable account_size_usd=%r — OPENs blocked", raw_account_size
                )

        # --- live execution controls --------------------------------------- #
        # Every one of these fails closed: an absent column, a null, or an
        # unrecognised value leaves the control in its most restrictive state.
        # The precedence rule against the environment lives in live_controls.py;
        # here we only record what the database asked for.
        raw_exec_mode = config.get("execution_mode")
        self._db_execution_mode = (
            raw_exec_mode if isinstance(raw_exec_mode, str) else "OFF"
        )
        if "execution_mode" not in config:
            log.error(
                "config has no execution_mode — treating the database request as "
                "OFF. The app may predate the live-control columns."
            )

        self._db_auto_execute_enabled = config.get("auto_execute_enabled") is True
        self._db_is_running = config.get("is_running") is True
        self._live_allow_full_capital_db = config.get("live_allow_full_capital") is True

        # The effective cap is the smaller of the host ceiling and the request.
        self._live_order_cap_usd = resolve_live_order_cap(
            self._live_order_cap_ceiling, config.get("live_order_cap_usd")
        )
        if self._risk_guard is not None:
            self._risk_guard.set_live_cap(self._live_order_cap_usd)

        # On mainnet, full_capital needs consent from BOTH the database and the
        # host. The config alone can never move a live executor onto the
        # uncapped sizing path — someone with server access has to agree too.
        if self._is_live and self._sizing_mode == "full_capital" and not allow_full_capital(
            self._live_allow_full_capital_env, self._live_allow_full_capital_db
        ):
            self._config_invalid = True
            log.error(
                "sizing_mode=full_capital is not permitted in %s without both "
                "%s=1 (host: %s) and live_allow_full_capital (database: %s) "
                "— OPENs blocked",
                self._execution_mode,
                LIVE_ALLOW_FULL_CAPITAL_ENV,
                self._live_allow_full_capital_env,
                self._live_allow_full_capital_db,
            )

        # full_capital sizes directly off account_size_usd, so a missing or
        # non-positive value leaves the sizing base undefined. Fail closed.
        if self._sizing_mode == "full_capital" and (
            self._account_size_usd is None or self._account_size_usd <= 0
        ):
            self._config_invalid = True
            log.error(
                "sizing_mode=full_capital requires account_size_usd > 0 "
                "(got %r) — OPENs blocked",
                self._account_size_usd,
            )

        # Leverage may have moved, so the applicable bracket tier may have too.
        self._apply_bracket_selection()

        log.info(
            "config refreshed | leverage=%s | alloc_pct=%s | capital_usd=%s | "
            "max_notional_usd=%s",
            self._leverage,
            self._alloc_pct,
            self._capital_usd,
            self._max_notional_usd,
        )
        # The line that determines exposure. Never log the response body.
        log.info(
            "sizing | mode=%s | account_size_usd=%s | capital_usd=%s | "
            "leverage=%s | max_notional_usd=%s",
            self._sizing_mode,
            self._account_size_usd,
            self._capital_usd,
            self._leverage,
            self._max_notional_usd,
        )
        # The control plane, logged every refresh. Never the response body:
        # these are the only fields worth reading and none of them is a secret.
        log.info(
            "live controls | db_execution_mode=%s | auto_execute=%s | "
            "is_running=%s | db_allow_full_capital=%s | cap: host=%s db=%s "
            "effective=%s",
            self._db_execution_mode,
            self._db_auto_execute_enabled,
            self._db_is_running,
            self._live_allow_full_capital_db,
            self._live_order_cap_ceiling,
            config.get("live_order_cap_usd"),
            self._live_order_cap_usd,
        )
        if self._risk_guard is not None:
            self._risk_guard.update_limits(max_notional_usd=self._max_notional_usd)
            self._risk_guard.set_sizing_mode(self._sizing_mode)

    def ensure_config(self) -> None:
        """Refresh config if due. Safe to call every cycle.

        Owns the refresh schedule and the cycle counter. Idempotent within a
        cycle: only the first call of a cycle refreshes and increments, so
        _cycles advances exactly once per cycle however many callers there are."""
        if self._cycle_counted:
            return
        if self._leverage is None or self._cycles % CONFIG_REFRESH_EVERY_CYCLES == 0:
            self._refresh_config()
        self._cycles += 1
        self._cycle_counted = True

    # ------------------------------------------------------------------ #
    # live controls, as last read from the database
    # ------------------------------------------------------------------ #

    @property
    def db_execution_mode(self) -> str:
        return self._db_execution_mode

    @property
    def db_auto_execute_enabled(self) -> bool:
        return self._db_auto_execute_enabled

    @property
    def db_is_running(self) -> bool:
        return self._db_is_running

    @property
    def live_order_cap_usd(self):
        """The cap actually in force: min(host ceiling, database request)."""
        return self._live_order_cap_usd

    @property
    def live_order_cap_ceiling(self):
        """This host's hard ceiling from .env; never raised by any config."""
        return self._live_order_cap_ceiling

    def end_cycle(self) -> None:
        """Close the cycle without running poll_once.

        ensure_config() refuses to refresh twice in one cycle, and poll_once's
        `finally` is normally what reopens it. A caller that returns early —
        idling at effective OFF, for instance — must call this instead, or the
        next ensure_config() sees a stranded flag, skips its refresh, and the
        executor never notices the database changing back."""
        self._cycle_counted = False

    def set_trader(self, trader) -> None:
        """Attach or detach the order-placement client for this cycle.

        None removes the placement path entirely: `_process_intent` reaches
        `_place_order` only through `self._binance_trader is not None`, so a
        detached trader is not a flag that a later edit could invert — there is
        no object to place with."""
        self._binance_trader = trader

    @property
    def desired_leverage(self) -> int | None:
        """Configured leverage as an int, or None before the first refresh."""
        return int(self._leverage) if self._leverage is not None else None

    def set_leverage_limits(self, max_leverage: int, brackets: list[dict]) -> None:
        """Record the exchange's leverage ceiling and the FULL bracket ladder.

        The applicable notional cap depends on configured leverage, which can
        change on any config refresh, so it is selected from the ladder rather
        than fixed here."""
        self._exchange_max_leverage = int(max_leverage)
        self._bracket_ladder = [
            {
                "initialLeverage": int(b["initialLeverage"]),
                "notionalCap": Decimal(str(b["notionalCap"])),
            }
            for b in brackets
        ]
        self._apply_bracket_selection()

    @staticmethod
    def select_bracket(brackets: list[dict], configured_leverage: int) -> dict | None:
        """Return the bracket that applies at configured_leverage, or None.

        A tier qualifies when it permits at least the configured leverage;
        among those the LARGEST notionalCap is the true bound. None means no
        tier permits this leverage — a config error, never licence to size
        without a cap."""
        qualifying = [
            b
            for b in brackets
            if int(b["initialLeverage"]) >= int(configured_leverage)
        ]
        if not qualifying:
            return None
        return max(qualifying, key=lambda b: Decimal(str(b["notionalCap"])))

    def _apply_bracket_selection(self) -> None:
        """Re-select the bracket cap for the configured leverage. Called from
        the probe and from every config refresh, since either input can move."""
        if self._bracket_ladder is None or self._leverage is None:
            # Ladder not probed (read-only modes) or config not yet loaded.
            # Leaving the cap unset matches pre-existing behaviour; not an error.
            self._bracket_notional_cap = None
            return
        configured = int(self._leverage)
        selected = self.select_bracket(self._bracket_ladder, configured)
        if selected is None:
            # Unreachable while RiskGuard rejects leverage > max_leverage, but
            # never rely on that: fail closed rather than size uncapped.
            self._bracket_notional_cap = None
            self._config_invalid = True
            log.error(
                "BRACKET SELECTED | configured_leverage=%dx | no bracket permits "
                "this leverage (exchange max=%sx) — OPENs blocked",
                configured,
                self._exchange_max_leverage,
            )
            return
        self._bracket_notional_cap = Decimal(str(selected["notionalCap"]))
        log.info(
            "BRACKET SELECTED | configured_leverage=%dx | "
            "selected_bracket_leverage=%dx | selected_bracket_notional_cap=%s",
            configured,
            int(selected["initialLeverage"]),
            self._bracket_notional_cap,
        )

    def set_available_balance(self, bal: Decimal) -> None:
        """Record the account's available balance for the margin sanity check."""
        self._available_balance = Decimal(str(bal))

    def _post_order(self, order: dict) -> bool:
        """POST the intent. Returns True if newly created (201), False if the
        app reports it as a duplicate (409)."""
        payload = {
            k: v
            for k, v in order.items()
            if v is not None and k not in NON_PERSISTED_KEYS
        }
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

        order = {
            "user_id": self._user_id,
            "signal_bar_time": bar_time,
            "symbol": self._symbol,
            "side": side,
            "intent": "OPEN",
            "qty": 0.0,
            "ref_price": float(ref_price),
            "notional_usd": 0.0,
            "execution_mode": self._execution_mode,
            "status": "INTENT_LOGGED",
            "idempotency_key": idempotency_key,
            # Transient: read by the risk guard, stripped before the POST.
            "leverage": int(self._leverage),
        }

        mode = self._sizing_mode

        # Uncertainty never widens: an unusable sizing config sizes nothing.
        if self._config_invalid:
            order["status"] = "SKIPPED"
            order["error"] = "invalid sizing config"
            log.error("SIZE BLOCKED | invalid sizing config | key=%s", idempotency_key)
            return order

        if mode == "full_capital":
            # Load-bearing. Allocation mode tolerates an unknown balance because
            # HARD_CAP_USD still binds; here nothing would bound the base at all.
            if self._available_balance is None:
                order["status"] = "SKIPPED"
                order["error"] = "available balance unknown"
                log.error(
                    "SIZE BLOCKED | available balance unknown | key=%s",
                    idempotency_key,
                )
                return order
            # base = min(account_size, available_balance * MARGIN_SAFETY_FRACTION),
            # expressed as a cap on the target so the binding constraint is named.
            target = self._account_size_usd * self._leverage
            caps = [
                (
                    "available_margin",
                    self._available_balance
                    * MARGIN_SAFETY_FRACTION
                    * self._leverage,
                )
            ]
        else:
            target = self._capital_usd * self._leverage
            caps = [
                ("max_notional_usd", self._max_notional_usd),
                ("hard_cap", HARD_CAP_USD),
            ]
        if self._bracket_notional_cap is not None:
            caps.append(("bracket_cap", self._bracket_notional_cap))
        # Appended last and outside the mode branch: the live cap binds in
        # allocation and full_capital alike, and no config value can lift it.
        if self._live_order_cap_usd is not None:
            caps.append(("live_order_cap", self._live_order_cap_usd))

        notional_target = target
        bound_by = None
        for name, cap in caps:
            if cap < notional_target:
                notional_target = cap
                bound_by = name
        if bound_by is not None:
            log.info(
                "SIZE CLAMPED | mode=%s | target=%s -> %s | bound_by=%s",
                mode,
                target,
                notional_target,
                bound_by,
            )

        qty = (notional_target / ref_price).quantize(STEP_SIZE, rounding=ROUND_DOWN)
        notional = qty * ref_price
        order["qty"] = float(qty)
        order["notional_usd"] = float(notional)

        if notional < MIN_NOTIONAL_USD:
            order["status"] = "SKIPPED"
            order["error"] = "below min notional"
        elif self._available_balance is not None and (
            notional / self._leverage
            > self._available_balance * MARGIN_SAFETY_FRACTION
        ):
            # Redundant by construction in full_capital; kept in both modes as a
            # second line of defence.
            order["status"] = "SKIPPED"
            order["error"] = "insufficient margin for configured size"
        return order

    def _build_close_intent(self, signal: dict, ref_price: Decimal, position_amt) -> dict | None:
        """Build a CLOSE intent for a flatten signal. The side being closed is
        position_before (LONG or SHORT); returns None if it is neither, so the
        caller can skip with a warning. qty is the absolute current Binance
        position amount, rounded DOWN to step size. CLOSE is exempt from the
        min-notional gate, so notional is informational only."""
        side = signal.get("position_before")
        if side not in ("LONG", "SHORT"):
            return None
        bar_time = to_z_iso(signal["bar_time"])
        idempotency_key = f"{self._user_id}:{bar_time}:{side}:CLOSE"

        qty = abs(Decimal(str(position_amt or 0))).quantize(STEP_SIZE, rounding=ROUND_DOWN)
        notional = qty * ref_price

        return {
            "user_id": self._user_id,
            "signal_bar_time": bar_time,
            "symbol": self._symbol,
            "side": side,
            "intent": "CLOSE",
            "qty": float(qty),
            "ref_price": float(ref_price),
            "notional_usd": float(notional),
            "execution_mode": self._execution_mode,
            "status": "INTENT_LOGGED",
            "idempotency_key": idempotency_key,
        }

    # ------------------------------------------------------------------ #
    # order placement (trade-capable modes only)
    # ------------------------------------------------------------------ #

    def _place_order(self, order: dict):
        """Place a MARKET order for an allowed, newly-created intent and record
        the result via ingest/order_update. TESTNET_TRADE only. Never retries a
        given key (the client_order_id makes retries rejectable anyway).

        Returns the position amount after the order settles (see _settle_open /
        _settle_close), or None if the placement itself failed."""
        idempotency_key = order["idempotency_key"]
        # 35 chars, alphanumeric: "x" + 32 hex chars of sha256(idempotency_key).
        client_order_id = "x" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        is_close = order["intent"] == "CLOSE"
        if is_close:
            # Closing LONG sells, closing SHORT buys; reduceOnly guards against flips.
            mapped_side = "SELL" if order["side"] == "LONG" else "BUY"
        else:
            mapped_side = "BUY" if order["side"] == "LONG" else "SELL"

        try:
            result = self._binance_trader.place_market_order(
                self._symbol, mapped_side, order["qty"], client_order_id,
                reduce_only=is_close,
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
            return None

        binance_id = result.get("orderId")
        binance_status = result.get("status")
        log.info(
            "ORDER SENT | %s %s | qty=%s | binance_id=%s | status=%s",
            order["side"],
            order["intent"],
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
        # Wait for the position to reflect this order before the caller moves on
        # to any further signal in the same cycle. Returns the settled amount.
        if is_close:
            return self._settle_close()
        return self._settle_open(order["side"])

    # ------------------------------------------------------------------ #
    # settle helpers: reconcile the local view with Binance after placement
    # ------------------------------------------------------------------ #

    def _read_position_amt(self):
        """Fresh signed read of the symbol's position amount; None on read error."""
        try:
            positions = self._binance_trader.get_positions(self._symbol)
        except (BinanceAPIError, OSError) as exc:
            log.error("settle position read failed: %s", exc)
            return None
        if not positions:
            return 0.0
        try:
            return float(positions[0].get("positionAmt", 0) or 0)
        except (TypeError, ValueError):
            return None

    def _settle_open(self, side: str) -> float:
        """Poll until the just-placed OPEN shows up as a nonzero position with
        the matching sign (negative for SHORT, positive for LONG). Returns the
        settled amount. If it does not settle within SETTLE_MAX_POLLS, raise so
        the cycle fails without advancing the cursor: the OPEN was already posted
        and placed (safe under idempotency), and any pending CLOSE stays
        unprocessed for the retry."""
        for n in range(1, SETTLE_MAX_POLLS + 1):
            amt = self._read_position_amt()
            if amt is not None and (
                (side == "SHORT" and amt < 0) or (side == "LONG" and amt > 0)
            ):
                log.info("SETTLED | pos=%s after %d polls", amt, n)
                return amt
            if n < SETTLE_MAX_POLLS:
                time.sleep(SETTLE_POLL_INTERVAL_SECONDS)
        log.error("open order not yet reflected in position — failing cycle for retry")
        raise SignalConsumerError("open order not settled")

    def _settle_close(self) -> float:
        """Poll until the just-placed CLOSE flattens the position. Returns the
        last-read amount. A lingering nonzero here is not a cycle failure —
        reconciliation catches it later — so this only warns and continues."""
        amt = 0.0
        for n in range(1, SETTLE_MAX_POLLS + 1):
            amt = self._read_position_amt()
            if amt == 0.0:
                log.info("SETTLED | pos=%s after %d polls", amt, n)
                return 0.0
            if n < SETTLE_MAX_POLLS:
                time.sleep(SETTLE_POLL_INTERVAL_SECONDS)
        log.warning("WARN | close sent but position not yet flat")
        return amt if amt is not None else 0.0

    # ------------------------------------------------------------------ #
    # reconciliation (runs at the start of every cycle)
    # ------------------------------------------------------------------ #

    def reconcile(self, position_amt) -> tuple[bool, str | None]:
        """Reconcile the app's last-executed record against the live position at
        cycle start. Returns (opens_blocked, block_reason).

        Trade-capable mode (TESTNET_TRADE): closes out stale intents via
        order_update, and blocks OPENs when the kill switch is off or the
        position mismatches. A fetch failure raises SignalConsumerError so the
        unified retry counter treats it as a failed cycle — the fail-safe that
        guarantees no OPEN is placed in a cycle that could not reconcile.

        Read-only mode (TESTNET_READ): log-only. It logs the RECONCILE line but
        writes no order_update, never blocks (nothing is placed anyway), and a
        fetch failure is swallowed."""
        trade_capable = self._binance_trader is not None
        try:
            state = self._reconciler.reconcile(position_amt)
        except ReconcilerError as exc:
            if trade_capable:
                # poll_once will not run this cycle, so close the cycle here
                # instead — otherwise the next ensure_config() would see a
                # stranded flag and skip its refresh.
                self._cycle_counted = False
                log.error("reconcile failed, failing cycle: %s", exc)
                raise SignalConsumerError(f"reconcile failed: {exc}") from exc
            log.warning("reconcile failed (read-only, ignored): %s", exc)
            return False, None

        log.info(
            "RECONCILE | match=%s | expected=%s | actual=%s | is_running=%s",
            state["match"],
            state["expected"],
            position_amt,
            state["is_running"],
        )

        # Record for telemetry. Set only on a successful reconcile, so the
        # timestamp always describes the reading it is attached to.
        self.last_reconcile = {
            "expected": state["expected"],
            "match": state["match"],
            "actual": position_amt,
            "at": to_z_iso(datetime.now(timezone.utc)),
        }

        if not trade_capable:
            # Read-only reconciliation is purely informational.
            return False, None

        # Close out intents that were logged but never sent (stuck INTENT_LOGGED).
        for key in state["stale_intents"]:
            self._post_order_update(
                {
                    "user_id": self._user_id,
                    "idempotency_key": key,
                    "status": "FAILED",
                    "error": "stale: never sent",
                }
            )
            log.info("STALE INTENT CLOSED | %s", key)

        reason = None
        if not state["is_running"]:
            reason = "kill_switch_active"
        elif not state["match"]:
            reason = "reconcile_mismatch"
        return reason is not None, reason

    # ------------------------------------------------------------------ #
    # public entrypoint
    # ------------------------------------------------------------------ #

    def poll_once(
        self,
        position_amt=None,
        opens_blocked: bool = False,
        block_reason: str | None = None,
    ) -> None:
        """One poll cycle: refresh config as scheduled, fetch pending signals,
        log each as an order intent. Raises SignalConsumerError on failure.

        position_amt: current position amount for the symbol, used by the risk
        guard in trade-capable modes.

        opens_blocked / block_reason: supplied by the caller from reconcile().
        When set, OPEN intents this cycle are recorded SKIPPED with block_reason
        and never placed; CLOSE intents are evaluated and placed as usual."""
        # ensure_config() owns both the refresh schedule and the cycle counter,
        # so _cycles increments exactly once per cycle even when main.py has
        # already called it earlier in the same cycle.
        self.ensure_config()
        try:
            self._poll_signals(position_amt, opens_blocked, block_reason)
        finally:
            # poll_once is the last step of a cycle: close it so the next
            # ensure_config() counts again, whether or not this one raised.
            self._cycle_counted = False

    # ------------------------------------------------------------------ #
    # smoke test: one synthetic OPEN + CLOSE, used to prove a live path
    # ------------------------------------------------------------------ #

    def run_smoke_test(self, side: str, position_amt) -> int:
        """Place one synthetic OPEN, wait for it to settle, then CLOSE it.

        Returns a process exit code: 0 only if the position finished flat.

        No pending signal is fetched and the cursor is never moved, so this
        neither consumes nor skips any natural ML signal. The intents are built
        by the same _build_intent / _build_close_intent and evaluated by the
        same risk guard as a real signal — the only synthetic part is the
        trigger. The OPEN is therefore subject to the live cap, the bracket
        cap, the min-notional floor and the margin check exactly as usual.
        """
        if self._binance_trader is None:
            log.error("SMOKE ABORTED | no trader in %s", self._execution_mode)
            return 1

        try:
            starting_amt = float(position_amt or 0)
        except (TypeError, ValueError):
            log.error("SMOKE ABORTED | unreadable starting position %r", position_amt)
            return 1
        if starting_amt != 0:
            log.error(
                "SMOKE ABORTED | position is not flat (amt=%s) — the smoke test "
                "only ever runs from flat",
                starting_amt,
            )
            return 1

        # Config must be loaded before sizing; main.py has already refreshed it
        # this cycle, but the smoke path must not depend on that ordering.
        self.ensure_config()
        try:
            ref_price = Decimal(str(self._binance.get_mark_price(self._symbol)))
        except (BinanceAPIError, OSError) as exc:
            log.error("SMOKE ABORTED | mark price unavailable: %s", exc)
            return 1

        bar_time = datetime.now(timezone.utc)
        signal = {
            "bar_time": bar_time,
            "rule_side": 1 if side == "LONG" else -1,
            "position_before": side,
        }

        log.warning(
            "SMOKE OPEN | %s %s | ref_price=%s | placing a real order",
            self._symbol,
            side,
            ref_price,
        )
        open_order = self._build_intent(signal, ref_price)
        try:
            settled = self._process_intent(open_order, starting_amt)
        except SignalConsumerError as exc:
            # _settle_open raises when the OPEN does not show up as a position
            # within the settle window. The order was already sent, so the true
            # state is unknown — never guess and never auto-close on a state we
            # could not read. Stop and hand it to a human.
            log.error(
                "SMOKE HALTED | OPEN was sent but did not settle: %s | STATE "
                "UNKNOWN — check ETHUSDT on Binance now and close it manually "
                "if a position is open",
                exc,
            )
            return 1

        if open_order["status"] != "INTENT_LOGGED":
            log.error(
                "SMOKE HALTED | OPEN was not placed | status=%s | reason=%s | "
                "nothing to close, no position was taken",
                open_order["status"],
                open_order.get("error"),
            )
            return 1

        try:
            settled_amt = float(settled or 0)
        except (TypeError, ValueError):
            settled_amt = 0.0
        if settled_amt == 0:
            log.error(
                "SMOKE HALTED | OPEN sent but position still reads flat — "
                "CHECK BINANCE MANUALLY before rerunning; a fill may be pending"
            )
            return 1

        log.warning("SMOKE OPEN FILLED | pos=%s | closing immediately", settled_amt)

        close_order = self._build_close_intent(signal, ref_price, settled_amt)
        if close_order is None:
            log.error(
                "SMOKE HALTED | could not build CLOSE for side=%r | POSITION IS "
                "OPEN — close it manually on Binance now",
                side,
            )
            return 1
        final_amt = self._process_intent(close_order, settled_amt)

        try:
            final = float(final_amt or 0)
        except (TypeError, ValueError):
            final = None
        if final != 0:
            log.error(
                "SMOKE FAILED | position did not go flat (amt=%s) — CLOSE IT "
                "MANUALLY ON BINANCE NOW",
                final_amt,
            )
            return 1

        log.warning("SMOKE PASSED | %s OPEN and CLOSE completed | position flat", side)
        return 0

    def _poll_signals(
        self,
        position_amt,
        opens_blocked: bool,
        block_reason: str | None,
    ) -> None:
        """Fetch pending signals and record/place an intent for each."""
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

        # Track the position amount across the batch: placements settle mid-cycle,
        # so a later signal (e.g. a CLOSE after an OPEN) must size against the
        # settled value, not the stale cycle-start read.
        current_amt = position_amt
        for signal in signals:
            # A signal may carry both a close (closed_reason + position_after
            # FLAT) and an entry (rule_side +/-1) — e.g. a reversal. Process the
            # CLOSE first, then the OPEN, as independent intents each with its
            # own idempotency key and guard evaluation.
            if (
                signal.get("closed_reason") is not None
                and signal.get("position_after") == "FLAT"
            ):
                close_order = self._build_close_intent(signal, ref_price, current_amt)
                if close_order is None:
                    log.warning(
                        "CLOSE signal with unusable position_before=%r, skipping | "
                        "bar_time=%s",
                        signal.get("position_before"),
                        signal.get("bar_time"),
                    )
                else:
                    current_amt = self._process_intent(
                        close_order, current_amt, opens_blocked, block_reason
                    )

            if is_accepted_open_signal(signal):
                current_amt = self._process_intent(
                    self._build_intent(signal, ref_price),
                    current_amt,
                    opens_blocked,
                    block_reason,
                )
            elif signal.get("rule_side") in (1, -1):
                log.info(
                    "NO OPEN | rule_side=%r not an accepted open "
                    "(ml_accept=%r opened=%r) | bar_time=%s",
                    signal.get("rule_side"),
                    signal.get("ml_accept"),
                    signal.get("opened"),
                    signal.get("bar_time"),
                )

            # Advance cursor only after the intent(s) are persisted (or confirmed
            # duplicate) so a mid-batch failure resumes from the right place next
            # cycle. Normalized to Z-form: the pending route rejects +00:00 offsets.
            self._cursor = to_z_iso(signal["created_at"])

    def _process_intent(self, order: dict, position_amt, opens_blocked: bool = False, block_reason: str | None = None):
        """Guard, persist, and (in TESTNET_TRADE) place a single intent. Returns
        the current position amount, updated to the settled value after a
        placement so a same-cycle follow-up sizes against the real state.

        A blocked OPEN (kill switch off or reconcile mismatch) is recorded
        SKIPPED with block_reason and never placed. CLOSE intents ignore the
        block."""
        if (
            order["intent"] == "OPEN"
            and opens_blocked
            and order["status"] == "INTENT_LOGGED"
        ):
            order["status"] = "SKIPPED"
            order["error"] = block_reason
            log.info(
                "BLOCKED | OPEN %s | %s | key=%s",
                order["side"],
                block_reason,
                order["idempotency_key"],
            )
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
            "INTENT | %s %s | qty=%s | ref_price=%s | notional=%s | key=%s",
            order["side"],
            order["intent"],
            order["qty"],
            order["ref_price"],
            order["notional_usd"],
            order["idempotency_key"],
        )
        created = self._post_order(order)
        if created and order["status"] == "INTENT_LOGGED":
            # Feed the guard's min-interval check from allowed OPEN intents only;
            # a CLOSE must never block the OPEN half of a same-cycle reversal.
            if self._risk_guard is not None and order["intent"] == "OPEN":
                self._last_order_time = time.time()
            # Placement is enabled only in TESTNET_TRADE (trader supplied there).
            if self._binance_trader is not None:
                settled = self._place_order(order)
                if settled is not None:
                    return settled
        return position_amt
