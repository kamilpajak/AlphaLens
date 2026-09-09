"""Tests for ``status_snapshot`` — the read-only operator snapshot (#1378).

The snapshot must report the numbers the DAEMON'S GATES will use, so it calls
the daemon's own folds with the candidate set to zero rather than recomputing
anything. The tests below therefore do two different jobs:

* pin the snapshot's own contract (sections, fail-closed content, absent
  sources);
* pin PARITY with the real gates by BOUNDARY PROBE — a candidate sized exactly
  to the reported headroom must pass ``_check_gross_cap``, one epsilon larger
  must be refused, and every state the snapshot reports as ``blocked`` must be
  a state the gate refuses. Parsing the refusal string would break on
  formatting; the probe pins the arithmetic semantically.

Hermetic: no broker, no systemctl, no network. Journals and textfiles are
written into a temporary home.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from alphalens_pipeline.brokers.automanager import entry_trails, safety, status_snapshot
from broker_contract.contract import AccountSnapshot, InstrumentRef, Position

_NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)
_ACCOUNT_CCY = "PLN"


def _account(total: float = 100_000.0, margin: float | None = 50_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        account_id="ACCT",
        currency=_ACCOUNT_CCY,
        cash=40_000.0,
        total_value=total,
        margin_available=margin,
        asof=_NOW,
    )


def _instrument(ticker: str = "KO", uic: int = 211, currency: str = _ACCOUNT_CCY) -> InstrumentRef:
    return InstrumentRef(
        ticker=ticker,
        exchange_mic="XNYS",
        asset_type="Stock",
        broker_instrument_id=str(uic),
        broker_symbol=f"{ticker}:xnys",
        currency=currency,
    )


def _position(
    ticker: str = "KO",
    uic: int = 211,
    *,
    quantity: float = 10.0,
    market_value: float | None = 1_000.0,
    currency: str = _ACCOUNT_CCY,
) -> Position:
    return Position(
        instrument=_instrument(ticker, uic, currency),
        quantity=quantity,
        avg_price=100.0,
        market_value=market_value,
        unrealized_pnl=0.0,
        position_id=f"P-{uic}",
    )


@dataclass
class _FakeOrder:
    order_id: str
    status: Any
    instrument: InstrumentRef | None
    filled_quantity: float
    raw_status: str
    uic: int | None = None
    side: str | None = None
    order_type: str | None = None
    amount: float | None = None
    external_reference: str | None = None
    order_relation: str | None = None


class _FakeBroker:
    """Counts every call so the cost contract can be asserted."""

    def __init__(
        self,
        *,
        account: AccountSnapshot | None = None,
        positions: list[Position] | None = None,
        orders: list[Any] | None = None,
    ) -> None:
        self.account = account or _account()
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def get_account(self) -> AccountSnapshot:
        self._count("get_account")
        return self.account

    def get_positions(self) -> list[Position]:
        self._count("get_positions")
        return list(self.positions)

    def list_open_orders(self) -> list[Any]:
        self._count("list_open_orders")
        return list(self.orders)

    def resolve_order_outcome(self, order_id: str) -> Any:  # pragma: no cover - must not run
        self._count("resolve_order_outcome")
        raise AssertionError("status must never reach the audit path")


def _submission(order_id: str, *, ticker: str = "KO", entry: float = 100.0, qty: float = 5.0):
    return {
        "ts": "2026-09-08T10:00:00+00:00",
        "trade_date": "2026-09-08",
        "ticker": ticker,
        "mic": "XNYS",
        "fx_rate": None,
        "brackets": [
            {
                "client_request_id": f"crid-{order_id}",
                "entry_order_id": order_id,
                "entry": entry,
                "qty": qty,
                "ttl": 7,
            }
        ],
    }


def _working_order(order_id: str) -> _FakeOrder:
    from broker_contract.contract import OrderStatus

    return _FakeOrder(
        order_id=order_id,
        status=OrderStatus.WORKING,
        instrument=_instrument(),
        filled_quantity=0.0,
        raw_status="Working",
        uic=211,
        side="BUY",
        order_type="Limit",
        amount=5.0,
        external_reference="KO-2026-09-08-entry-t0",
        order_relation="StandAlone",
    )


def _watch_line(crid: str, *, limit: float, qty: float, pick_key: str, uic: int = 999) -> str:
    return json.dumps(
        {
            "kind": "watch_open",
            "crid": crid,
            "pick_key": pick_key,
            "ticker": "ENPH",
            "tier_index": 0,
            "uic": uic,
            "exchange_mic": "XNYS",
            "instrument_currency": _ACCOUNT_CCY,
            "window_end": "2026-09-17T20:00:00+00:00",
            "limit": limit,
            "qty": qty,
            "fx_rate": None,
        }
    )


class _SnapshotHarness:
    """A temporary home plus the inputs ``build_snapshot`` reads."""

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        case.addCleanup(patcher.stop)
        self.env = "sim"
        self.root = self.home / ".alphalens" / "broker_orders" / self.env
        self.root.mkdir(parents=True, exist_ok=True)
        self.textfile_dir = self.home / "textfile"
        self.textfile_dir.mkdir(parents=True, exist_ok=True)
        env_patch = mock.patch.dict(
            "os.environ",
            {
                "ALPHALENS_TEXTFILE_DIR": str(self.textfile_dir),
                safety.MAX_OPEN_ENV: "10",
                safety.PORTFOLIO_GROSS_FRAC_ENV: "1.0",
            },
            clear=False,
        )
        env_patch.start()
        case.addCleanup(env_patch.stop)

    def write_watches(self, *lines: str) -> None:
        (self.root / "entry_trails.jsonl").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )

    def write_heartbeat(self, *, age_s: float, kill_active: int = 0) -> None:
        job = f"broker-manager-{self.env}"
        stamp = _NOW.timestamp() - age_s
        (self.textfile_dir / f"alphalens_domain_{job}.prom").write_text(
            f'alphalens_broker_manager_last_tick_timestamp_seconds{{job="{job}"}} {stamp}\n'
            f'alphalens_broker_manager_kill_active{{job="{job}"}} {kill_active}\n',
            encoding="utf-8",
        )

    def build(self, broker: _FakeBroker, **kwargs: Any):
        return status_snapshot.build_snapshot(broker=broker, env=self.env, now=_NOW, **kwargs)


class TestExposureAndSlots(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def test_empty_book_is_all_zeroes_with_the_configured_limits(self) -> None:
        snapshot = self.h.build(_FakeBroker())
        self.assertEqual(snapshot.exposure.used, 0.0)
        self.assertEqual(snapshot.exposure.limit, 100_000.0)
        self.assertEqual(snapshot.exposure.headroom, 100_000.0)
        self.assertEqual(snapshot.slots.used, 0)
        self.assertEqual(snapshot.slots.limit, 10)
        self.assertEqual(snapshot.slots.free, 10)
        self.assertEqual(snapshot.exposure.blocked, [])

    def test_every_exposure_term_is_folded(self) -> None:
        (self.h.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1", entry=100.0, qty=5.0)) + "\n", encoding="utf-8"
        )
        self.h.write_watches(
            _watch_line("ENPH-2026-09-08-entry-t0", limit=30.0, qty=4.0, pick_key="ENPH:2026-09-08")
        )
        broker = _FakeBroker(
            positions=[_position(market_value=2_000.0)], orders=[_working_order("O-1")]
        )
        snapshot = self.h.build(broker)
        self.assertEqual(snapshot.exposure.committed, 500.0)  # 100 x 5
        self.assertEqual(snapshot.exposure.filled, 2_000.0)
        self.assertEqual(snapshot.exposure.watching, 120.0)  # 30 x 4
        self.assertEqual(snapshot.exposure.used, 2_620.0)
        self.assertEqual(snapshot.exposure.headroom, 100_000.0 - 2_620.0)

    def test_slots_count_brackets_positions_and_watch_picks(self) -> None:
        (self.h.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1")) + "\n", encoding="utf-8"
        )
        self.h.write_watches(
            _watch_line("ENPH-2026-09-08-entry-t0", limit=30.0, qty=4.0, pick_key="ENPH:2026-09-08")
        )
        broker = _FakeBroker(positions=[_position()], orders=[_working_order("O-1")])
        snapshot = self.h.build(broker)
        self.assertEqual(snapshot.slots.brackets, 1)
        self.assertEqual(snapshot.slots.positions, 1)
        self.assertEqual(snapshot.slots.watch_picks, 1)
        self.assertEqual(snapshot.slots.used, 3)
        self.assertEqual(snapshot.slots.free, 7)

    def test_a_watch_on_an_open_position_uic_does_not_double_count(self) -> None:
        # The daemon excludes a watch whose uic already holds a position; the
        # snapshot must exclude it too or it would report a phantom slot.
        self.h.write_watches(
            _watch_line(
                "KO-2026-09-08-entry-t1", limit=10.0, qty=1.0, pick_key="KO:2026-09-08", uic=211
            )
        )
        snapshot = self.h.build(_FakeBroker(positions=[_position(uic=211)]))
        self.assertEqual(snapshot.slots.watch_picks, 0)
        self.assertEqual(snapshot.slots.used, 1)

    def test_a_net_flat_round_trip_occupies_no_slot(self) -> None:
        broker = _FakeBroker(
            positions=[_position(uic=211, quantity=5.0), _position(uic=211, quantity=-5.0)]
        )
        self.assertEqual(self.h.build(broker).slots.positions, 0)

    def test_an_unstamped_currency_position_is_flagged_and_headroom_is_an_upper_bound(self) -> None:
        broker = _FakeBroker(positions=[_position(currency="", market_value=3_000.0)])
        snapshot = self.h.build(broker)
        self.assertEqual(snapshot.exposure.filled, 3_000.0)  # folded RAW, no conversion
        self.assertEqual(snapshot.exposure.unstamped_positions, 1)
        self.assertTrue(snapshot.exposure.headroom_is_upper_bound)

    def test_a_position_without_a_mark_blocks_the_exposure_section_with_null_numbers(self) -> None:
        broker = _FakeBroker(positions=[_position(market_value=None)])
        snapshot = self.h.build(broker)
        self.assertIsNone(snapshot.exposure.used)
        self.assertIsNone(snapshot.exposure.headroom)
        self.assertTrue(snapshot.exposure.blocked)
        self.assertIn("market_value", " ".join(snapshot.exposure.blocked))

    def test_an_unvaluable_watch_tier_blocks_the_exposure_section(self) -> None:
        self.h.write_watches(json.dumps({"kind": "touched", "crid": "orphan-entry-t0"}))
        snapshot = self.h.build(_FakeBroker())
        self.assertIsNone(snapshot.exposure.used)
        self.assertTrue(snapshot.exposure.blocked)


class TestCashFloor(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def _build(self, mode: str, **kwargs: Any):
        from alphalens_pipeline.brokers.automanager.live_rails import SIZING_EQUITY_MODE_ENV

        with mock.patch.dict("os.environ", {SIZING_EQUITY_MODE_ENV: mode}):
            return self.h.build(_FakeBroker(**kwargs))

    def test_outside_declared_mode_the_floor_does_not_apply(self) -> None:
        cash_floor = self._build("clamped").cash_floor
        self.assertFalse(cash_floor.applies)
        self.assertIsNone(cash_floor.headroom)

    def test_declared_mode_reserves_committed_plus_watching_against_margin(self) -> None:
        (self.h.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1", entry=100.0, qty=5.0)) + "\n", encoding="utf-8"
        )
        self.h.write_watches(
            _watch_line("ENPH-2026-09-08-entry-t0", limit=30.0, qty=4.0, pick_key="ENPH:2026-09-08")
        )
        cash_floor = self._build("declared", orders=[_working_order("O-1")]).cash_floor
        self.assertTrue(cash_floor.applies)
        self.assertEqual(cash_floor.reserved, 620.0)
        self.assertEqual(cash_floor.available, 50_000.0)
        self.assertEqual(cash_floor.headroom, 50_000.0 - 620.0)

    def test_a_missing_margin_figure_blocks_the_section(self) -> None:
        cash_floor = self._build("declared", account=_account(margin=None)).cash_floor
        self.assertTrue(cash_floor.applies)
        self.assertIsNone(cash_floor.headroom)
        self.assertTrue(cash_floor.blocked)


class TestCostContract(unittest.TestCase):
    """The command runs during an incident: no audit fan-out, ever."""

    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def test_a_closed_bracket_never_reaches_the_audit_path(self) -> None:
        # O-2 is journaled but NOT in the open-orders view: the unfiltered
        # reconcile would resolve it through the /cs audit bucket that has
        # twice tripped 429s. The filtered records must drop it instead.
        (self.h.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1")) + "\n" + json.dumps(_submission("O-2")) + "\n",
            encoding="utf-8",
        )
        broker = _FakeBroker(orders=[_working_order("O-1")])
        snapshot = self.h.build(broker)
        self.assertEqual(broker.calls.get("resolve_order_outcome", 0), 0)
        self.assertEqual(snapshot.slots.brackets, 1)

    def test_an_empty_order_id_never_admits_an_id_less_bracket(self) -> None:
        # Positive control for the filter: a broker row with an empty order_id
        # would otherwise put "" in the open set and admit every bracket that
        # carries no entry_order_id, sending it to the audit path as "open".
        from alphalens_pipeline.brokers.automanager.status_snapshot import _filter_open_records

        self.assertEqual(_filter_open_records([{"brackets": [{"qty": 1}]}], {""}), [])
        self.assertEqual(
            _filter_open_records([{"brackets": [{"entry_order_id": ""}]}], {"", "O-1"}), []
        )

    def test_only_the_open_bracket_of_a_multi_bracket_record_survives(self) -> None:
        from alphalens_pipeline.brokers.automanager.status_snapshot import _filter_open_records

        record = {
            "ticker": "KO",
            "brackets": [{"entry_order_id": "O-1"}, {"entry_order_id": "O-2"}],
        }
        self.assertEqual(
            _filter_open_records([record], {"O-1"}),
            [{"ticker": "KO", "brackets": [{"entry_order_id": "O-1"}]}],
        )

    def test_the_broker_is_read_a_bounded_number_of_times(self) -> None:
        broker = _FakeBroker(orders=[_working_order("O-1")])
        self.h.build(broker)
        self.assertEqual(broker.calls.get("get_account"), 1)
        self.assertEqual(broker.calls.get("get_positions"), 1)
        self.assertLessEqual(broker.calls.get("list_open_orders", 0), 2)

    def test_offline_touches_no_broker_at_all(self) -> None:
        broker = _FakeBroker()
        snapshot = self.h.build(broker, offline=True)
        self.assertEqual(broker.calls, {})
        self.assertTrue(snapshot.offline)
        self.assertIsNone(snapshot.account)
        # The offline half is exactly what a broker outage needs.
        self.assertIsNotNone(snapshot.health)


class TestContainmentAndSkew(unittest.TestCase):
    """Two promises that had no test until the pre-merge review said so."""

    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def test_a_fold_that_raises_degrades_the_section_not_the_command(self) -> None:
        # "Fail-closed is CONTENT, not an exception" must hold for an
        # UNEXPECTED error too — a malformed record mid-incident cannot be
        # allowed to abort the snapshot the operator is reading.
        from alphalens_pipeline.brokers.automanager import control_loop

        with mock.patch.object(
            control_loop, "_committed_working_gross_acct", side_effect=KeyError("shape drift")
        ):
            snapshot = self.h.build(_FakeBroker())
        self.assertIsNone(snapshot.exposure.used)
        self.assertTrue(snapshot.exposure.blocked)
        self.assertIn("shape drift", " ".join(snapshot.exposure.blocked))
        # The rest of the snapshot still renders.
        self.assertIsNotNone(snapshot.health)

    def test_a_journal_written_during_the_broker_reads_is_flagged_as_skewed(self) -> None:
        journal = self.h.root / "submissions.jsonl"
        journal.write_text(json.dumps(_submission("O-1")) + "\n", encoding="utf-8")
        broker = _FakeBroker(orders=[_working_order("O-1")])
        original = broker.get_positions

        def touch_then_read():
            # The daemon appends between the snapshot's two mtime reads.
            journal.write_text(
                json.dumps(_submission("O-1")) + "\n" + json.dumps(_submission("O-2")) + "\n",
                encoding="utf-8",
            )
            return original()

        broker.get_positions = touch_then_read
        snapshot = self.h.build(broker)
        self.assertIn("submissions", snapshot.skewed)

    def test_an_untouched_journal_is_not_flagged(self) -> None:
        (self.h.root / "submissions.jsonl").write_text(
            json.dumps(_submission("O-1")) + "\n", encoding="utf-8"
        )
        snapshot = self.h.build(_FakeBroker(orders=[_working_order("O-1")]))
        self.assertEqual(snapshot.skewed, [])


class TestHealth(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def test_absent_sources_read_absent_never_raise(self) -> None:
        health = self.h.build(_FakeBroker()).health
        self.assertIsNone(health.heartbeat_age_s)
        self.assertFalse(health.kill_instance)
        self.assertFalse(health.kill_global)

    def test_heartbeat_age_is_computed_from_the_gauge(self) -> None:
        self.h.write_heartbeat(age_s=42.0)
        self.assertAlmostEqual(self.h.build(_FakeBroker()).health.heartbeat_age_s, 42.0, places=0)

    def test_the_kill_file_is_truth_and_the_gauge_is_the_daemons_last_view(self) -> None:
        # A KILL touched since the daemon's last tick: the file says stopped,
        # the daemon's own gauge still says 0. Reporting only one would lie.
        (self.h.root / "KILL").write_text("", encoding="utf-8")
        self.h.write_heartbeat(age_s=10.0, kill_active=0)
        health = self.h.build(_FakeBroker()).health
        self.assertTrue(health.kill_instance)
        self.assertEqual(health.kill_active_gauge, 0.0)

    def test_the_last_refusal_comes_from_the_pick_journal(self) -> None:
        from alphalens_pipeline.brokers.automanager.picks import mark_refused

        picks_path = self.h.root / "picks.jsonl"
        mark_refused("KO", dt.date(2026, 9, 8), reason="gross cap", path=picks_path)
        health = self.h.build(_FakeBroker()).health
        self.assertIsNotNone(health.last_refusal)
        self.assertIn("gross cap", health.last_refusal)

    def test_the_refusal_age_comes_from_refused_ts_never_from_the_brief_date(self) -> None:
        # #1385: the rendered date is the BRIEF date. A month-old refusal read
        # as fresh in the first LIVE run, so the age must come from the
        # journal's own `refused_ts`.
        picks_path = self.h.root / "picks.jsonl"
        picks_path.write_text(
            json.dumps(
                {
                    "ticker": "KO",
                    "date": "2026-08-13",
                    "refused_ts": (_NOW - dt.timedelta(days=27)).isoformat(timespec="seconds"),
                    "reason": "gross cap",
                    "status": "refused",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        health = self.h.build(_FakeBroker()).health
        self.assertAlmostEqual(health.last_refusal_age_s, 27 * 86_400, delta=2)

    def test_a_refusal_without_a_timestamp_has_no_age_rather_than_a_guessed_one(self) -> None:
        picks_path = self.h.root / "picks.jsonl"
        picks_path.write_text(
            json.dumps(
                {"ticker": "KO", "date": "2026-08-13", "reason": "gross cap", "status": "refused"}
            )
            + "\n",
            encoding="utf-8",
        )
        health = self.h.build(_FakeBroker()).health
        self.assertIsNotNone(health.last_refusal)
        self.assertIsNone(health.last_refusal_age_s)


class TestGateParity(unittest.TestCase):
    """The headroom the snapshot reports must be the headroom the gate leaves.

    Boundary probe over SEVERAL states: one clean state would pass even if a
    fold's semantics drifted while its name stayed (the name-pinning test below
    cannot see that).
    """

    def setUp(self) -> None:
        self.h = _SnapshotHarness(self)

    def _states(self):
        yield "empty", [], []
        yield "position in account currency", [_position(market_value=2_000.0)], []
        yield (
            "watch open",
            [],
            [_watch_line("E-2026-09-08-entry-t0", limit=30.0, qty=4.0, pick_key="E:2026-09-08")],
        )
        yield (
            "position plus watch",
            [_position(market_value=1_500.0)],
            [_watch_line("E-2026-09-08-entry-t0", limit=10.0, qty=3.0, pick_key="E:2026-09-08")],
        )

    def _plan(self, gross: float):
        @dataclass(frozen=True)
        class _Tier:
            qty: float
            limit_price: float

        @dataclass(frozen=True)
        class _Plan:
            entry_tiers: tuple[_Tier, ...]

        return _Plan(entry_tiers=(_Tier(qty=1.0, limit_price=gross),))

    def test_a_candidate_sized_to_the_headroom_passes_the_real_gate(self) -> None:
        from alphalens_pipeline.brokers.automanager import control_loop

        for label, positions, watches in self._states():
            with self.subTest(state=label):
                self.h.write_watches(*watches)
                broker = _FakeBroker(positions=positions)
                snapshot = self.h.build(broker)
                headroom = snapshot.exposure.headroom
                self.assertIsNotNone(headroom)
                fold = entry_trails.read_entry_trail_fold(path=self.h.root / "entry_trails.jsonl")
                common = {
                    "account": broker.account,
                    "open_verdicts": [],
                    "records": [],
                    "positions": positions,
                    "ticker": "NEW",
                    "entry_trail_fold": fold,
                }
                self.assertIsNone(
                    control_loop._check_gross_cap(self._plan(headroom), None, **common),
                    "a candidate exactly at the reported headroom must be allowed",
                )
                refusal = control_loop._check_gross_cap(self._plan(headroom + 1.0), None, **common)
                self.assertIsNotNone(refusal, "one unit over the headroom must be refused")

    def test_no_free_slot_means_the_real_safety_gate_refuses(self) -> None:
        with mock.patch.dict("os.environ", {safety.MAX_OPEN_ENV: "1"}):
            broker = _FakeBroker(positions=[_position()])
            snapshot = self.h.build(broker)
            self.assertEqual(snapshot.slots.free, 0)
            decision = safety.check(
                object(),
                safety.JournalView(
                    open_bracket_count=snapshot.slots.brackets, realized_r_today=0.0
                ),
                safety.BrokerView(open_position_count=snapshot.slots.positions, equity=100_000.0),
                _AliveSession(),
                kill_path=self.h.root / "KILL",
                global_kill_path=self.h.root / "GLOBAL_KILL",
            )
            self.assertIsInstance(decision, safety.Refuse)

    def test_every_blocked_state_is_a_state_the_gate_also_refuses(self) -> None:
        from alphalens_pipeline.brokers.automanager import control_loop

        # No mark on a position.
        positions = [_position(market_value=None)]
        broker = _FakeBroker(positions=positions)
        snapshot = self.h.build(broker)
        self.assertTrue(snapshot.exposure.blocked)
        self.assertIsNotNone(
            control_loop._check_gross_cap(
                self._plan(1.0),
                None,
                account=broker.account,
                open_verdicts=[],
                records=[],
                positions=positions,
                ticker="NEW",
                entry_trail_fold=entry_trails.EntryTrailFold(tiers={}, malformed=0),
            ),
            "the gate must refuse exactly where the snapshot reports blocked",
        )

        # Unvaluable watch tier.
        self.h.write_watches(json.dumps({"kind": "touched", "crid": "orphan-entry-t0"}))
        snapshot = self.h.build(_FakeBroker())
        self.assertTrue(snapshot.exposure.blocked)
        fold = entry_trails.read_entry_trail_fold(path=self.h.root / "entry_trails.jsonl")
        self.assertIsNotNone(
            control_loop._check_gross_cap(
                self._plan(1.0),
                None,
                account=_account(),
                open_verdicts=[],
                records=[],
                positions=[],
                ticker="NEW",
                entry_trail_fold=fold,
            )
        )


class _AliveSession:
    alive = True


class TestDaemonFoldCoupling(unittest.TestCase):
    def test_the_private_folds_the_snapshot_reuses_still_exist(self) -> None:
        # Necessary but NOT sufficient: a rename breaks CI here, while a
        # semantic drift under the same name is caught only by the boundary
        # probe above. Both tests exist for that reason.
        from alphalens_pipeline.brokers.automanager import control_loop

        for name in (
            "_committed_working_gross_acct",
            "_filled_positions_gross_acct",
            "_make_position_rate_lookup",
            "_net_open_position_uics",
            "_summarize_open_verdicts",
            "_open_watch_picks_for_max_open",
        ):
            with self.subTest(fold=name):
                self.assertTrue(callable(getattr(control_loop, name, None)))


if __name__ == "__main__":
    unittest.main()
