import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from trade_app import demo_session
from trade_app.analytics import BANGKOK


class SessionTests(unittest.TestCase):
    def exercise(self, directory, *, demo=True, max_trades=5, evaluate_session=False):
        deadline = int(time.time()) + 2
        plan = {
            "expires_at": deadline, "port": 0, "symbol": "XAUUSD",
            "expected_scope": "test:demo", "max_trades": max_trades,
            "date": datetime.now(BANGKOK).date().isoformat(),
            "session_requested_at": int(time.time()), "db_path": "unused.db",
            "research_assumptions": [], "costs": {}, "margin_per_lot": 1,
            "evaluate_session": evaluate_session,
        }
        output = Path(directory)
        demo_session.write_json(output / "plan.json", plan)
        calls = []
        current = {
            "connected": True, "scope": "test:demo", "token": "test-secret",
            "account": {"is_demo": demo, "balance": 10000},
            "settings": {"kill_switch": False}, "positions": [], "orders": [],
            "journal": [], "analytics": {}, "day_net": 0,
            "monitor": {"enabled": False, "close_on_expiry": False, "trades_used": 0},
        }

        class Server:
            should_exit = False
            def __init__(self, *args):
                self.__class__.instance = self
            def run(self):
                while not self.should_exit:
                    time.sleep(.01)

        class Client:
            def __init__(self, **kwargs):
                self.headers = {}
            def close(self):
                pass
            def request(self, method, route, **kwargs):
                calls.append((method, route, kwargs.get("json")))
                payload = {}
                if route == "/api/state":
                    payload = copy.deepcopy(current)
                elif route == "/api/research/collect":
                    payload = {"id": "test-job", "status": "queued"}
                elif route == "/api/research/jobs/test-job":
                    payload = {"id": "test-job", "status": "failed", "error": "No history in test"}
                elif route == "/api/monitor/start":
                    current["monitor"].update(enabled=True, close_on_expiry=True)
                    if evaluate_session:
                        current['account']['balance']=9000
                elif route == '/api/research/datasets':
                    payload={'dataset_hash':'test-final-dataset'}
                elif route == '/api/research/jobs':
                    payload={'id':'final-job','status':'queued'}
                elif route == '/api/research/jobs/final-job':
                    payload={'id':'final-job','status':'failed'}
                elif route == "/api/monitor/stop":
                    current["monitor"]["enabled"] = False
                elif route == "/api/kill":
                    current["settings"]["kill_switch"] = True
                    current["monitor"]["close_on_expiry"] = False
                content = b"ticket,net\n" if route.startswith("/api/journal.csv") else None
                return httpx.Response(200, json=payload if content is None else None,
                                      content=content, request=httpx.Request(method, "http://localhost" + route))

        with (patch.object(demo_session, "create_app", return_value=object()),
              patch.object(demo_session.uvicorn, "Server", Server),
              patch.object(demo_session.httpx, "Client", Client)):
            if demo:
                demo_session.run(output / "plan.json")
            else:
                with self.assertRaisesRegex(RuntimeError, "Expected Demo"):
                    demo_session.run(output / "plan.json")
        self.assertTrue(Server.instance.should_exit)
        return plan, calls

    def test_deadline_stops_entries_sets_kill_exports_and_shuts_down(self):
        with tempfile.TemporaryDirectory() as directory:
            plan, calls = self.exercise(directory)
            started = next(body for method, route, body in calls if route == "/api/monitor/start")
            self.assertEqual(started["max_trades"], 5)
            self.assertEqual(started["expires_at"], plan["expires_at"])
            self.assertTrue(started["close_on_expiry"])
            self.assertIn(("POST", "/api/kill", {"active": True}), calls)
            output = Path(directory)
            self.assertTrue((output / "completed.json").exists())
            self.assertTrue((output / "journal.csv").exists())
            self.assertEqual(json.loads((output / "summary.json").read_text())["phase"], "completed")
            self.assertNotIn("token", json.loads((output / "latest-state.json").read_text()))

    def test_real_account_aborts_before_arming_monitor(self):
        with tempfile.TemporaryDirectory() as directory:
            _, calls = self.exercise(directory, demo=False)
            self.assertFalse(any(route == "/api/monitor/start" for _, route, _ in calls))
            self.assertTrue((Path(directory) / "fatal-error.json").exists())

    def test_unlimited_plan_still_closes_at_its_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            plan,calls=self.exercise(directory,max_trades=None)
            body=next(body for _,route,body in calls if route=='/api/monitor/start')
            self.assertIsNone(body['max_trades'])
            self.assertEqual(body['expires_at'],plan['expires_at'])
            self.assertTrue((Path(directory)/'completed.json').exists())

    def test_final_research_does_not_use_future_closing_balance_for_sizing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(demo_session.SessionRecorder,'export_dataset',return_value={'version':1}):
                _,calls=self.exercise(directory,evaluate_session=True)
            body=next(body for _,route,body in calls if route=='/api/research/jobs')
            self.assertEqual(body['options']['initial_balance'],10000)
            self.assertFalse(body['options']['settings']['kill_switch'])
            self.assertTrue(body['options']['require_quote_coverage'])


if __name__ == "__main__":
    unittest.main()
