import copy
import json
import unittest

from trade_app.datasets import load_dataset
from trade_app.evaluation import evaluate_dataset


BASE = 1_700_000_000


def dataset(count=110, rising=False):
    bars = {"M15": [], "H1": []}
    quotes = {}
    for frame, seconds in (("M15", 900), ("H1", 3600)):
        previous = 100.0
        for index in range(count):
            time = BASE + index * seconds
            close = 100 + index * .04 if rising else 100 + index * .015 + ((index % 8) - 4) * .22
            open_price = previous
            high = max(open_price, close) + .5
            low = min(open_price, close) - .5
            bars[frame].append({"time": time, "available_at": time + seconds,
                                "open": open_price, "high": high, "low": low, "close": close})
            quotes[time + seconds + 1] = {"time": time + seconds + 1, "bid": close - .01,
                                         "ask": close + .01}
            previous = close
    payload = {"version": 1, "source": "synthetic-test", "timezone": "UTC",
               "instrument": {"symbol": "TEST", "currency": "USD", "point": .01,
                              "tick_size": .01, "contract_size": 1, "volume_min": .1,
                              "volume_max": 2, "volume_step": .1, "stops_level": 1,
                              "margin_per_lot": 50},
               "costs": {"commission_per_lot_side": 0, "fee_per_lot_side": 0,
                         "slippage_points": 0, "swap_long_per_lot_day": 0,
                         "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                         "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
               "quotes": sorted(quotes.values(), key=lambda row: row["time"]),
               "bars": bars, "evidence": []}
    return load_dataset(payload)


class EvaluationTests(unittest.TestCase):
    def evaluate(self, ds, strategy="ema", **kwargs):
        return evaluate_dataset(ds, strategies=[strategy], train_bars=80,
                                validation_bars=20, test_bars=2, purge_bars=1,
                                embargo_bars=1, **kwargs)

    def test_test_blocks_are_disjoint_and_result_is_repeatable_json(self):
        ds = dataset()
        first = self.evaluate(ds)
        second = self.evaluate(ds)
        strategy = first["strategies"]["ema"]
        self.assertEqual(first["run_hash"], second["run_hash"])
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(strategy["folds"]), 2)
        test_ranges = [fold["indices"]["test"] for fold in strategy["folds"]]
        self.assertTrue(all(left[1] <= right[0] for left, right in zip(test_ranges, test_ranges[1:])))
        self.assertTrue(all(fold["indices"]["purge"][1] == fold["indices"]["validation"][0]
                            and fold["indices"]["embargo"][1] == fold["indices"]["test"][0]
                            for fold in strategy["folds"]))
        json.dumps(first, allow_nan=False)

    def test_future_bar_change_does_not_change_earlier_ai_training_or_predictions(self):
        original = dataset()
        changed_payload = original.to_dict()
        changed_payload["bars"]["H1"][-1]["close"] += 3
        changed_payload["bars"]["H1"][-1]["high"] += 3
        changed = load_dataset(changed_payload)
        first = self.evaluate(original, "ai")["strategies"]["ai"]["folds"][0]
        second = self.evaluate(changed, "ai")["strategies"]["ai"]["folds"][0]
        self.assertEqual(first["training"]["fingerprint"], second["training"]["fingerprint"])
        self.assertEqual(first["training"]["provenance"], second["training"]["provenance"])
        self.assertEqual(first["predictions"], second["predictions"])
        indexes = first["training"]["provenance"]["fit_feature_indices"]
        self.assertTrue(all(index < first["indices"]["train"][1] for index in indexes))
        maximum = first["training"]["provenance"]["label_max_available_at"]
        self.assertLessEqual(maximum, first["training"]["provenance"]["validation_start_at"])

    def test_single_class_training_is_reported_and_cannot_propose_ai_orders(self):
        result = self.evaluate(dataset(rising=True), "ai")["strategies"]["ai"]
        fold = result["folds"][0]
        self.assertEqual(fold["training"]["status"], "unavailable")
        self.assertEqual(fold["training"]["reason"], "insufficient_training_rows_or_single_class")
        self.assertEqual(fold["proposals"], [])

    def test_insufficient_data_reports_frame_specific_counts(self):
        result = evaluate_dataset(dataset(count=50), strategies=["ema", "ai"],
                                  train_bars=80, validation_bars=20, test_bars=1,
                                  purge_bars=1, embargo_bars=1)
        self.assertEqual(result["status"], "insufficient_data")
        for row in result["strategies"].values():
            self.assertEqual(row["status"], "insufficient_data")
            self.assertGreater(row["bars_required"], row["bars_available"])

    def test_validation_rows_are_separate_from_fit_indices(self):
        fold = self.evaluate(dataset(), "ai")["strategies"]["ai"]["folds"][0]
        fit = fold["training"]["provenance"]["fit_feature_indices"]
        val_start, val_end = fold["indices"]["validation"]
        self.assertTrue(all(index < val_start for index in fit))
        self.assertTrue(all(not val_start <= index < val_end for index in fit))
        self.assertLessEqual(fold["training"]["provenance"]["validation_label_max_available_at"],
                             fold["training"]["provenance"]["validation_known_by"])

    def test_calendar_evidence_not_known_at_decision_is_not_a_gate_input(self):
        payload = dataset().to_dict()
        at = payload["bars"]["M15"][102]["available_at"]
        payload["evidence"] = [{"id": "later", "kind": "calendar", "source": "test",
                                "symbol": "TEST", "published_at": at + 10,
                                "available_at": at + 11, "fetched_at": at + 12,
                                "event_at": at, "impact": "high", "value": "later-known"}]
        result = self.evaluate(load_dataset(payload))["strategies"]["ema"]["folds"][0]
        self.assertFalse(any(row.get("event_ids") == ["later"] for row in result["exclusions"]))

    def test_complete_fold_count_over_limit_fails_before_evaluation(self):
        with self.assertRaisesRegex(ValueError, "requires 203 folds; limit is 200"):
            evaluate_dataset(dataset(count=305), strategies=["ema"], train_bars=80,
                             validation_bars=20, test_bars=1, purge_bars=1,
                             embargo_bars=1)

    def test_final_fold_quote_window_excludes_unrelated_future_quotes(self):
        original = dataset()
        changed_payload = original.to_dict()
        last_time = changed_payload["quotes"][-1]["time"] + 365 * 86400
        changed_payload["quotes"].append({"time": last_time, "bid": 500, "ask": 500.01})
        extended = load_dataset(changed_payload)
        original_fold = self.evaluate(original)["strategies"]["ema"]["folds"][-1]
        extended_fold = self.evaluate(extended)["strategies"]["ema"]["folds"][-1]
        self.assertEqual(original_fold["quote_window"], extended_fold["quote_window"])
        self.assertEqual(original_fold["replay"]["trades"], extended_fold["replay"]["trades"])
        self.assertEqual(original_fold["replay"]["metrics"], extended_fold["replay"]["metrics"])
        self.assertLessEqual(extended_fold["quote_window"]["end_exclusive"],
                             extended_fold["times"]["test"][1] + 3600)


if __name__ == "__main__":
    unittest.main()
