"""Effective execution mode: the environment's capability AND the database's
request, never either one alone.

The environment is the ceiling. `.env` says what this host is *permitted* to
do; the database says what the operator is *asking* for right now. The result
is the weaker of the two, always:

    effective_level  = min(env_level, db_level)
    effective_network = network(env)          # never from the database

Two properties follow, and they are the point of this module:

  * No database value can raise capability. A row saying LIVE_TRADE on a host
    whose env says LIVE_READ yields LIVE_READ. Compromising the database, or
    the app that writes to it, cannot arm a host that was never armed.
  * No database value can move a host between networks. The testnet/mainnet
    choice comes from `.env` alone, so a mainnet request on a testnet host
    stays on testnet rather than silently becoming real money.

Everything here is pure: no I/O, no environment reads, no logging. It is the
one place the precedence rule is written down, so it can be tested exhaustively
and cannot drift from a second copy elsewhere.
"""

from decimal import Decimal, InvalidOperation

# Capability levels. Ordering is the whole mechanism: min() over these is what
# makes the environment a ceiling rather than a suggestion.
LEVEL_OFF = 0
LEVEL_READ = 1
LEVEL_TRADE = 2

# What each .env mode permits at most.
ENV_MODE_LEVEL = {
    "OFF": LEVEL_OFF,
    "TESTNET_READ": LEVEL_READ,
    "TESTNET_TRADE": LEVEL_TRADE,
    "LIVE_READ": LEVEL_READ,
    "LIVE_TRADE": LEVEL_TRADE,
}

# Which exchange each .env mode talks to. Deliberately not derivable from the
# database: this is the testnet/mainnet decision and it is host-local.
ENV_MODE_NETWORK = {
    "OFF": None,
    "TESTNET_READ": "TESTNET",
    "TESTNET_TRADE": "TESTNET",
    "LIVE_READ": "LIVE",
    "LIVE_TRADE": "LIVE",
}

# What the web may request. Narrower than the env set on purpose — a UI has no
# business selecting a network.
DB_MODE_LEVEL = {
    "OFF": LEVEL_OFF,
    "LIVE_READ": LEVEL_READ,
    "LIVE_TRADE": LEVEL_TRADE,
}

MODE_BY_NETWORK_LEVEL = {
    ("TESTNET", LEVEL_READ): "TESTNET_READ",
    ("TESTNET", LEVEL_TRADE): "TESTNET_TRADE",
    ("LIVE", LEVEL_READ): "LIVE_READ",
    ("LIVE", LEVEL_TRADE): "LIVE_TRADE",
}

# Modes that may place an order. Kept identical to main.py's set and asserted
# against it in the tests, so the two can never drift.
TRADE_CAPABLE_MODES = frozenset({"TESTNET_TRADE", "LIVE_TRADE"})


def db_mode_level(db_mode) -> int:
    """Level a database request grants, or LEVEL_OFF for anything unrecognised.

    An absent column (pre-migration row, or an app that predates the whitelist
    entry) and an unparseable value are both LEVEL_OFF. Uncertainty about what
    was requested is never resolved as permission to trade.
    """
    if not isinstance(db_mode, str):
        return LEVEL_OFF
    return DB_MODE_LEVEL.get(db_mode.strip(), LEVEL_OFF)


def resolve_effective_mode(env_mode: str, db_mode) -> str:
    """The mode this cycle actually runs in."""
    env_level = ENV_MODE_LEVEL.get(env_mode)
    network = ENV_MODE_NETWORK.get(env_mode)
    # An unrecognised env mode is a misconfiguration, not a licence.
    if env_level is None or env_level == LEVEL_OFF or network is None:
        return "OFF"

    level = min(env_level, db_mode_level(db_mode))
    if level == LEVEL_OFF:
        return "OFF"
    return MODE_BY_NETWORK_LEVEL[(network, level)]


def is_trade_capable(mode: str) -> bool:
    return mode in TRADE_CAPABLE_MODES


def _to_decimal(value):
    """Decimal, or None when the value is absent or unreadable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return None


def resolve_live_order_cap(env_cap, db_cap):
    """The per-order notional ceiling actually in force: the smallest of the
    caps that are known.

    The environment cap is the host's hard ceiling and the database cap is the
    operator's request. Taking the minimum means a database value can only ever
    lower exposure. An unreadable database cap is treated as absent rather than
    as zero or as infinity: the mode gate has already failed closed by then, and
    inventing a number here would either fabricate a block or fabricate room.
    """
    caps = [c for c in (_to_decimal(env_cap), _to_decimal(db_cap)) if c is not None]
    if not caps:
        return None
    return min(caps)


def allow_full_capital(env_allow: bool, db_allow) -> bool:
    """Full-capital sizing needs consent from BOTH sources.

    The database toggle records the operator's intent; the environment flag
    records that someone with access to the host agreed. Either alone is
    insufficient by design — this is the one sizing path with no internal
    notional ceiling.
    """
    return bool(env_allow) and db_allow is True


def placement_block_reason(
    *,
    effective_mode: str,
    db_execution_mode,
    auto_execute_enabled,
    is_running,
    live_order_cap_usd,
    ack_present: bool,
) -> str | None:
    """Why an OPEN may not be placed this cycle, or None when every gate passes.

    Checked in order of how fundamental each gate is, so the reported reason is
    the most informative one. CLOSE is deliberately NOT governed by this: a
    position that is already open must stay closable when auto-execute is
    switched off or the kill switch is thrown, exactly as reconcile mismatches
    have always behaved.
    """
    if effective_mode != "LIVE_TRADE":
        return f"effective_mode={effective_mode}"
    # Redundant given the lattice, and kept anyway: the two are checked
    # independently so a future change to one cannot quietly widen the other.
    if db_execution_mode != "LIVE_TRADE":
        return f"db_execution_mode={db_execution_mode!r}"
    if not ack_present:
        return "live_trading_ack_missing"
    if auto_execute_enabled is not True:
        return "auto_execute_disabled"
    if is_running is not True:
        return "kill_switch_active"
    cap = _to_decimal(live_order_cap_usd)
    if cap is None or cap <= 0:
        return f"live_order_cap_invalid={live_order_cap_usd!r}"
    return None
