"""One user's execution state, and the trading cycle that runs against it.

This is the single implementation of the trading cycle. Both the single-user
executor and the multi-tenant loop drive the same class, because two copies of
the gate logic would be a worse defect than the one this module exists to fix:
a safety check that is enforced in one loop and forgotten in the other is
indistinguishable from no safety check at all.

Isolation is the design constraint. Everything that could carry one user's
identity, funds or position into another user's decision lives on the instance
and nowhere else:

  * `_credentials` / `_client`  - the key pair and the object that signs with it
  * `_consumer`                 - cursor, sizing config, reconciler, trader
  * `_risk_guard`               - per-user caps
  * `_probed` / `_startup_done` - exchange state established for THIS account
  * `_effective_mode`, `_orders_enabled`, `_blocked_reason`

There is no module-level mutable state, no shared client, and no cache keyed by
anything other than the session itself. A session is constructed with a user id
and can only ever act on that user.

What is shared, deliberately, is read-only: the host's environment ceiling (mode,
network, ack, order cap) and the app's base URL. The environment remains the
ceiling for every user on the host; the database can only ever narrow it.
"""

import logging
from decimal import Decimal

log = logging.getLogger("executor.session")

SYMBOL = "ETHUSDT"

# Strategy stop distance assumed purely for the liquidation-vs-stop warning.
SL_PCT_ASSUMED = 0.015


class EnforcementError(Exception):
    """Account-config enforcement failed in a retryable way."""


class FatalConfigError(Exception):
    """Account config could not be verified — refuse to run trade-capable."""


class HostLimits:
    """What this HOST permits, for every user on it.

    Read once at startup and never mutated. Passed to every session rather than
    read from the environment inside one, so a session cannot acquire capability
    the host was not started with, and a test can state the ceiling explicitly.
    """

    __slots__ = (
        "env_mode",
        "base_url",
        "trade_capable",
        "ack_present",
        "app_api_base",
        "engine_service_token",
        "is_live",
    )

    def __init__(
        self,
        env_mode: str,
        base_url: str,
        trade_capable: bool,
        ack_present: bool,
        app_api_base: str,
        engine_service_token: str,
        is_live: bool,
    ):
        self.env_mode = env_mode
        self.base_url = base_url
        self.trade_capable = trade_capable
        self.ack_present = ack_present
        self.app_api_base = app_api_base
        self.engine_service_token = engine_service_token
        self.is_live = is_live


class UserSession:
    """Everything the executor knows about one user, and how it acts on it.

    Constructed per user. Never reused across users, never shares a Binance
    client, and never reads another user's configuration.
    """

    def __init__(
        self,
        user_id: str,
        limits: HostLimits,
        credentials_client=None,
        env_credentials=None,
        reporter=None,
        start_after=None,
        symbol: str = SYMBOL,
        smoke_side=None,
    ):
        if not user_id:
            raise ValueError("a session must belong to a user")
        # Deferred so this module can be imported without the executor's
        # optional dependencies present.
        from binance_client import BinanceFuturesClient, ReadOnlyFuturesClient
        from signal_consumer import SignalConsumer

        self._BinanceFuturesClient = BinanceFuturesClient
        self._ReadOnlyFuturesClient = ReadOnlyFuturesClient

        self.user_id = user_id
        self._limits = limits
        self._symbol = symbol
        self._reporter = reporter

        # Live hosts fetch the user's own keys; testnet hosts use the host pair.
        self._credentials_client = credentials_client
        self._env_credentials = env_credentials

        self._client = None
        self._credentials = None
        # None until a live session has actually asked. "Not yet known" is not
        # the same claim as "none connected".
        self._keys_present = None if credentials_client is not None else True

        self._risk_guard = None
        if limits.trade_capable:
            from risk_guard import RiskGuard

            self._risk_guard = RiskGuard(max_leverage=1)

        self._consumer = SignalConsumer(
            limits.app_api_base,
            limits.engine_service_token,
            user_id,
            limits.env_mode,
            symbol,
            start_after=start_after,
            risk_guard=self._risk_guard,
            # Starts detached. The trader is attached per cycle, and only when
            # this user's EFFECTIVE mode is trade-capable, so a session begins
            # closed and stays closed until the database has been read.
            binance_trader=None,
        )

        if not limits.is_live and env_credentials is not None:
            self._credentials = env_credentials
            self._client = self._build_client(
                env_credentials.api_key, env_credentials.api_secret
            )

        # Exchange state established against THIS user's account.
        self._probed = False
        self._startup_done = False
        self._liquidation_warning_logged = False
        self._exchange_max_leverage = None

        # Starts at OFF so anything reading it before the first successful
        # config refresh sees the closed position.
        self._effective_mode = "OFF"
        self._orders_enabled = False
        self._blocked_reason = None

        # Per-user failure counter. One user's outage must not retire another.
        self.consecutive_failures = 0

        # Smoke test: one synthetic OPEN then CLOSE, then exit. Single-user runs
        # only — a multi-tenant loop never sets it, because "place one order and
        # exit" is not a thing to do to a process holding other clients' funds.
        self._smoke_side = smoke_side
        # Set the instant the smoke test may have sent its first order. From that
        # point a failed cycle must NOT be retried: a retry could place a second
        # synthetic order against a position the first one already opened.
        self.smoke_started = False

    # -- introspection, for the loop and the tests ------------------------- #

    @property
    def effective_mode(self) -> str:
        return self._effective_mode

    @property
    def orders_enabled(self) -> bool:
        return self._orders_enabled

    @property
    def blocked_reason(self):
        return self._blocked_reason

    @property
    def keys_present(self):
        return self._keys_present

    @property
    def client(self):
        """The signing client, or None. Exposed for assertions about isolation."""
        return self._client

    @property
    def consumer(self):
        return self._consumer

    def __repr__(self) -> str:
        # user_id is not a secret; the credentials on this object are, and they
        # are never rendered.
        return (
            f"<UserSession user={self.user_id} effective={self._effective_mode} "
            f"orders_enabled={self._orders_enabled}>"
        )

    # -- clients ----------------------------------------------------------- #

    def _build_client(self, key: str, secret: str):
        """Read modes get a facade with no write methods defined at all.

        Placement is not merely gated off in a read mode — set_leverage,
        set_margin_type and place_market_order do not exist on the object, so no
        code path can reach them.
        """
        if self._limits.trade_capable:
            return self._BinanceFuturesClient(self._limits.base_url, key, secret)
        return self._ReadOnlyFuturesClient(self._limits.base_url, key, secret)

    def _resolve_credentials(self):
        """This user's live credentials. Returns a blocked_reason, or None.

        Re-resolved every cycle, which is what lets a client connect, rotate or
        disconnect their keys on the website and have the executor follow within
        one cycle instead of at the next restart.
        """
        if self._credentials_client is None:
            return None

        # Raises CredentialsUnavailable on a transport failure, which fails this
        # user's cycle only — the same fail-closed retry every other fetch
        # failure gets, and nothing another user's session ever sees.
        result = self._credentials_client.fetch()
        if result.present:
            self._keys_present = True
            if result.credentials != self._credentials:
                rotated = self._credentials is not None
                self._credentials = result.credentials
                self._client = self._build_client(
                    result.credentials.api_key, result.credentials.api_secret
                )
                # A new key pair is a different account. The clock offset,
                # symbol filters, bracket ladder and leverage enforcement were
                # established against the old one and are redone before anything
                # is sized against the new one.
                self._probed = False
                self._startup_done = False
                log.warning(
                    "LIVE CREDENTIALS %s | user=%s | key ...%s",
                    "ROTATED" if rotated else "BOUND",
                    self.user_id,
                    result.credentials.last4,
                )
            return None

        self._keys_present = False
        # The client is dropped, not merely bypassed. Once this user's keys are
        # gone the process must hold no means of signing on their behalf.
        self._credentials = None
        self._client = None
        self._probed = False
        self._startup_done = False
        return result.blocked_reason

    # -- telemetry --------------------------------------------------------- #

    def report(self, account=None, positions=None, message=None, permission=None):
        """Send one telemetry snapshot for THIS user.

        Contains its own failures: a telemetry problem must never disturb a
        trading cycle, and must never disturb another user's cycle.
        """
        if self._reporter is None:
            return
        from executor_status import build_snapshot

        try:
            self._reporter.report(
                build_snapshot(
                    mode=self._effective_mode,
                    # The .env value: the ceiling, not the outcome.
                    env_mode_ceiling=self._limits.env_mode,
                    db_execution_mode=self._consumer.db_execution_mode,
                    auto_execute_enabled=self._consumer.db_auto_execute_enabled,
                    orders_enabled=self._orders_enabled,
                    blocked_reason=self._blocked_reason,
                    account=account,
                    positions=positions,
                    reconcile=self._consumer.last_reconcile,
                    keys_present=self._keys_present,
                    permission_status=permission,
                    message=message,
                ),
                user_id=self.user_id,
            )
        except Exception as exc:  # noqa: BLE001
            # Telemetry is best-effort by contract. In a multi-tenant loop it is
            # also a shared surface, so an exception escaping here would let one
            # user's reporting failure abort another user's cycle.
            log.warning(
                "telemetry failed for user=%s: %s | trading is unaffected",
                self.user_id,
                exc,
            )

    # -- exchange work ----------------------------------------------------- #

    def _extract_filters(self, symbol_info: dict):
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

    def _probe_leverage_brackets(self) -> None:
        """One-time trade-capable probe of the symbol's leverage brackets.

        The exchange is authoritative: no order may be sized or placed before the
        ceiling and the bracket ladder are known, so a failure here is retryable
        rather than ignorable.
        """
        from binance_client import BinanceAPIError

        try:
            brackets = self._client.get_leverage_brackets(self._symbol)
        except (BinanceAPIError, OSError) as exc:
            raise EnforcementError(f"leverage bracket probe failed: {exc}") from exc
        if not brackets:
            raise EnforcementError(f"no leverage brackets returned for {self._symbol}")

        ladder = []
        for entry in brackets:
            try:
                ladder.append(
                    {
                        "initialLeverage": int(entry["initialLeverage"]),
                        "notionalCap": Decimal(str(entry["notionalCap"])),
                    }
                )
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                raise EnforcementError(f"unreadable leverage bracket: {entry!r}") from exc

        # brackets[0] is the highest-leverage tier and carries the SMALLEST
        # notionalCap — correct as the leverage ceiling, wrong as the notional
        # cap for a lower configured leverage. The full ladder is retained and
        # the applicable tier is re-selected per configured leverage.
        max_leverage = ladder[0]["initialLeverage"]

        log.info(
            "BRACKET LADDER | user=%s | %s | tiers=%d",
            self.user_id,
            self._symbol,
            len(ladder),
        )
        self._exchange_max_leverage = max_leverage
        self._consumer.set_leverage_limits(max_leverage, ladder)
        if self._risk_guard is not None:
            self._risk_guard.update_limits(max_leverage=max_leverage)

    def _enforce_account_config(self, desired_leverage: int) -> bool:
        """Bring the account in line with the configured leverage on isolated
        margin. Returns True when the account matches, False when it does not and
        could not be fixed (the caller then blocks OPENs).

        A leverage mismatch NEVER raises: an already-open position must stay
        closable. FatalConfigError is reserved for isolated margin, which is
        non-negotiable.
        """
        from binance_client import BinanceAPIError

        desired = int(desired_leverage)
        if self._exchange_max_leverage is not None and desired > self._exchange_max_leverage:
            log.warning(
                "CLAMPED | user=%s | configured leverage %dx exceeds %s exchange "
                "max %dx — using %dx",
                self.user_id,
                desired,
                self._symbol,
                self._exchange_max_leverage,
                self._exchange_max_leverage,
            )
            desired = self._exchange_max_leverage

        positions = self._client.get_positions(self._symbol)
        pos = positions[0] if positions else {}
        amt = float(pos.get("positionAmt", 0) or 0)
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()

        if leverage == str(desired) and margin_type == "isolated":
            return True

        if amt != 0:
            log.warning(
                "config change deferred: position open | user=%s | %s | account "
                "leverage=%s margin=%s | desired=%dx — OPENs blocked, CLOSE still "
                "allowed",
                self.user_id,
                self._symbol,
                leverage,
                margin_type,
                desired,
            )
            return False

        try:
            self._client.set_margin_type(self._symbol, "ISOLATED")
        except BinanceAPIError as exc:
            log.error(
                "HALT | user=%s | %s isolated margin could not be set while flat: %s",
                self.user_id,
                self._symbol,
                exc,
            )
            raise FatalConfigError("isolated margin could not be set") from exc

        try:
            self._client.set_leverage(self._symbol, desired)
        except BinanceAPIError as exc:
            if exc.code == -4028:
                log.error(
                    "leverage %dx rejected by exchange (-4028) | user=%s | OPENs blocked",
                    desired,
                    self.user_id,
                )
                return False
            raise

        positions = self._client.get_positions(self._symbol)
        pos = positions[0] if positions else {}
        leverage = str(pos.get("leverage", ""))
        margin_type = str(pos.get("marginType", "")).lower()
        if margin_type != "isolated":
            log.error(
                "HALT | user=%s | %s margin=%s (want isolated) after write",
                self.user_id,
                self._symbol,
                margin_type,
            )
            raise FatalConfigError("isolated margin could not be set")
        if leverage != str(desired):
            log.warning(
                "leverage verification failed | user=%s | %s leverage=%s (want %dx) "
                "— OPENs blocked",
                self.user_id,
                self._symbol,
                leverage,
                desired,
            )
            return False
        log.info(
            "ENFORCED | user=%s | %s | leverage=%dx | margin=ISOLATED",
            self.user_id,
            self._symbol,
            desired,
        )
        return True

    # -- the cycle --------------------------------------------------------- #

    def run_cycle(self):
        """One full cycle for this user.

        Returns None normally, or a process exit code when a smoke test has run
        to completion. Every gate that existed in the single-user executor is
        evaluated here, in the same order, from values read this cycle — so a
        Stop, a mode change or a cap change takes effect on the next cycle rather
        than at the next restart.

        Raises on failure, which the caller counts against THIS user only.
        """
        from live_controls import (
            is_trade_capable,
            placement_block_reason,
            resolve_effective_mode,
        )

        # The config drives the mode decision, so it is refreshed before any
        # exchange call. A failed refresh raises and fails this user's cycle,
        # which leaves the previous state untouched and places nothing.
        self._consumer.ensure_config()

        previous_mode = self._effective_mode
        self._effective_mode = resolve_effective_mode(
            self._limits.env_mode, self._consumer.db_execution_mode
        )
        effective_trade_capable = is_trade_capable(self._effective_mode)
        if self._effective_mode != previous_mode:
            log.warning(
                "EFFECTIVE MODE | user=%s | %s -> %s | env_ceiling=%s | db_request=%s",
                self.user_id,
                previous_mode,
                self._effective_mode,
                self._limits.env_mode,
                self._consumer.db_execution_mode,
            )

        # Skipped entirely at effective OFF: that mode makes no exchange call, so
        # it needs no key and should not be asking the app for one.
        credentials_reason = None
        if self._effective_mode != "OFF":
            credentials_reason = self._resolve_credentials()

        self._blocked_reason = placement_block_reason(
            effective_mode=self._effective_mode,
            db_execution_mode=self._consumer.db_execution_mode,
            auto_execute_enabled=self._consumer.db_auto_execute_enabled,
            is_running=self._consumer.db_is_running,
            ack_present=self._limits.ack_present,
        )
        self._orders_enabled = self._blocked_reason is None

        if credentials_reason is not None:
            # Outranks every other reason: without this user's keys there is no
            # authenticated path to their account at all.
            self._blocked_reason = credentials_reason
            self._orders_enabled = False

        # Attach the placement client only when this user's effective mode is
        # trade-capable. Detaching removes the placement path rather than setting
        # a flag beside it.
        self._consumer.set_trader(
            self._client
            if (effective_trade_capable and self._client is not None)
            else None
        )

        if self._effective_mode == "OFF":
            log.info(
                "user idle | user=%s | effective=OFF | env_ceiling=%s | db_request=%s",
                self.user_id,
                self._limits.env_mode,
                self._consumer.db_execution_mode,
            )
            self.report(
                message=(
                    f"idle: effective OFF (env {self._limits.env_mode}, database "
                    f"{self._consumer.db_execution_mode})"
                )
            )
            # end_cycle is what normally reopens the cycle; returning without it
            # would strand the flag and stop every later refresh for this user.
            self._consumer.end_cycle()
            return None

        if credentials_reason is not None:
            # Fail closed. No client was built, so no signed call is made, no
            # balance is read and no order can be placed — the refusal is
            # structural, not a flag checked further down. The session stays
            # alive and keeps heartbeating so the website shows the reason.
            log.error(
                "LIVE ACCESS BLOCKED | user=%s | %s | effective=%s | no mainnet "
                "call will be made for this user",
                self.user_id,
                credentials_reason,
                self._effective_mode,
            )
            self.report(message=f"blocked: {credentials_reason}")
            self._consumer.end_cycle()
            return None

        if not self._startup_done:
            offset = self._client.sync_clock()
            log.info("clock synced | user=%s | offset=%dms", self.user_id, offset)
            symbol_info = self._client.get_exchange_info(self._symbol)
            tick_size, step_size, min_notional = self._extract_filters(symbol_info)
            log.info(
                "%s filters | tick_size=%s | step_size=%s | min_notional=%s",
                self._symbol,
                tick_size,
                step_size,
                min_notional,
            )

        # Gated on the EFFECTIVE mode: a host whose .env permits trading but
        # whose database asks for read-only must not probe or enforce.
        if effective_trade_capable and not self._probed:
            self._probe_leverage_brackets()
            self._probed = True

        positions = self._client.get_positions(self._symbol)
        account = self._client.get_account()
        # The capital base for every OPEN this cycle: THIS user's own
        # totalWalletBalance, read with THIS user's own credentials. Passed
        # together with availableBalance, which is only ever a margin sanity
        # check. Neither figure is stored, and a session can hold no other
        # user's account.
        self._consumer.set_account_balances(
            wallet_balance=account.get("totalWalletBalance"),
            available_balance=account.get("availableBalance"),
        )

        if not self._startup_done:
            self._startup_done = True
            log.info(
                "account | user=%s | total_wallet_balance=%s | available_balance=%s",
                self.user_id,
                account.get("totalWalletBalance"),
                account.get("availableBalance"),
            )

        pos_amt = positions[0].get("positionAmt") if positions else "0"
        log.info(
            "user alive | user=%s | mode=%s | %s pos=%s | bal=%s",
            self.user_id,
            self._effective_mode,
            self._symbol,
            pos_amt,
            account.get("availableBalance"),
        )

        try:
            position_amt = float(pos_amt or 0)
        except (TypeError, ValueError):
            position_amt = 0.0

        leverage_blocked = False
        desired = self._consumer.desired_leverage
        # set_leverage / set_margin_type are writes. Gating them on the EFFECTIVE
        # mode is what makes a degraded LIVE_READ genuinely read-only even on a
        # host whose .env would permit trading.
        if effective_trade_capable and desired is not None:
            if not self._liquidation_warning_logged:
                self._liquidation_warning_logged = True
                if desired > 0 and 1 / desired < SL_PCT_ASSUMED:
                    log.warning(
                        "WARNING | user=%s | leverage %dx puts liquidation at "
                        "~%.2f%% which is inside the assumed %.2f%% stop",
                        self.user_id,
                        desired,
                        100.0 / desired,
                        SL_PCT_ASSUMED * 100.0,
                    )
            if not self._enforce_account_config(desired):
                leverage_blocked = True

        opens_blocked, block_reason = self._consumer.reconcile(position_amt)
        if leverage_blocked:
            # OPENs blocked, CLOSEs still allowed — the same asymmetric
            # kill-switch behaviour reconcile() already relies on.
            opens_blocked = True
            if block_reason is None:
                block_reason = "leverage_config_mismatch"
        if self._blocked_reason is not None:
            # The live-control gates block OPENs only. A position that is already
            # open must stay closable when auto-execute is switched off or Stop
            # is pressed — stranding real exposure behind a control flag would be
            # worse than the exposure itself.
            opens_blocked = True
            if block_reason is None:
                block_reason = self._blocked_reason

        # Reported here: reconcile has just run and every branch below is reached
        # through this point. get_account() above is a signed call, so reaching
        # this line is itself the proof that this user's credential works.
        self.report(
            account=account,
            positions=positions,
            permission="verified_futures",
            message=(f"opens blocked: {block_reason}" if opens_blocked else "cycle ok"),
        )

        if self._smoke_side is not None:
            # The smoke test replaces signal processing for this run: no pending
            # ML signal is fetched, sized or placed. It runs only after the full
            # startup path above (clock, filters, bracket probe, account
            # enforcement, reconcile) has succeeded, so it faces exactly the same
            # preconditions a natural OPEN would.
            if not effective_trade_capable:
                log.error(
                    "SMOKE ABORTED | effective mode is %s (env %s, database %s) — "
                    "the smoke test never runs outside a trade-capable effective mode",
                    self._effective_mode,
                    self._limits.env_mode,
                    self._consumer.db_execution_mode,
                )
                return 1
            if opens_blocked:
                log.error(
                    "SMOKE ABORTED | OPENs are blocked (%s) — refusing to place a "
                    "synthetic order while the executor would reject a real one",
                    block_reason,
                )
                return 1
            self.smoke_started = True
            return self._consumer.run_smoke_test(self._smoke_side, position_amt)

        self._consumer.poll_once(
            position_amt=position_amt,
            opens_blocked=opens_blocked,
            block_reason=block_reason,
        )
        return None
