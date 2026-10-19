import copy
import json
import unittest
from datetime import datetime, timezone

from trade_app.datasets import MAX_TIMESTAMP, load_dataset
from trade_app.replay import run_replay


BASE = 1_704_067_200  # Monday 2024-01-01 00:00 UTC


def dataset(quotes, costs=None):
    return load_dataset({"version": 1, "source": "synthetic-test", "timezone": "UTC",
                         "instrument": {"symbol": "TEST", "currency": "USD", "point": 0.01, "tick_size": 0.01,
                                        "contract_size": 1, "volume_min": 0.1, "volume_max": 2,
                                        "volume_step": 0.1, "stops_level": 1, "margin_per_lot": 50},
                         "costs": costs or {"commission_per_lot_side": 0, "fee_per_lot_side": 0,
                                            "slippage_points": 0, "swap_long_per_lot_day": 0,
                                            "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                                            "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
                         "quotes": [{"time": t, "bid": bid, "ask": ask} for t, bid, ask in quotes],
                         "bars": {"M15": [], "H1": []}, "evidence": []})


def proposal(at=BASE+1, side="BUY", kind="MARKET", entry=None, sl=98, tp=104, strategy="s"):
    return {"decision_at": at, "side": side, "kind": kind, "entry": entry,
            "sl": sl, "tp": tp, "lot": 0.1, "strategy": strategy, "trace_id": "trace-x"}


class ReplayTests(unittest.TestCase):
    def test_market_uses_next_quote_and_preserves_spread(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 101, 101.2), (BASE+6, 101.3, 101.4)])
        result = run_replay(ds, [proposal()])
        self.assertEqual(result["accepted"][0]["accepted_at"], BASE+5)
        entry = next(d for d in result["deals"] if d["entry"] == 0)
        self.assertEqual(entry["price"], 101.2)
        self.assertTrue(result["trades"][0]["forced_exit"])
        self.assertEqual(result["trades"][0]["exit"], 101.3)

    def test_sell_entry_uses_bid_and_exit_uses_ask(self):
        ds = dataset([(BASE, 100, 100.2), (BASE+5, 100, 100.3), (BASE+6, 99, 99.1)])
        result = run_replay(ds, [proposal(side="SELL", sl=103, tp=97)])
        self.assertEqual(next(d for d in result["deals"] if d["entry"] == 0)["price"], 100)
        self.assertEqual(result["trades"][0]["exit"], 99.1)

    def test_take_profit_gap_uses_better_observed_price(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 110, 110.1)])
        result = run_replay(ds, [proposal(tp=105)])
        self.assertEqual(result["trades"][0]["reason"], "TP")
        self.assertEqual(result["trades"][0]["exit"], 110)

    def test_stop_gap_fills_from_observed_quote_with_adverse_slippage(self):
        costs = {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 2,
                 "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                 "rollover_weekdays": [0], "triple_swap_weekday": None}
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 103, 103.1),
                      (BASE+7, 105, 105.1)], costs)
        p = proposal(kind="STOP", entry=102, sl=98, tp=110)
        result = run_replay(ds, [p], close_at_end=False)
        entry = next(d for d in result["deals"] if d["entry"] == 0)
        self.assertAlmostEqual(entry["price"], 103.12)
        self.assertEqual(result["orders"][0]["status"], "filled")

    def test_limit_fill_never_exceeds_limit_and_can_improve(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1),
                      (BASE+6, 98, 98.1), (BASE+7, 98, 98.1)])
        result = run_replay(ds, [proposal(kind="LIMIT", entry=99, sl=96, tp=103)])
        entry = next(d for d in result["deals"] if d["entry"] == 0)
        self.assertLessEqual(entry["price"], 99)
        self.assertEqual(entry["price"], 98.1)

    def test_buy_and_sell_limit_fills_are_capped_even_with_slippage_configured(self):
        costs = {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 20,
                 "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                 "rollover_weekdays": [0], "triple_swap_weekday": None}
        buy_ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 98, 98.1)], costs)
        buy = run_replay(buy_ds, [proposal(kind="LIMIT", entry=99, sl=96, tp=103)], close_at_end=False)
        buy_entry = next(d for d in buy["deals"] if d["entry"] == 0)
        self.assertEqual(buy_entry["price"], 98.1)
        self.assertEqual(buy_entry["slippage"], 0)
        sell_ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 102, 102.1)], costs)
        sell = run_replay(sell_ds, [proposal(side="SELL", kind="LIMIT", entry=101, sl=105, tp=98)], close_at_end=False)
        sell_entry = next(d for d in sell["deals"] if d["entry"] == 0)
        self.assertEqual(sell_entry["price"], 102)
        self.assertEqual(sell_entry["slippage"], 0)

    def test_market_slippage_is_in_risk_gate_and_tp_crossing_is_rejected(self):
        costs = {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 20,
                 "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                 "rollover_weekdays": [0], "triple_swap_weekday": None}
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 100, 100.1)], costs)
        result = run_replay(ds, [proposal(tp=104)])
        entry = next(d for d in result["deals"] if d["entry"] == 0)
        self.assertAlmostEqual(entry["price"], 100.3)
        self.assertAlmostEqual(result["accepted"][0]["risk"]["loss"], 0.23)
        self.assertAlmostEqual(entry["quoted_spread"], 10)
        crossing = run_replay(ds, [proposal(tp=100.2)])
        self.assertEqual(crossing["rejected"][0]["stage"], "acceptance")
        self.assertEqual(crossing["deals"], [])

    def test_spread_costs_swap_and_forced_close_reconcile(self):
        costs = {"commission_per_lot_side": 2, "fee_per_lot_side": 1, "slippage_points": 0,
                 "swap_long_per_lot_day": -5, "swap_short_per_lot_day": -7, "rollover_hour_utc": 1,
                 "rollover_weekdays": [0], "triple_swap_weekday": None}
        ds = dataset([(BASE, 100, 100.1), (BASE+100, 100, 100.1), (BASE+7200, 100.2, 100.3)], costs)
        result = run_replay(ds, [proposal(sl=98, tp=105)])
        trade = result["trades"][0]
        self.assertEqual(trade["swap"], -0.5)
        self.assertAlmostEqual(sum(d["commission"] + d["fee"] + d["profit"] + d["swap"] for d in result["deals"]), result["metrics"]["net_pnl"])
        self.assertEqual(result["equity_curve"][-1]["balance"], 10000 + result["metrics"]["net_pnl"])
        self.assertTrue(trade["forced_exit"])
        self.assertAlmostEqual(sum(t["net_pnl"] for t in result["trades"]), result["metrics"]["net_pnl"])
        self.assertAlmostEqual(result["metrics"]["turnover"], 0.1)
        self.assertEqual(result["metrics"]["turnover_unit"], "lots")

    def test_positive_negative_and_triple_swap_reconcile_once(self):
        base_quotes = [(BASE, 100, 100.1), (BASE+100, 100, 100.1),
                       (BASE+90000, 100, 100.1), (BASE+176400, 100, 100.1),
                       (BASE+180000, 100, 100.1)]
        for side, long_swap, short_swap, expected in (("BUY", 2, -7, 1), ("SELL", 7, -2, -1)):
            costs = {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
                     "swap_long_per_lot_day": long_swap, "swap_short_per_lot_day": short_swap,
                     "rollover_hour_utc": 1, "rollover_weekdays": [0, 1, 2], "triple_swap_weekday": 2}
            ds = dataset(base_quotes, costs)
            p = proposal(side=side, sl=98 if side == "BUY" else 103, tp=105 if side == "BUY" else 97)
            result = run_replay(ds, [p])
            accruals = [d for d in result["deals"] if d.get("cost_only")]
            self.assertEqual(len(accruals), 3)
            self.assertTrue(all(d["volume"] == 0 and d["accrual_volume"] == 0.1 for d in accruals))
            self.assertAlmostEqual(sum(d["swap"] for d in accruals), expected)
            self.assertEqual(next(d for d in result["deals"] if d["entry"] == 1)["swap"], 0)
            fields_total = sum(d["profit"]+d["commission"]+d["swap"]+d["fee"] for d in result["deals"])
            self.assertAlmostEqual(fields_total, result["metrics"]["net_pnl"])
            self.assertAlmostEqual(sum(t["net_pnl"] for t in result["trades"]), result["metrics"]["net_pnl"])

    def test_duplicate_no_eligible_quote_open_at_end_and_repeatability(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1)])
        p = proposal()
        first = run_replay(ds, [p, copy.deepcopy(p)], close_at_end=False)
        second = run_replay(ds, [p, copy.deepcopy(p)], close_at_end=False)
        self.assertEqual(first, second)
        self.assertEqual(first["duplicate_count"], 1)
        self.assertEqual(len(first["positions_open"]), 1)
        self.assertEqual(first["metrics"]["closed_trades"], 0)
        late = run_replay(ds, [proposal(at=BASE+5)])
        self.assertEqual(late["rejected"][0]["stage"], "data")
        self.assertEqual(late["rejected"][0]["evaluated_at"], BASE+5)
        self.assertEqual(late["rejected"][0]["decision_at"], BASE+5)
        self.assertEqual(late["rejected"][0]["evaluated_at"], BASE+5)
        self.assertEqual(late["rejected"][0]["decision_at"], BASE+5)

    def test_risk_rejection_and_fill_revalidation(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 96, 96.1),
                      (BASE+7, 96, 96.1)])
        rejected = run_replay(ds, [proposal()], settings={"daily_loss": 0.01})
        self.assertEqual(rejected["rejected"][0]["stage"], "acceptance")
        self.assertEqual(rejected["rejected"][0]["evaluated_at"], BASE+5)
        self.assertIn("risk_snapshot", rejected["rejected"][0])
        self.assertEqual(rejected["rejected"][0]["evaluated_at"], BASE+5)
        self.assertIn("risk_snapshot", rejected["rejected"][0])
        p = proposal(kind="LIMIT", entry=99.5, sl=98, tp=104)
        result = run_replay(ds, [p], close_at_end=False)
        self.assertEqual(result["orders"][0]["status"], "filled")
        self.assertEqual(result["risk_breach_count"], 1)
        self.assertEqual(result["risk_breaches"][0]["stage"], "fill")
        self.assertFalse(result["orders"][0]["fill_revalidation"]["allowed"])
        self.assertEqual(result["orders"][0]["fill_risk_snapshot"]["deals"], [])
        self.assertEqual(result["trades"][0]["reason"], "SL")
        self.assertLess(result["trades"][0]["net_pnl"], 0)

    def test_risk_snapshots_are_point_in_time_copies_and_result_is_json_safe(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+100, 100, 100.1),
                      (BASE+200, 99, 99.1), (BASE+300, 100, 100.1)])
        p1 = proposal(at=BASE+1, strategy="first")
        p2 = proposal(at=BASE+101, strategy="second")
        result = run_replay(ds, [p1, p2], close_at_end=False)
        first, second = result["accepted"]
        self.assertEqual(first["accepted_at"], BASE+100)
        self.assertEqual(first["risk_snapshot"]["deals"], [])
        self.assertEqual(first["risk_snapshot"]["positions"], [])
        saved_price = second["risk_snapshot"]["positions"][0]["price_current"]
        self.assertEqual(saved_price, 99)
        self.assertEqual(second["risk_snapshot"]["deals"], result["deals"][:1])
        json.dumps(result, allow_nan=False)
        self.assertTrue(all(d["order"] in {o["ticket"] for o in result["orders"]} for d in result["deals"]))

    def test_pending_order_reserves_account_order_slot(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+5, 100, 100.1), (BASE+6, 100, 100.1)])
        pending = proposal(kind="LIMIT", entry=99, sl=97, tp=103, strategy="pending")
        market = proposal(strategy="market")
        result = run_replay(ds, [pending, market], settings={"max_orders": 1}, close_at_end=False)
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["rejected"][0]["risk"]["errors"][0], "จำนวน Position + Pending ทั้งบัญชีเต็มแล้ว")

    def test_latency_and_timestamp_ordering_are_deterministic(self):
        ds = dataset([(BASE, 100, 100.1), (BASE+4, 100, 100.1), (BASE+10, 100, 100.1)])
        result = run_replay(ds, [proposal()], latency_seconds=5)
        self.assertEqual(result["accepted"][0]["accepted_at"], BASE+10)

    def test_max_timestamp_does_not_overflow_rollover_day_iteration(self):
        ds = dataset([(MAX_TIMESTAMP-86400, 100, 100.1),
                      (MAX_TIMESTAMP-86390, 100, 100.1),
                      (MAX_TIMESTAMP, 100, 100.1)], costs={"commission_per_lot_side": 0,
                      "fee_per_lot_side": 0, "slippage_points": 0, "swap_long_per_lot_day": 0,
                      "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                      "rollover_weekdays": [], "triple_swap_weekday": None})
        result = run_replay(ds, [proposal(at=MAX_TIMESTAMP-86399)])
        self.assertEqual(result["trades"][0]["exit_time"], MAX_TIMESTAMP)
        json.dumps(result, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
