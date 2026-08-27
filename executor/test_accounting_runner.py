"""How accounting actually runs in production, and what it still cannot do.

Three properties are under test, and each of them is load-bearing:

  1. ISOLATION. The accounting runner is a separate process with a separate
     command. It cannot start the trading executor, it imports no trading
     module, and its container is configured so that even a hand-run main.py
     inside it could not place an order.

  2. DRY RUN IS WRITE-FREE. Before this is pointed at a real client's Binance
     history we need to prove it cannot create a single accounting row. That is
     asserted structurally — the dry-run writer has no network code in it at all
     — rather than by trusting a flag checked before a POST.

  3. THE LOOP SURVIVES ITSELF. One failing pass, one failing customer, or a
     container killed mid-pass must not stop accounting; the writes are
     idempotent, so the safe response to any failure is to try again.

Nothing here reaches a network, and nothing here starts a container.
"""

import io
import logging
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest

import accounting_loop
import accounting_sync
from accounting_loop import AlreadyRunning, RunLock, Stopper, resolve_interval, run_forever

ALICE = "aaaaaaaa-1111-1111-1111-111111111111"
BOB = "bbbbbbbb-2222-2222-2222-222222222222"
SYMBOL = "ETHUSDT"

T_ENTRY = 1785110400000
T_EXIT = T_ENTRY + 8 * 3600 * 1000

EXECUTOR_DIR = Path(accounting_sync.__file__).parent
COMPOSE = EXECUTOR_DIR / "docker-compose.yml"
DOCKERFILE = EXECUTOR_DIR / "Dockerfile"

BASE_ENV = {
    "APP_API_BASE": "http://app",
    "ENGINE_SERVICE_TOKEN": "svc-token",
    "ENGINE_CREDENTIALS_TOKEN": "cred-token",
}


def fill(order_id, qty, price, commission, *, side="BUY", realized="0",
         t=T_ENTRY, asset="USDT", fid=None):
    return {
        "id": fid or f"{order_id}-{t}",
        "orderId": order_id,
        "qty": qty,
        "price": price,
        "commission": commission,
        "commissionAsset": asset,
        "realizedPnl": realized,
        "side": side,
        "time": t,
    }


def order(intent, side="LONG", oid="1", symbol=SYMBOL):
    return {"intent": intent, "side": side, "binance_order_id": oid, "symbol": symbol}


@pytest.fixture
def one_real_trade(monkeypatch):
    """A single completed Binance trade, with no network anywhere."""

    class Cred:
        present = True
        blocked_reason = None

        class credentials:
            api_key = "ACCTKEY_aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            api_secret = "ACCTSECRET_bbbbbbbbbbbbbbbbbbbbbbbb"

    monkeypatch.setattr(
        accounting_sync, "UserCredentialsClient",
        lambda *a, **k: type("C", (), {"fetch": lambda self: Cred()})(),
    )
    monkeypatch.setattr(accounting_sync.BinanceAccountingClient, "sync_clock", lambda self: None)
    monkeypatch.setattr(
        accounting_sync, "_get_orders",
        lambda base, token, user_id, symbol, since=None: [order("OPEN", "LONG", "100"),
                                              order("CLOSE", "LONG", "200")],
    )
    monkeypatch.setattr(
        accounting_sync, "fetch_recent_fills",
        lambda c, s, d: [
            fill("100", "1", "3000", "0.44887517", side="BUY", t=T_ENTRY, fid="1"),
            fill("200", "1", "2992.57590", "0.45258722", side="SELL",
                 realized="-7.42410000", t=T_EXIT, fid="2"),
        ],
    )
    monkeypatch.setattr(accounting_sync, "fetch_funding_events", lambda c, s, d: [])
    monkeypatch.setattr(accounting_sync, "resolve_users", lambda *a, **k: [ALICE])
    return Cred


# --------------------------------------------------------------------------- #
# dry run writes nothing
# --------------------------------------------------------------------------- #

def strip_prose(source: str) -> str:
    """Executable lines only — comments and docstrings removed.

    These files explain at length what they are forbidden to do, and naming
    those things in prose is exactly the documentation we want. Scanning the
    raw text would make the explanation fail the test it is explaining.
    """
    out = []
    in_doc = False
    for line in source.splitlines():
        stripped = line.strip()
        if in_doc:
            if stripped.endswith('"""'):
                in_doc = False
            continue
        if stripped.startswith('"""'):
            # A one-line docstring opens and closes on the same line.
            if not (len(stripped) > 3 and stripped.endswith('"""')):
                in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_the_dry_run_writer_has_no_way_to_write():
    """Not a flag before a POST — an object with no network code in it."""
    source = Path(accounting_sync.__file__).read_text(encoding="utf-8")
    body = strip_prose(
        source[source.index("class DryRunWriter"):source.index("def format_dry_run_row")]
    )
    for forbidden in ("requests", "http", "post", "_url", "_token"):
        assert forbidden not in body, f"DryRunWriter mentions {forbidden}"


def test_a_dry_run_pass_makes_zero_write_requests(monkeypatch, one_real_trade, capsys):
    """The acceptance property: pointing this at production changes nothing."""
    calls = []
    monkeypatch.setattr(accounting_sync.requests, "post",
                        lambda *a, **k: calls.append(a) or pytest.fail("dry run posted"))
    monkeypatch.setattr(accounting_sync.requests, "put",
                        lambda *a, **k: pytest.fail("dry run wrote"), raising=False)
    monkeypatch.setattr(accounting_sync.requests, "delete",
                        lambda *a, **k: pytest.fail("dry run wrote"), raising=False)

    failures = accounting_sync.run_once({**BASE_ENV, "ACCOUNTING_DRY_RUN": "1"}, argv=[])
    assert failures == 0
    assert calls == []
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "0 written" in out


def test_the_cli_flag_also_selects_dry_run(monkeypatch, one_real_trade):
    monkeypatch.setattr(accounting_sync.requests, "post",
                        lambda *a, **k: pytest.fail("dry run posted"))
    assert accounting_sync.run_once(BASE_ENV, argv=["--dry-run"]) == 0


def test_is_dry_run_reads_flag_or_env():
    assert accounting_sync.is_dry_run({}, ["--dry-run"]) is True
    assert accounting_sync.is_dry_run({"ACCOUNTING_DRY_RUN": "1"}, []) is True
    assert accounting_sync.is_dry_run({"ACCOUNTING_DRY_RUN": "true"}, []) is True
    assert accounting_sync.is_dry_run({"ACCOUNTING_DRY_RUN": "0"}, []) is False
    assert accounting_sync.is_dry_run({}, []) is False


def test_a_real_run_does_write(monkeypatch, one_real_trade):
    """The negative control: without dry run, the same pass posts."""
    posted = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"net_pnl_usd": "-8.32556239"}

    monkeypatch.setattr(accounting_sync.requests, "post",
                        lambda url, **k: posted.append(url) or FakeResponse())
    assert accounting_sync.run_once(BASE_ENV, argv=[]) == 0
    assert posted == ["http://app/api/public/engine/accounting/trade"]


def test_the_dry_run_report_shows_every_required_figure(one_real_trade, capsys):
    accounting_sync.run_once({**BASE_ENV, "ACCOUNTING_DRY_RUN": "1"}, argv=[])
    out = capsys.readouterr().out
    for field in ("LONG", "close_source=", "status=COMPLETE",
                  "qty=", "entry_avg=", "exit_avg=", "gross_pnl_usd=",
                  "entry_commission_usd=", "exit_commission_usd=", "commission_usd=",
                  "funding_usd=", "net_pnl_usd="):
        assert field in out, field
    # Entry and exit times, both marked UTC.
    assert out.count("Z ->") >= 1


def test_the_dry_run_reproduces_the_known_short_round_trip(capsys):
    """The real historical reference, computed from fills rather than hard-coded."""
    fills = [
        fill("100", "1", "3000", "0.44887517", side="SELL", t=T_ENTRY, fid="1"),
        fill("200", "1", "3007.4241", "0.45258722", side="BUY",
             realized="-7.42410000", t=T_EXIT, fid="2"),
    ]
    (episode,) = accounting_sync.reconstruct_episodes(fills, {"100"})
    payload = accounting_sync.build_episode_payload(
        ALICE, SYMBOL, episode, {"100"}, {"200"}, [], set()
    )
    writer = accounting_sync.DryRunWriter()
    writer.write(payload)
    out = capsys.readouterr().out
    assert "gross_pnl_usd=-7.42410000" in out
    assert "entry_commission_usd=0.44887517" in out
    assert "exit_commission_usd=0.45258722" in out
    assert "commission_usd=0.90146239" in out
    assert "net_pnl_usd=-8.32556239" in out
    assert payload["side"] == "SHORT"


def test_the_dry_run_output_carries_no_credential(one_real_trade, capsys):
    accounting_sync.run_once({**BASE_ENV, "ACCOUNTING_DRY_RUN": "1"}, argv=[])
    out = capsys.readouterr().out
    for secret in ("ACCTKEY_aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                   "ACCTSECRET_bbbbbbbbbbbbbbbbbbbbbbbb",
                   "svc-token", "cred-token"):
        assert secret not in out


# --------------------------------------------------------------------------- #
# the runner cannot trade
# --------------------------------------------------------------------------- #

def test_the_runner_imports_no_trading_module():
    source = Path(accounting_loop.__file__).read_text(encoding="utf-8")
    imports = [line.strip() for line in source.splitlines()
               if re.match(r"^\s*(import |from \S+ import )", line)]
    for module in ("main", "signal_consumer", "user_session", "risk_guard",
                   "reconciler", "multi_tenant", "binance_client", "live_controls"):
        for line in imports:
            assert not re.search(rf"\b{module}\b", line), f"{line!r} reaches trading code"
    assert any("accounting_sync" in line for line in imports)


def test_the_runner_never_names_the_executor_entrypoint():
    code = strip_prose(Path(accounting_loop.__file__).read_text(encoding="utf-8"))
    for forbidden in ("main.py", "subprocess", "os.system", "os.exec", "popen"):
        assert forbidden not in code.lower(), forbidden


def test_the_compose_accounting_service_runs_only_the_accounting_loop():
    compose = COMPOSE.read_text(encoding="utf-8")
    assert 'command: ["python", "accounting_loop.py"]' in compose
    # Overriding the image CMD is what keeps main.py out of this container.
    assert 'CMD ["python", "main.py"]' in DOCKERFILE.read_text(encoding="utf-8")
    # Defence in depth: even a hand-run executor here could not trade.
    assert 'EXECUTION_MODE: "OFF"' in compose


def test_the_accounting_service_is_independent_of_the_executor():
    compose = COMPOSE.read_text(encoding="utf-8")
    accounting = compose[compose.index("  accounting:"):]
    # No ordering or lifecycle coupling: accounting runs when trading is stopped.
    for coupling in ("depends_on", "network_mode", "volumes_from", "links:"):
        assert coupling not in accounting, coupling
    # Opt-in, so deploying the executor cannot silently start writing rows.
    assert 'profiles: ["accounting"]' in accounting
    assert "restart: unless-stopped" in accounting


def test_the_executor_service_is_untouched():
    compose = COMPOSE.read_text(encoding="utf-8")
    executor = compose[compose.index("  executor:"):compose.index("  # Read-only Binance")]
    assert executor.strip() == (
        "executor:\n    build: .\n    restart: unless-stopped\n    env_file: .env"
    ).strip()


# --------------------------------------------------------------------------- #
# single instance
# --------------------------------------------------------------------------- #

def test_two_passes_cannot_run_at_once(tmp_path):
    path = str(tmp_path / "acct.lock")
    with RunLock(path):
        with pytest.raises(AlreadyRunning):
            RunLock(path).acquire()


def test_the_lock_is_released_for_the_next_pass(tmp_path):
    path = str(tmp_path / "acct.lock")
    with RunLock(path):
        pass
    with RunLock(path):
        pass
    assert not os.path.exists(path)


def test_a_stale_lock_is_taken_over_rather_than_wedging_accounting(tmp_path):
    """A container killed mid-pass must not stop accounting forever.

    Taking over is safe because the upsert is idempotent: the worst outcome is
    a trade restated to the same values.
    """
    path = str(tmp_path / "acct.lock")
    RunLock(path).acquire()
    os.utime(path, (0, 0))
    with RunLock(path, stale_seconds=60):
        pass


def test_a_fresh_lock_is_never_stolen(tmp_path):
    path = str(tmp_path / "acct.lock")
    RunLock(path).acquire()
    with pytest.raises(AlreadyRunning):
        RunLock(path, stale_seconds=3600).acquire()


def test_a_held_lock_makes_the_tick_skip_not_fail(monkeypatch, tmp_path, caplog):
    path = str(tmp_path / "acct.lock")
    RunLock(path).acquire()
    monkeypatch.setattr(accounting_sync, "run_once",
                        lambda env: pytest.fail("ran while locked"))
    with caplog.at_level(logging.INFO):
        code = run_forever({**BASE_ENV, "ACCOUNTING_LOCK_PATH": path,
                            "ACCOUNTING_RUN_ONCE": "1"}, sleeper=lambda s: None)
    assert code == 0
    assert "skipping this tick" in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# the loop survives itself
# --------------------------------------------------------------------------- #

def test_a_failing_pass_is_retried_rather_than_ending_the_loop(monkeypatch, tmp_path, caplog):
    attempts = []

    def flaky(env):
        attempts.append(1)
        if len(attempts) < 3:
            raise accounting_sync.AccountingError("Binance GET /fapi/v1/userTrades -> HTTP 503")
        return 0

    monkeypatch.setattr(accounting_sync, "run_once", flaky)
    stopper = Stopper()

    def sleeper(_):
        # Stop after the second sleep: the flag is read at the top of the next
        # iteration, so the third pass — the one that succeeds — still runs.
        if len(attempts) >= 2:
            stopper.stop = True

    with caplog.at_level(logging.ERROR):
        code = run_forever({**BASE_ENV, "ACCOUNTING_LOCK_PATH": str(tmp_path / "l")},
                           sleeper=sleeper, stopper=stopper)
    assert code == 0
    # Two failures did not end the loop, and the third pass still ran.
    assert len(attempts) == 3
    assert "retrying at the next interval" in "\n".join(r.getMessage() for r in caplog.records)


def test_an_unfetchable_roster_does_not_end_the_loop(monkeypatch, tmp_path):
    attempts = []

    def unavailable(env):
        attempts.append(1)
        raise accounting_sync.RosterUnavailable("roster endpoint returned HTTP 500")

    monkeypatch.setattr(accounting_sync, "run_once", unavailable)
    stopper = Stopper()
    run_forever({**BASE_ENV, "ACCOUNTING_LOCK_PATH": str(tmp_path / "l")},
                sleeper=lambda _: setattr(stopper, "stop", True), stopper=stopper)
    # It tried again after the roster failure instead of exiting on it.
    assert len(attempts) == 2


def test_run_once_env_makes_a_single_pass(monkeypatch, tmp_path):
    attempts = []
    monkeypatch.setattr(accounting_sync, "run_once", lambda env: attempts.append(1))
    run_forever({**BASE_ENV, "ACCOUNTING_RUN_ONCE": "1",
                 "ACCOUNTING_LOCK_PATH": str(tmp_path / "l")},
                sleeper=lambda s: pytest.fail("run-once must not sleep"))
    assert len(attempts) == 1


def test_the_interval_defaults_to_five_minutes():
    assert resolve_interval({}) == 300
    assert resolve_interval({"ACCOUNTING_INTERVAL_SECONDS": "600"}) == 600


def test_the_interval_has_a_floor_so_a_typo_cannot_hammer_binance():
    assert resolve_interval({"ACCOUNTING_INTERVAL_SECONDS": "1"}) == 60
    assert resolve_interval({"ACCOUNTING_INTERVAL_SECONDS": "0"}) == 60
    assert resolve_interval({"ACCOUNTING_INTERVAL_SECONDS": "-5"}) == 60


def test_an_unreadable_interval_falls_back_rather_than_crashing():
    assert resolve_interval({"ACCOUNTING_INTERVAL_SECONDS": "five minutes"}) == 300


def test_the_loop_logs_no_credential(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(accounting_sync, "run_once", lambda env: 0)
    with caplog.at_level(logging.DEBUG):
        run_forever({**BASE_ENV, "ACCOUNTING_RUN_ONCE": "1",
                     "ACCOUNTING_LOCK_PATH": str(tmp_path / "l")}, sleeper=lambda s: None)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "svc-token" not in blob
    assert "cred-token" not in blob


# --------------------------------------------------------------------------- #
# a run that could not start is not a run that found nothing
# --------------------------------------------------------------------------- #

def test_a_missing_token_is_refused_before_anything_is_read():
    with pytest.raises(accounting_sync.AccountingError, match="required"):
        accounting_sync.run_once({"APP_API_BASE": "http://app"}, argv=[])


def test_an_unfetchable_roster_writes_nothing(monkeypatch):
    def unavailable(*a, **k):
        raise accounting_sync.RosterUnavailable("roster endpoint returned HTTP 500")

    monkeypatch.setattr(accounting_sync, "resolve_users", unavailable)
    monkeypatch.setattr(accounting_sync.requests, "post",
                        lambda *a, **k: pytest.fail("wrote despite an unknown roster"))
    with pytest.raises(accounting_sync.RosterUnavailable):
        accounting_sync.run_once(BASE_ENV, argv=[])
