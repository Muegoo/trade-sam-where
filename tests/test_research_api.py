import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trade_app.decision_trace import verify_trace
from trade_app.research_api import MAX_DATASET_BYTES, install_research
from trade_app.server import create_app


BASE = 1_700_000_000


def payload(with_bars=False):
    bars = [{"time": BASE + i * 900, "available_at": BASE + (i + 1) * 900,
             "open": 100 + i, "high": 102 + i, "low": 99 + i, "close": 101 + i}
            for i in range(80)] if with_bars else []
    return {"version": 1, "source": "api-fixture", "timezone": "UTC",
        "instrument": {"symbol": "TEST", "currency": "USD", "point": .01, "tick_size": .01,
            "contract_size": 1, "volume_min": .1, "volume_max": 2, "volume_step": .1,
            "stops_level": 1, "margin_per_lot": 50},
        "costs": {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
            "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
            "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
        "quotes": [{"time": BASE, "bid": 100, "ask": 100.1},
                   {"time": BASE + 5, "bid": 100, "ask": 100.1},
                   {"time": BASE + 10, "bid": 101, "ask": 101.1},
                   {"time": BASE + 100, "bid": 101, "ask": 101.1}],
        "bars": {"M15": bars, "H1": []}, "evidence": []}


def wait_job(client, job_id, token):
    for _ in range(100):
        result = client.get(f"/api/research/jobs/{job_id}", headers={"X-Trade-Token": token})
        if result.json()["status"] in ("completed", "failed"):
            return result.json()
        time.sleep(.02)
    raise AssertionError("research job did not finish")


class ResearchApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.token = "test-session-token"
        self.workspace = install_research(self.app, lambda: {"max_lot": .1},
                                          lambda: [], lambda: self.token)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.workspace.close()
        self.client.close()

    def upload(self, value):
        return self.client.post("/api/research/datasets", headers={"X-Trade-Token": self.token},
                                json=value)

    def test_get_requires_token_and_upload_rejects_duplicate_nan_and_size(self):
        self.assertEqual(self.client.get("/api/research/status").status_code, 403)
        for raw in (b'{"x":1,"x":2}', b'[NaN]'):
            response = self.client.post("/api/research/datasets", content=raw,
                headers={"X-Trade-Token": self.token, "Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("x\":2", response.text)
        oversized = self.client.post("/api/research/datasets", content=b" " * (MAX_DATASET_BYTES + 1),
            headers={"X-Trade-Token": self.token, "Content-Type": "application/json"})
        self.assertEqual(oversized.status_code, 413)

    def test_dataset_caps_deduplicate_and_snapshot_is_causal(self):
        first = self.upload(payload())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["first_quote_time"], BASE)
        self.assertEqual(first.json()["last_quote_time"], BASE + 100)
        self.assertEqual(self.upload(payload()).json()["dataset_hash"], first.json()["dataset_hash"])
        snapshot = self.client.get(f"/api/research/datasets/{first.json()['dataset_hash']}/snapshot?as_of={BASE+5}",
            headers={"X-Trade-Token": self.token})
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.json()["quotes"][-1]["time"], BASE + 5)

    def test_replay_result_and_trace_download_verify(self):
        manifest = self.upload(payload()).json()
        proposal = {"decision_at": BASE + 1, "side": "BUY", "kind": "MARKET", "entry": None,
                    "sl": 98, "tp": 104, "lot": .1, "strategy": "test"}
        submitted = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json={
            "dataset_hash": manifest["dataset_hash"], "kind": "replay",
            "proposals_json": json.dumps([proposal]), "options": {}})
        self.assertEqual(submitted.status_code, 202)
        job_id = submitted.json()["id"]
        self.assertEqual(wait_job(self.client, job_id, self.token)["status"], "completed")
        result = self.client.get(f"/api/research/jobs/{job_id}/result", headers={"X-Trade-Token": self.token})
        trace = self.client.get(f"/api/research/jobs/{job_id}/trace", headers={"X-Trade-Token": self.token})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(trace.status_code, 200)
        self.assertTrue(verify_trace(trace.json()))
        self.assertEqual(result.json()["dataset_hash"], manifest["dataset_hash"])

    def test_evaluate_uses_real_function_and_reports_insufficient_data(self):
        manifest = self.upload(payload()).json()
        submitted = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json={
            "dataset_hash": manifest["dataset_hash"], "kind": "evaluate", "options": {}})
        self.assertEqual(submitted.status_code, 202)
        job_id = submitted.json()["id"]
        self.assertEqual(wait_job(self.client, job_id, self.token)["status"], "completed")
        result = self.client.get(f"/api/research/jobs/{job_id}/result", headers={"X-Trade-Token": self.token})
        self.assertEqual(result.json()["status"], "insufficient_data")

    def test_job_busy_and_secret_is_absent_from_public_status(self):
        manifest = self.upload(payload()).json()
        secret = "sensitive-api-key-marker"
        submitted = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json={
            "dataset_hash": manifest["dataset_hash"], "kind": "advisor", "provider": "openai",
            "model": "test-model", "api_key": secret, "options": {"as_of": BASE + 100}})
        self.assertEqual(submitted.status_code, 202)
        status = self.client.get("/api/research/status", headers={"X-Trade-Token": self.token})
        self.assertNotIn(secret, status.text)
        active = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json={
            "dataset_hash": manifest["dataset_hash"], "kind": "shadow", "proposals_json": "[]",
            "options": {"as_of": BASE + 100}})
        self.assertIn(active.status_code, (202, 409))
        job_id = submitted.json()["id"]
        self.assertEqual(wait_job(self.client, job_id, self.token)["status"], "completed")
        result = self.client.get(f"/api/research/jobs/{job_id}/result", headers={"X-Trade-Token": self.token})
        self.assertNotIn(secret, result.text)

    def test_kind_specific_fields_and_advisor_credentials_validate_before_enqueue(self):
        manifest = self.upload(payload()).json()
        secret = "sentinel-api-key-never-echo"
        invalid_requests = [
            {"dataset_hash": manifest["dataset_hash"], "kind": "replay", "options": {},
             "proposals_json": "[]", "api_key": secret},
            {"dataset_hash": manifest["dataset_hash"], "kind": "evaluate", "options": {},
             "provider": "openai"},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "disabled", "api_key": secret},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "disabled", "model": ""},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "openai", "model": "model\nname", "api_key": secret},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "openai", "model": "model-name", "api_key": secret + "\n"},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "openai", "model": "model-name"},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "gemini", "model": " model-name", "api_key": secret},
            {"dataset_hash": manifest["dataset_hash"], "kind": "advisor", "options": {"as_of": BASE + 100},
             "provider": "unknown", "model": "model-name", "api_key": secret},
        ]
        for body in invalid_requests:
            before = self.client.get("/api/research/status", headers={"X-Trade-Token": self.token}).json()["jobs"]
            response = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json=body)
            after = self.client.get("/api/research/status", headers={"X-Trade-Token": self.token}).json()["jobs"]
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
            self.assertEqual(after, before)

        valid = self.client.post("/api/research/jobs", headers={"X-Trade-Token": self.token}, json={
            "dataset_hash": manifest["dataset_hash"], "kind": "advisor", "provider": "disabled",
            "model": None, "api_key": None, "options": {"as_of": BASE + 100}})
        self.assertEqual(valid.status_code, 202)
        finished = wait_job(self.client, valid.json()["id"], self.token)
        self.assertEqual(finished["status"], "completed")
        result = self.client.get(f"/api/research/jobs/{valid.json()['id']}/result",
                                 headers={"X-Trade-Token": self.token}).json()
        self.assertEqual(result["executed_orders"], 0)
        self.assertEqual(result["provider_status"], "not_configured")

    def test_real_server_preserves_host_token_and_origin_guards(self):
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(db_path=Path(folder) / "research-api.db")
            with TestClient(app) as client:
                initial_state = client.get("/api/state").json()
                token = initial_state["token"]
                self.assertNotIn("forward_observation", initial_state)
                self.assertEqual(client.get("/api/research/status").status_code, 403)
                self.assertEqual(client.get("/api/research/status", headers={"X-Trade-Token": token}).status_code, 200)
                blocked = client.post("/api/research/datasets", json=payload(),
                    headers={"X-Trade-Token": token, "Origin": "http://attacker.example"})
                self.assertEqual(blocked.status_code, 403)
                host_blocked = client.get("/api/research/status", headers={"X-Trade-Token": token,
                    "Host": "attacker.example"})
                self.assertEqual(host_blocked.status_code, 403)


if __name__ == "__main__":
    unittest.main()
