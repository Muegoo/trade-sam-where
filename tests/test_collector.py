import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trade_app.broker import BrokerError, MT5Broker
from trade_app.collector import collect_mt5_dataset
from trade_app.datasets import DatasetError


UTC = timezone.utc
START = 1_700_000_000
COSTS = {"commission_per_lot_side": 3.5, "fee_per_lot_side": 0.0, "slippage_points": 1,
         "swap_long_per_lot_day": -2, "swap_short_per_lot_day": 1, "rollover_hour_utc": 21,
         "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": 2}
INFO = {"currency_profit": "USD", "point": 0.01, "trade_tick_size": 0.01,
        "trade_contract_size": 100, "volume_min": 0.01, "volume_max": 10,
        "volume_step": 0.01, "trade_stops_level": 10}


def history():
    return {"symbol": "XAUUSD", "currency": "USD", "info": dict(INFO),
            "requested_range": {"start": START, "end": START + 120}, "fetched_at": START + 120,
            "ticks": [{"time_msc": (START + 1) * 1000 + 50, "bid": 2000, "ask": 2000.2},
                      {"time_msc": (START + 1) * 1000 + 900, "bid": 2001, "ask": 2001.2},
                      {"time_msc": (START + 5) * 1000, "bid": 2002, "ask": 2002.2}],
            "bars": {"M15": [{"time": START - 900, "open": 10, "high": 12, "low": 9, "close": 11},
                              {"time": START + 100, "open": 10, "high": 12, "low": 9, "close": 11}],
                     "H1": [{"time": START - 3600, "open": 10, "high": 12, "low": 9, "close": 11}]}}


class Row(dict):
    def __init__(self, values, names):
        super().__init__(values)
        self.dtype = SimpleNamespace(names=names)


class Batch(list):
    def __init__(self, names, rows=()):
        super().__init__(Row(row, names) for row in rows)
        self.dtype = SimpleNamespace(names=names)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    COPY_TICKS_INFO = 1
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60

    def __init__(self):
        self.calls = []
        self.account = SimpleNamespace(trade_mode=0, currency="USD")
        self.terminal = SimpleNamespace(connected=True)
        self.info = SimpleNamespace(**INFO, _asdict=lambda: dict(INFO))

    def account_info(self): self.calls.append("account_info"); return self.account
    def terminal_info(self): self.calls.append("terminal_info"); return self.terminal
    def symbol_select(self, symbol, selected): self.calls.append("symbol_select"); return True
    def symbol_info(self, symbol): self.calls.append("symbol_info"); return self.info
    def symbol_info_tick(self, symbol): return SimpleNamespace(ask=2000.2, bid=2000,
        _asdict=lambda: {"ask": 2000.2, "bid": 2000})
    def order_calc_profit(self, order_type, symbol, lot, entry, exit_price):
        self.calls.append("order_calc_profit")
        return ((exit_price - entry) if order_type == self.ORDER_TYPE_BUY else (entry - exit_price)) * 100 * lot
    def copy_ticks_range(self, symbol, start, end, flags):
        self.calls.append(("ticks", start, end))
        return Batch(("time", "time_msc", "bid", "ask", "flags"))
    def copy_rates_range(self, symbol, timeframe, start, end):
        self.calls.append(("rates", timeframe, start, end))
        return Batch(("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"))
    def last_error(self): return "fake error"


class CollectorTests(unittest.TestCase):
    def test_quotes_round_up_and_keep_last_observation_per_second_without_lookahead(self):
        result = collect_mt5_dataset(history(), COSTS, 1000, gap_threshold_seconds=1)
        data = result["dataset"]
        self.assertEqual([q["time"] for q in data["quotes"]], [START + 2, START + 5])
        self.assertEqual(data["quotes"][0]["bid"], 2001)
        self.assertEqual(data["bars"]["M15"][0]["available_at"], START)
        self.assertEqual(len(data["bars"]["M15"]), 1)
        self.assertTrue(data["source"].startswith("mt5_historical;"))
        self.assertEqual(result["collection"]["same_second_deduplicated_count"], 1)
        self.assertEqual(result["collection"]["quote_gaps"]["internal_count"], 1)

    def test_costs_currency_and_pnl_model_are_explicit_and_checked(self):
        with self.assertRaisesRegex(DatasetError, "costs"):
            collect_mt5_dataset(history(), {key: value for key, value in COSTS.items() if key != "fee_per_lot_side"}, 1000)
        raw = history(); raw["info"]["currency_profit"] = "EUR"
        with self.assertRaisesRegex(DatasetError, "currency conversion"):
            collect_mt5_dataset(raw, COSTS, 1000)

    def test_broker_history_uses_utc_demo_read_only_and_never_initializes(self):
        mt5 = FakeMT5()
        broker = object.__new__(MT5Broker); broker.mt5 = mt5; broker.symbol = "XAUUSD"
        end = datetime.now(UTC).replace(microsecond=0)
        result = broker.research_history(end - timedelta(hours=2), end)
        self.assertEqual(result["requested_range"]["end"], int(end.timestamp()))
        self.assertEqual(len([call for call in mt5.calls if isinstance(call, tuple) and call[0] == "ticks"]), 2)
        self.assertEqual(len([call for call in mt5.calls if call == "order_calc_profit"]), 4)
        self.assertFalse(any(call in ("initialize", "shutdown", "order_send") for call in mt5.calls))
        mt5.copy_ticks_range = lambda *args: None
        with self.assertRaisesRegex(BrokerError, "tick history unavailable"):
            broker.research_history(end - timedelta(minutes=30), end)

    def test_broker_rejects_linear_model_mismatch_and_non_demo(self):
        mt5 = FakeMT5(); broker = object.__new__(MT5Broker); broker.mt5 = mt5; broker.symbol = "XAUUSD"
        end = datetime.now(UTC).replace(microsecond=0)
        mt5.order_calc_profit = lambda *args: 0
        with self.assertRaisesRegex(BrokerError, "linear dataset model"):
            broker.research_history(end - timedelta(minutes=5), end)
        mt5.account.trade_mode = 1
        with self.assertRaisesRegex(BrokerError, "demo account"):
            broker.research_history(end - timedelta(minutes=5), end)

    def test_canonical_dataset_size_is_bounded(self):
        raw = history()
        raw["bars"]["M15"] = [{"time": START - 900, "open": 1, "high": 2, "low": 1, "close": 1,
                                "tick_volume": "x" * (8 * 1024 * 1024)}]
        with self.assertRaisesRegex(DatasetError, "8 MiB"):
            collect_mt5_dataset(raw, COSTS, 1000)

    def test_normalized_quote_symbols_are_included_in_canonical_size_limit(self):
        raw = history()
        raw["symbol"] = "S" * 500
        raw["requested_range"]["end"] = START + 100_001
        raw["fetched_at"] = START + 100_001
        raw["ticks"] = [{"time_msc": (START + index + 1) * 1000,
                         "bid": 2000, "ask": 2000.2} for index in range(100_000)]
        with self.assertRaisesRegex(DatasetError, "8 MiB"):
            collect_mt5_dataset(raw, COSTS, 1000)

    def test_broker_rejects_invalid_specs_and_nonfinite_profit_calculation(self):
        mt5 = FakeMT5(); broker = object.__new__(MT5Broker); broker.mt5 = mt5; broker.symbol = "XAUUSD"
        end = datetime.now(UTC).replace(microsecond=0)
        mt5.info.trade_tick_size = None
        mt5.info._asdict = lambda: dict(INFO, trade_tick_size=None)
        with self.assertRaisesRegex(BrokerError, "Invalid MT5 instrument specification"):
            broker.research_history(end - timedelta(minutes=5), end)
        mt5 = FakeMT5(); broker.mt5 = mt5
        mt5.order_calc_profit = lambda *args: None
        with self.assertRaisesRegex(BrokerError, "linear dataset model"):
            broker.research_history(end - timedelta(minutes=5), end)


if __name__ == "__main__":
    unittest.main()
