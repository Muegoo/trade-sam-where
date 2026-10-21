import time
import unittest
from unittest.mock import patch

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trade_app.research_api import install_research
from trade_app.datasets import load_dataset
from tests.test_research_api import payload, wait_job


class NumpyH1Broker:
    def __init__(self, symbol, time_offset_seconds=0):
        self.symbol = symbol

    def snapshot(self):
        now = int(time.time())
        dtype = [("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8")]
        h1 = np.array([(now - 1601 * 3600 + i * 3600, 100 + i * .01,
                        101 + i * .01, 99 + i * .01, 100.5 + i * .01) for i in range(1601)], dtype=dtype)
        bars = [{"time": now - 601 * 900 + i * 900, "open": 100, "high": 101,
                 "low": 99, "close": 100.5} for i in range(601)]
        return {"account": {"currency": "USD", "balance": 10000, "equity": 10000,
                    "is_demo": True, "trade_allowed": True},
            "info": {"point": .01, "trade_tick_size": .01, "currency_profit": "USD",
                "trade_contract_size": 1, "volume_min": .1, "volume_max": 2, "volume_step": .1,
                "trade_stops_level": 1},
            "tick": {"bid": 100, "ask": 100.1, "time": now}, "positions": [], "orders": [],
            "deals": [], "bars": bars, "h1": h1, "scope": "demo:numpy-history"}

    def validate_research_instrument(self, *_args): return True
    def research_history(self, *_args): raise AssertionError("history is not part of this test")
    def close(self): pass


class DisconnectingNumpyBroker(NumpyH1Broker):
    calls = 0
    def snapshot(self):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("simulated disconnect")
        return super().snapshot()


class FakeForward:
    active = False
    def status(self): return {"status": "stopped", "active": False}
    def export_dataset(self): return payload()
    def export_result(self): return {"status": "stopped", "dataset_hash": "a" * 64,
        "source": "simulation_forward_shadow", "report": {"decisions": []}}
    def close(self): pass


class ResearchNextApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.token = "research-next-token"
        self.workspace = install_research(self.app, lambda: {"max_lot": .1}, lambda: [],
            lambda: self.token, forward=FakeForward())
        self.client = TestClient(self.app)

    def tearDown(self):
        self.workspace.close()
        self.client.close()

    def test_forward_downloads_are_separate_result_and_importable_dataset(self):
        self.assertEqual(self.client.get("/api/research/forward/dataset").status_code, 403)
        headers = {"X-Trade-Token": self.token}
        dataset = self.client.get("/api/research/forward/dataset", headers=headers).json()
        result = self.client.get("/api/research/forward/result", headers=headers).json()
        self.assertEqual(dataset["version"], 1)
        self.assertIn("quotes", dataset)
        self.assertIn("report", result)
        self.assertNotIn("dataset", result)

    def test_failed_collection_does_not_import_dataset_or_leak_reservation(self):
        import trade_app.research_api as research_api
        end = int(time.time()) - 10
        start = end - 120
        raw = {"unused": True}
        costs = payload()["costs"]
        def provider(_start, _end): return raw
        self.workspace._history_provider = provider
        self.workspace._collection_status_provider = lambda: {"available": True}
        self.workspace._monitor_blocked_provider = lambda: False
        with patch.object(research_api, "collect_mt5_dataset", return_value={"dataset": payload(),
                    "dataset_hash": load_dataset(payload()).dataset_hash}), \
             patch.object(research_api, "MAX_RETAINED_RESULT_BYTES", 0):
            response = self.client.post("/api/research/collect", headers={"X-Trade-Token": self.token}, json={
                "start_at": start, "end_at": end, "costs": costs, "margin_per_lot": 50})
            self.assertEqual(response.status_code, 202)
            finished = wait_job(self.client, response.json()["id"], self.token)
            self.assertEqual(finished["status"], "failed")
            self.assertIsNone(finished["dataset_hash"])
            self.assertEqual(len(self.workspace._datasets), 0)
            self.assertEqual(self.workspace._reserved_datasets, 0)

    def test_numpy_structured_h1_keeps_mt5_server_connected_and_state_serializable(self):
        import trade_app.server as server
        with patch.object(server, "MT5Broker", NumpyH1Broker):
            from pathlib import Path
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                app = server.create_app(mode="mt5", db_path=Path(folder) / "mt5-test.db")
                with TestClient(app) as client:
                    deadline = time.time() + 3
                    body = client.get("/api/state").json()
                    while not body.get("connected") and time.time() < deadline:
                        time.sleep(.02)
                        body = client.get("/api/state").json()
                    self.assertTrue(body["connected"], body.get("error"))
                    self.assertNotIn("forward_observation", body)
                    self.assertIsInstance(body["bars"], list)

    def test_server_clears_forward_cache_and_reports_disconnect_after_snapshot_error(self):
        import trade_app.server as server
        DisconnectingNumpyBroker.calls = 0
        with patch.object(server, "MT5Broker", DisconnectingNumpyBroker):
            from pathlib import Path
            import tempfile
            with tempfile.TemporaryDirectory() as folder:
                app = server.create_app(mode="mt5", db_path=Path(folder) / "disconnect-test.db")
                with TestClient(app) as client:
                    deadline = time.time() + 3
                    body = client.get("/api/state").json()
                    while not body.get("connected") and time.time() < deadline:
                        time.sleep(.02)
                        body = client.get("/api/state").json()
                    self.assertTrue(body["connected"], body.get("error"))
                    deadline = time.time() + 4
                    while body.get("connected") and time.time() < deadline:
                        time.sleep(.05)
                        body = client.get("/api/state").json()
                    self.assertFalse(body["connected"])


if __name__ == "__main__":
    unittest.main()
