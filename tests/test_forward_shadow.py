import threading
import time
import unittest
from unittest.mock import patch

from trade_app.forward_shadow import ForwardShadow


BASE = 1_700_000_000
COSTS = {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
         "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
         "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None}
SETTINGS = {"max_lot": .1, "risk_percent": 1, "daily_loss": 5, "max_orders": 2,
            "max_spread": 100, "enabled_strategies": ["scalp"], "kill_switch": False}


def context(observed, bar_open, source_time=None, signal=False):
    bars = [{"time": BASE + i * 900, "open": 100, "high": 102, "low": 99, "close": 101}
            for i in range(80)]
    bars.append({"time": bar_open, "open": 100, "high": 102, "low": 99, "close": 101})
    tick = {"bid": 100, "ask": 100.1, "time": source_time or observed, "time_msc": (source_time or observed) * 1000}
    return {"connected": True, "scope": "demo-scope", "mode": "simulation", "symbol": "TEST",
        "observed_at": observed, "account": {"currency": "USD", "is_demo": True},
        "info": {"currency_profit": "USD", "point": .01, "trade_tick_size": .01,
            "trade_contract_size": 1, "volume_min": .1, "volume_max": 2, "volume_step": .1,
            "trade_stops_level": 1}, "tick": tick, "bars": bars, "h1": [],
        "signals": ([{"key": "scalp", "time": bar_open, "signal": "BUY", "sl": 99, "tp": 102}]
                    if signal else []), "settings": SETTINGS}


class ForwardShadowTests(unittest.TestCase):
    def setUp(self):
        self.now = [BASE + 81_800]
        self.shadow = ForwardShadow(clock=lambda: self.now[0])
        self.shadow.start({"strategy": "scalp", "margin_per_lot": 50, "costs": COSTS,
            "settings": SETTINGS, "provider": "openai", "model": "test", "api_key": "secret",
            "client": object(), "calendar_before_minutes": 0, "calendar_after_minutes": 0,
            "initial_balance": 10000, "latency_seconds": 0, "horizon_seconds": 900,
            "max_decisions": 20}, context(BASE + 80_000, BASE + 80_000))

    def tearDown(self):
        self.shadow.close()

    def test_observe_keeps_polling_while_provider_is_blocked_and_retains_new_quote(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"analyst_proposal": {"action": "BUY"}, "challenger": {"approve": True}}
        with patch("trade_app.advisor.generate_advice", side_effect=blocked):
            first = context(BASE + 81_800, BASE + 80_900, signal=True)
            self.shadow.observe(first)
            self.assertTrue(entered.wait(2))
            second = context(BASE + 81_801, BASE + 80_900)
            self.shadow.observe(second)
            self.assertEqual(self.shadow.status()["quotes"], 2)
            release.set()

    def test_stale_source_tick_is_not_recorded_with_fresh_availability_time(self):
        stale = context(BASE + 81_800, BASE + 80_900, source_time=BASE + 81_619)
        self.shadow.observe(stale)
        self.assertEqual(self.shadow.status()["quotes"], 0)

    def test_buy_is_risk_checked_and_resolves_only_after_fresh_horizon_quotes(self):
        with self.shadow._lock:
            self.shadow._config["provider"] = "disabled"
        self.shadow.observe(context(BASE + 81_800, BASE + 80_900, signal=True))
        for _ in range(100):
            with self.shadow._lock:
                decision = next(iter(self.shadow._decisions), None)
                status = decision.get("status") if decision else None
            if status == "unresolved":
                break
            time.sleep(.01)
        self.assertEqual(status, "unresolved")
        for timestamp in range(BASE + 81_860, BASE + 82_820, 60):
            self.shadow.observe(context(timestamp, BASE + 80_900, source_time=timestamp))
        self.shadow.observe(context(BASE + 82_700, BASE + 80_900, source_time=BASE + 82_700))
        for _ in range(100):
            with self.shadow._lock:
                status = self.shadow._decisions[0]["status"]
            if status != "unresolved":
                break
            time.sleep(.01)
        self.assertEqual(status, "completed")
        with self.shadow._lock:
            result = self.shadow._decisions[0]
            self.assertEqual(result["side"], "BUY")
            self.assertGreater(result["lot"], 0)
            self.assertEqual(result["actual_fills"], False)
            self.assertIn("net_pnl", result)

    def test_changed_settings_stops_run_and_clears_provider_key(self):
        changed = context(BASE + 81_800, BASE + 80_900)
        changed["settings"] = dict(SETTINGS, max_spread=99)
        self.shadow.observe(changed)
        status = self.shadow.status()
        self.assertEqual(status["status"], "stopped")
        with self.shadow._lock:
            self.assertIsNone(self.shadow._config["api_key"])

    def test_stale_tick_after_provider_wait_is_rejected_without_another_poll(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"analyst_proposal": {"action": "BUY"}, "challenger": {"approve": True}}
        with patch("trade_app.advisor.generate_advice", side_effect=blocked):
            self.shadow.observe(context(BASE + 81_800, BASE + 80_900,
                                        source_time=BASE + 81_700, signal=True))
            self.assertTrue(entered.wait(2))
            self.now[0] = BASE + 81_900
            release.set()
            self._wait_for_decision()
        with self.shadow._lock:
            decision = self.shadow._decisions[0]
            self.assertEqual(decision["status"], "rejected")
            self.assertEqual(decision["reason"], "Scope changed or latest source tick is stale")

    def test_decision_limit_keeps_pending_candidate_quotes_until_resolution_then_restarts(self):
        entered, release = threading.Event(), threading.Event()
        with self.shadow._lock:
            self.shadow._config["max_decisions"] = 1
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"analyst_proposal": {"action": "BUY"}, "challenger": {"approve": True}}
        with patch("trade_app.advisor.generate_advice", side_effect=blocked):
            self.shadow.observe(context(BASE + 81_800, BASE + 80_900, signal=True))
            self.assertTrue(entered.wait(2))
            self.shadow.observe(context(BASE + 81_801, BASE + 80_900))
            self.assertEqual(self.shadow.status()["status"], "decision_limit")
            self.assertTrue(self.shadow.status()["capturing_quotes"])
            self.now[0] = BASE + 81_801
            release.set()
            self._wait_for_decision("unresolved")
        for timestamp in range(BASE + 81_860, BASE + 82_820, 60):
            self.shadow.observe(context(timestamp, BASE + 80_900, source_time=timestamp))
        self._wait_for_decision("completed")
        self.assertFalse(self.shadow.status()["capturing_quotes"])
        restarted = self.shadow.start({"strategy": "scalp", "margin_per_lot": 50, "costs": COSTS,
            "settings": SETTINGS, "provider": "disabled", "model": None, "api_key": None,
            "calendar_before_minutes": 0, "calendar_after_minutes": 0, "initial_balance": 10000,
            "latency_seconds": 0, "horizon_seconds": 900, "max_decisions": 1},
            context(BASE + 82_800, BASE + 80_900))
        self.assertEqual(restarted["status"], "running")

    def test_cap_during_provider_call_cancels_late_acceptance(self):
        from trade_app import forward_shadow as module
        entered, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"analyst_proposal": {"action": "BUY"}, "challenger": {"approve": True}}
        with patch("trade_app.advisor.generate_advice", side_effect=blocked):
            self.shadow.observe(context(BASE + 81_800, BASE + 80_900, signal=True))
            self.assertTrue(entered.wait(2))
            with patch.object(module, "MAX_QUOTES", 1):
                self.shadow.observe(context(BASE + 81_801, BASE + 80_900))
            release.set()
            self._wait_for_decision("unknown")
        with self.shadow._lock:
            self.assertEqual(self.shadow._status, "limit_reached")
            self.assertEqual(self.shadow._decisions[0]["status"], "unknown")
            self.assertIsNone(self.shadow._config["api_key"])

    def _wait_for_decision(self, expected=None):
        for _ in range(200):
            with self.shadow._lock:
                row = dict(self.shadow._decisions[0]) if self.shadow._decisions else None
                status = row["status"] if row else None
            if status is not None and (expected is None and status != "pending_advice" or status == expected):
                return status
            time.sleep(.01)
        with self.shadow._lock:
            quote_times = [quote["time"] for quote in self.shadow._quotes]
        self.fail(f"forward decision did not reach {expected or 'a terminal status'}: {row}; quotes={quote_times}")

    def test_cap_failure_marks_unresolved_decision_unknown(self):
        with self.shadow._lock:
            self.shadow._decisions.append({"id": "pending", "status": "unresolved"})
        with patch("trade_app.forward_shadow.MAX_SESSION_BYTES", 0):
            self.shadow._check_caps()
        with self.shadow._lock:
            self.assertEqual(self.shadow._decisions[0]["status"], "unknown")
            self.assertIsNone(self.shadow._config["api_key"])


if __name__ == "__main__":
    unittest.main()
