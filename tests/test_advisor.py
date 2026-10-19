import json
import unittest

import httpx

from trade_app.advisor import AdvisorError, ENDPOINTS, generate_advice
from trade_app.datasets import load_dataset


AS_OF = 1_700_072_000
API_KEY = "sentinel-private-key-keep-out"


def dataset(with_bars=True, with_calendar=False):
    start = AS_OF - 80 * 900
    bars = [{"time": start + i * 900, "available_at": start + (i + 1) * 900,
             "open": 100 + i * .01, "high": 101 + i * .01,
             "low": 99 + i * .01, "close": 100 + i * .01}
            for i in range(80)] if with_bars else []
    evidence = []
    if with_calendar:
        evidence = [{"id": "event-1", "kind": "calendar", "source": "archive", "symbol": "TEST",
                     "published_at": AS_OF - 20, "available_at": AS_OF - 10, "fetched_at": AS_OF - 5,
                     "event_at": AS_OF, "impact": "high", "value": "scheduled"}]
    payload = {"version": 1, "source": "fixture", "timezone": "UTC",
        "instrument": {"symbol": "TEST", "currency": "USD", "point": .01, "tick_size": .01,
            "contract_size": 1, "volume_min": .1, "volume_max": 2, "volume_step": .1,
            "stops_level": 1, "margin_per_lot": 50},
        "costs": {"commission_per_lot_side": 0, "fee_per_lot_side": 0, "slippage_points": 0,
            "swap_long_per_lot_day": 0, "swap_short_per_lot_day": 0, "rollover_hour_utc": 1,
            "rollover_weekdays": [0, 1, 2, 3, 4], "triple_swap_weekday": None},
        "quotes": [{"time": AS_OF - 1, "bid": 100, "ask": 100.1}],
        "bars": {"M15": bars, "H1": []}, "evidence": evidence}
    return load_dataset(payload)


def advisor_reply(action="WAIT", reason="wait", evidence_ids=None, entry=None, sl=None, tp=None):
    return {"action": action, "reason": reason, "evidence_ids": evidence_ids or [],
            "entry": entry, "sl": sl, "tp": tp}


class AdvisorTests(unittest.TestCase):
    def test_disabled_and_insufficient_data_never_request_provider(self):
        class NoCall:
            def stream(self, *args, **kwargs):
                raise AssertionError("provider must not be called")
        disabled = generate_advice(dataset(), AS_OF, client=NoCall())
        self.assertEqual(disabled["provider_status"], "not_configured")
        self.assertEqual(disabled["executed_orders"], 0)
        insufficient = generate_advice(dataset(with_bars=False), AS_OF, "openai", "model", API_KEY,
                                       client=NoCall())
        self.assertEqual(insufficient["provider_status"], "no_data")

    def test_openai_contract_is_fixed_and_wait_is_shadow_only(self):
        reply = advisor_reply()
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(reply)}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False,
                              follow_redirects=False)
        result = generate_advice(dataset(), AS_OF, "openai", "test-model", API_KEY,
                                 challenger=False, max_output_tokens=512, client=client)
        request = calls[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), ENDPOINTS["openai"])
        self.assertEqual(request.headers["authorization"], f"Bearer {API_KEY}")
        self.assertEqual(body["max_completion_tokens"], 512)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn(API_KEY, request.content.decode())
        self.assertEqual(result["proposal"]["action"], "WAIT")
        self.assertEqual(result["executed_orders"], 0)
        self.assertIsNone(result["cost"])

    def test_challenger_uses_its_own_schema_and_vetoes_only(self):
        replies = [advisor_reply(), {"approve": False, "reason": "evidence unclear", "evidence_ids": []}]
        calls = []
        urls = []
        def handler(request):
            calls.append(json.loads(request.content))
            urls.append(str(request.url))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(replies.pop(0))}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        result = generate_advice(dataset(), AS_OF, "gemini", "test-model", API_KEY,
                                 challenger=True, client=client)
        self.assertEqual(urls, [ENDPOINTS["gemini"], ENDPOINTS["gemini"]])
        second_prompt = calls[1]["messages"][1]["content"]
        self.assertIn('"approve":boolean', second_prompt)
        self.assertEqual(result["analyst_proposal"]["action"], "WAIT")
        self.assertEqual(result["proposal"]["action"], "NO_TRADE")
        self.assertEqual(result["executed_orders"], 0)

    def test_provider_redirect_and_error_body_are_not_followed_or_returned(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://attacker.invalid/"},
                                  text=API_KEY)
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False,
                              follow_redirects=False)
        with self.assertRaises(AdvisorError) as raised:
            generate_advice(dataset(), AS_OF, "openai", "test-model", API_KEY,
                             challenger=False, client=client)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), ENDPOINTS["openai"])
        self.assertNotIn(API_KEY, str(raised.exception))

    def test_invalid_analyst_and_secret_echo_stop_before_challenger(self):
        for invalid in (advisor_reply(evidence_ids=["unseen-id"]),
                        advisor_reply(reason=f"leaked {API_KEY}")):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                    "message": {"content": json.dumps(invalid)}}]})
            client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
            with self.assertRaises(AdvisorError):
                generate_advice(dataset(), AS_OF, "openai", "test-model", API_KEY, client=client)
            self.assertEqual(len(calls), 1)

    def test_calendar_blackout_changes_final_advice_to_no_trade(self):
        reply = advisor_reply("BUY", evidence_ids=["event-1"], entry=100.1, sl=99, tp=103)
        def handler(request):
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(reply)}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        result = generate_advice(dataset(with_calendar=True), AS_OF, "openai", "test-model", API_KEY,
                                 challenger=False, calendar_after_minutes=1, client=client)
        self.assertEqual(result["analyst_proposal"]["action"], "BUY")
        self.assertEqual(result["proposal"]["action"], "NO_TRADE")
        self.assertEqual(result["executed_orders"], 0)

    def test_outcome_memory_excludes_future_wrong_symbol_and_incomplete(self):
        from trade_app.advisor import _closed_memory
        rows = [{"symbol": "TEST", "closed_at": AS_OF, "net": 2.0, "source": "simulation", "completed": True},
                {"symbol": "TEST", "closed_at": AS_OF + 1, "net": 3.0, "source": "simulation", "completed": True},
                {"symbol": "OTHER", "closed_at": AS_OF, "net": 4.0, "source": "simulation", "completed": True},
                {"symbol": "TEST", "closed_at": AS_OF, "net": 5.0, "source": "simulation", "completed": False}]
        self.assertEqual(_closed_memory(rows, "TEST", AS_OF),
                         [{"closed_at": AS_OF, "net": 2.0, "source": "simulation", "net_r": None}])


if __name__ == "__main__":
    unittest.main()
