import copy
import unittest

from trade_app.datasets import DatasetError, load_dataset


def payload():
    t = 1_700_000_000
    return {"version": 1, "source": "synthetic-test", "timezone": "UTC",
            "instrument": {"symbol": "TEST", "currency": "USD", "point": 0.01, "tick_size": 0.01,
                           "contract_size": 1, "volume_min": 0.1, "volume_max": 2, "volume_step": 0.1,
                           "stops_level": 1, "margin_per_lot": 50},
            "costs": {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
                      "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                      "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
            "quotes": [{"time": t, "bid": 100, "ask": 100.1}, {"time": t+10, "bid": 100.2, "ask": 100.3}],
            "bars": {"M15": [{"time": t-1800, "available_at": t-900, "open": 10, "high": 12, "low": 9, "close": 11},
                              {"time": t+100, "available_at": t+1000, "open": 10, "high": 12, "low": 9, "close": 11}], "H1": []},
            "evidence": [{"id": "known-event", "kind": "calendar", "source": "test", "symbol": "TEST",
                          "published_at": t+2, "available_at": t+3, "fetched_at": t+1000,
                          "event_at": t+500, "value": "scheduled", "impact": "high"},
                         {"id": "future-news", "kind": "news", "source": "test", "symbol": "TEST",
                          "published_at": t+20, "available_at": t+21, "fetched_at": t+22, "value": "later"}]}


class DatasetTests(unittest.TestCase):
    def test_snapshot_is_point_in_time_and_can_include_known_future_event(self):
        ds = load_dataset(payload())
        snap = ds.snapshot(1_700_000_010)
        self.assertEqual(len(snap["bars"]["M15"]), 1)
        self.assertEqual([e["id"] for e in snap["evidence"]], ["known-event"])
        self.assertEqual(snap["evidence"][0]["event_at"], 1_700_000_500)
        self.assertEqual(snap["latest_quote"]["time"], 1_700_000_010)
        self.assertEqual(snap["dataset_hash"], ds.dataset_hash)
        self.assertNotIn("future-news", [e["id"] for e in snap["evidence"]])

    def test_symbol_proxy_is_rejected(self):
        data = payload(); data["quotes"][0]["symbol"] = "XAUUSD"
        with self.assertRaisesRegex(DatasetError, "quotes\\[0\\].symbol"):
            load_dataset(data)

    def test_missing_and_nonfinite_costs_are_rejected(self):
        data = payload(); del data["costs"]["fee_per_lot_side"]
        with self.assertRaisesRegex(DatasetError, "costs.fee_per_lot_side"):
            load_dataset(data)
        data = payload(); data["costs"]["slippage_points"] = float("nan")
        with self.assertRaisesRegex(DatasetError, "costs.slippage_points"):
            load_dataset(data)

    def test_hash_is_stable_and_input_is_not_mutated(self):
        data = payload(); before = copy.deepcopy(data)
        self.assertEqual(load_dataset(data).dataset_hash, load_dataset(copy.deepcopy(data)).dataset_hash)
        self.assertEqual(data, before)
        self.assertEqual(load_dataset(data).to_dict()["bars"]["H1"], [])

    def test_rejects_boolean_version_proxy_bars_unknown_frames_and_unusable_time(self):
        data = payload(); data["version"] = True
        with self.assertRaisesRegex(DatasetError, "version"):
            load_dataset(data)
        data = payload(); data["bars"]["M15"][0]["symbol"] = "PROXY"
        with self.assertRaisesRegex(DatasetError, "bars.M15.*symbol"):
            load_dataset(data)
        data = payload(); data["bars"]["H4"] = []
        with self.assertRaisesRegex(DatasetError, "unsupported timeframe"):
            load_dataset(data)
        data = payload(); data["quotes"][0]["time"] = 10**1000
        with self.assertRaisesRegex(DatasetError, "quotes\\[0\\].time"):
            load_dataset(data)

    def test_evidence_text_fields_are_bounded(self):
        data = payload(); data["evidence"][0]["source"] = "s" * 501
        with self.assertRaisesRegex(DatasetError, "evidence\\[0\\].source"):
            load_dataset(data)
        data = payload(); data["evidence"][0]["value"] = "v" * 20001
        with self.assertRaisesRegex(DatasetError, "evidence\\[0\\].value"):
            load_dataset(data)


if __name__ == "__main__":
    unittest.main()
