import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from trade_app.datasets import load_dataset


BASE = 1_700_000_000


def payload():
    return {"version": 1, "source": "synthetic-test", "timezone": "UTC",
            "instrument": {"symbol": "TEST", "currency": "USD", "point": 0.01, "tick_size": 0.01,
                           "contract_size": 1, "volume_min": 0.1, "volume_max": 2, "volume_step": 0.1,
                           "stops_level": 1, "margin_per_lot": 50},
            "costs": {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
                      "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
                      "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
            "quotes": [{"time": BASE, "bid": 100, "ask": 100.1},
                       {"time": BASE + 5, "bid": 100, "ask": 100.1},
                       {"time": BASE + 10, "bid": 101, "ask": 101.1},
                       {"time": BASE + 100, "bid": 101, "ask": 101.1}],
            "bars": {"M15": [], "H1": []}, "evidence": []}


def replay_proposal():
    return {"decision_at": BASE + 1, "side": "BUY", "kind": "MARKET", "entry": None,
            "sl": 98, "tp": 104, "lot": 0.1, "strategy": "test"}


class ResearchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset_path = self.write("dataset.json", payload())

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        return path

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "trade_app.research", *map(str, args)],
                              cwd=Path(__file__).resolve().parents[1], text=True,
                              capture_output=True, check=False)

    def test_validate_snapshot_and_replay_trace_integration(self):
        valid = self.run_cli("validate", "--dataset", self.dataset_path)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        manifest = json.loads(valid.stdout)
        self.assertEqual(manifest["counts"]["quotes"], 4)
        snapshot_path = self.root / "snapshot.json"
        snap = self.run_cli("snapshot", "--dataset", self.dataset_path, "--as-of", BASE + 5,
                            "--output", snapshot_path)
        self.assertEqual(snap.returncode, 0, snap.stderr)
        self.assertEqual(json.loads(snapshot_path.read_text())["quotes"][-1]["time"], BASE + 5)
        proposals_path = self.write("proposals.json", [replay_proposal()])
        result_path, trace_path = self.root / "result.json", self.root / "trace.json"
        replay = self.run_cli("replay", "--dataset", self.dataset_path, "--proposals", proposals_path,
                              "--output", result_path, "--trace-output", trace_path)
        self.assertEqual(replay.returncode, 0, replay.stderr)
        result, trace = json.loads(result_path.read_text()), json.loads(trace_path.read_text())
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(result["dataset_hash"], load_dataset(payload()).dataset_hash)
        self.assertEqual(trace["run_hash"], result["run_hash"])
        self.assertEqual(trace["trace_hash"], trace["events"][-1]["hash"])

    def test_shadow_enforces_snapshot_geometry_expiry_and_keeps_no_trade(self):
        ds = load_dataset(payload())
        snapshot = ds.snapshot(BASE + 10)
        proposal = {"action": "BUY", "strategy": "shadow", "created_at": BASE + 1,
                    "expires_at": BASE + 100, "snapshot_hash": snapshot["snapshot_hash"],
                    "evidence_ids": [], "reason": "test", "entry": 101, "sl": 99, "tp": 104}
        wrong = dict(proposal, snapshot_hash="0" * 64)
        proposals_path = self.write("shadow.json", [proposal, wrong, dict(proposal, action="WAIT")])
        wait = {key: proposal[key] for key in ("strategy", "created_at", "expires_at", "snapshot_hash",
                                                "evidence_ids", "reason")}
        wait["action"] = "WAIT"
        proposals_path = self.write("shadow.json", [proposal, wrong, wait])
        output = self.run_cli("shadow", "--dataset", self.dataset_path, "--proposals", proposals_path,
                              "--as-of", BASE + 10)
        self.assertEqual(output.returncode, 0, output.stderr)
        result = json.loads(output.stdout)
        self.assertEqual(result["provider_status"], "not_configured")
        self.assertEqual(result["executed_orders"], 0)
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["counts"]["no_trade"], 1)
        self.assertEqual(result["accepted"][1]["status"], "WAIT")

    def test_duplicate_keys_nonfinite_and_invalid_input_create_no_output(self):
        for raw in ('{"a":1,"a":2}', '[NaN]'):
            source = self.root / "bad.json"
            source.write_text(raw, encoding="utf-8")
            output = self.root / "must-not-exist.json"
            result = self.run_cli("snapshot", "--dataset", source, "--as-of", BASE, "--output", output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
        proposals = self.root / "bad-proposals.json"
        proposals.write_text('[{"decision_at":NaN}]', encoding="utf-8")
        output = self.root / "replay-output.json"
        result = self.run_cli("replay", "--dataset", self.dataset_path, "--proposals", proposals,
                              "--output", output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())

    def test_existing_output_is_preserved(self):
        path = self.root / "existing.json"
        path.write_text("preserve me", encoding="utf-8")
        result = self.run_cli("snapshot", "--dataset", self.dataset_path, "--as-of", BASE, "--output", path)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(path.read_text(encoding="utf-8"), "preserve me")

    def test_same_output_paths_are_rejected(self):
        proposals = self.write("proposals.json", [replay_proposal()])
        path = self.root / "one.json"
        result = self.run_cli("replay", "--dataset", self.dataset_path, "--proposals", proposals,
                              "--output", path, "--trace-output", self.root / "." / "one.json")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(path.exists())

    def test_calendar_filter_preserves_original_proposal_indexes_and_output_pair(self):
        data = payload()
        data["evidence"] = [{"id": "event", "kind": "calendar", "source": "test", "symbol": "TEST",
                             "published_at": BASE - 10, "available_at": BASE - 9,
                             "fetched_at": BASE - 8, "event_at": BASE + 30,
                             "impact": "high", "value": "scheduled"}]
        dataset_path = self.write("calendar-dataset.json", data)
        first = replay_proposal()
        first["strategy"] = "blocked"
        second = replay_proposal()
        second.update(decision_at=BASE + 90, strategy="accepted")
        proposals_path = self.write("calendar-proposals.json", [first, second])
        output, trace = self.root / "calendar-result.json", self.root / "calendar-trace.json"
        existing_trace = self.root / "existing-trace.json"
        existing_trace.write_text("keep", encoding="utf-8")
        failed = self.run_cli("replay", "--dataset", dataset_path, "--proposals", proposals_path,
                              "--calendar-before", 1, "--output", output, "--trace-output", existing_trace)
        self.assertEqual(failed.returncode, 2)
        self.assertFalse(output.exists())
        self.assertEqual(existing_trace.read_text(encoding="utf-8"), "keep")
        result = self.run_cli("replay", "--dataset", dataset_path, "--proposals", proposals_path,
                              "--calendar-before", 1, "--output", output, "--trace-output", trace)
        self.assertEqual(result.returncode, 0, result.stderr)
        replayed = json.loads(output.read_text())
        self.assertEqual(replayed["rejected"][0]["index"], 0)
        self.assertEqual(replayed["rejected"][0]["stage"], "calendar")
        self.assertEqual(replayed["accepted"][0]["index"], 1)
        self.assertEqual(replayed["rejected"][0]["next_allowed_at"], BASE + 30)

    def test_duplicate_replay_proposal_is_reported_once_and_counted(self):
        proposal = replay_proposal()
        proposals_path = self.write("duplicate-proposals.json", [proposal, dict(proposal)])
        result = self.run_cli("replay", "--dataset", self.dataset_path, "--proposals", proposals_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        replayed = json.loads(result.stdout)
        self.assertEqual(replayed["duplicate_count"], 1)
        self.assertEqual(len(replayed["accepted"]), 1)
        self.assertEqual(len([row for row in replayed["rejected"] if row["stage"] == "duplicate"]), 1)


if __name__ == "__main__":
    unittest.main()
