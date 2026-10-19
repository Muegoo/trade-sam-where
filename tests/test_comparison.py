import json
import unittest

import httpx
from unittest.mock import patch

from trade_app.comparison import compare_strategy
from trade_app.datasets import load_dataset
from trade_app.evaluation import evaluate_dataset
from tests.test_evaluation import dataset


BASE = 1_700_000_000
API_KEY = "comparison-test-secret"


def comparison_dataset(with_calendar=False):
    original = dataset(120).to_dict()
    decisions = [bar["available_at"] for bar in original["bars"]["M15"]]
    quotes = {row["time"]: row for row in original["quotes"]}
    for decision in decisions:
        quotes[decision - 1] = {"time": decision - 1, "bid": 100, "ask": 100.1}
    original["quotes"] = sorted(quotes.values(), key=lambda row: row["time"])
    if with_calendar:
        frozen = evaluate_dataset(load_dataset(original), strategies=["ema"], train_bars=80,
            validation_bars=20, test_bars=5, purge_bars=1, embargo_bars=1,
            calendar_before_minutes=0, calendar_after_minutes=0)
        proposal = next(row for fold in frozen["strategies"]["ema"]["folds"] for row in fold["proposals"])
        at = proposal["decision_at"]
        original["evidence"] = [{"id": "known-event", "kind": "calendar", "source": "fixture",
            "symbol": "TEST", "published_at": at - 30, "available_at": at - 20,
            "fetched_at": at - 10, "event_at": at, "impact": "high", "value": "scheduled"}]
    return load_dataset(original)


class ComparisonTests(unittest.TestCase):
    def options(self, **kwargs):
        return {"train_bars": 80, "validation_bars": 20, "test_bars": 5,
                "purge_bars": 1, "embargo_bars": 1, **kwargs}

    def test_calendar_veto_and_disabled_provider_are_explicit(self):
        result = compare_strategy(comparison_dataset(with_calendar=True), "ema",
            calendar_after_minutes=1, **self.options())
        self.assertGreater(result["variants"]["A_rules"]["metrics"]["closed_trades"], -1)
        self.assertEqual(result["provider_status"], "not_configured")
        self.assertEqual(result["variants"]["C_analyst_agreement"]["status"], "unavailable")
        self.assertIsNone(result["variants"]["C_analyst_agreement"]["metrics"])
        self.assertIsNone(result["winner"])
        blocked = [row for fold in result["folds"] for row in fold["decisions"] if row["calendar"]["blocked"]]
        self.assertTrue(blocked)
        self.assertTrue(all(not row["accepted"]["B_calendar"] for row in blocked))

    def test_mock_provider_uses_shared_analyst_response_for_c_and_d(self):
        calls = []
        challenge_count = 0
        def handler(request):
            nonlocal challenge_count
            body = json.loads(request.content)
            prompt = body["messages"][1]["content"]
            calls.append(prompt)
            if '"approve":boolean' in prompt:
                challenge_count += 1
                return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                    "message": {"content": json.dumps({"approve": challenge_count != 1,
                        "reason": "approved" if challenge_count != 1 else "vetoed", "evidence_ids": []})}}]})
            context = json.loads(prompt.split("Context JSON: ", 1)[1])
            signal = next(row for row in context["technical_signals"] if row["key"] == "ema")
            reply = {"action": signal["signal"], "reason": "matches frozen rule", "evidence_ids": [],
                     "entry": signal["entry"], "sl": signal["sl"], "tp": signal["tp"]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": json.dumps(reply)}}]})

        client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        result = compare_strategy(comparison_dataset(), "ema", provider="openai", model="mock",
            api_key=API_KEY, client=client, max_llm_decisions=20, **self.options())
        self.assertEqual(len(calls), result["llm_decisions"] * 2)
        self.assertTrue(all(row["accepted"]["C_analyst_agreement"] for fold in result["folds"]
                            for row in fold["decisions"] if row["advice_hash"]))
        c_count = sum(row["accepted"]["C_analyst_agreement"] for fold in result["folds"]
                      for row in fold["decisions"])
        d_count = sum(row["accepted"]["D_challenger_approved"] for fold in result["folds"]
                      for row in fold["decisions"])
        self.assertEqual(c_count - d_count, 1)
        self.assertNotIn(API_KEY, json.dumps(result))
        self.assertTrue(all(item["status"] == "available" for item in
                            result["paired_fold_pnl_delta_vs_A"].values()))

    def test_decision_budget_fails_before_any_provider_call(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "LLM decisions; limit is 1"):
            compare_strategy(comparison_dataset(), "ema", provider="openai", model="mock",
                api_key=API_KEY, max_llm_decisions=1, client=object(), **self.options())
        self.assertEqual(calls, [])

    def test_rejects_budget_over_hard_limit(self):
        with self.assertRaisesRegex(ValueError, "1..20"):
            compare_strategy(comparison_dataset(), "ema", max_llm_decisions=21, **self.options())

    def test_fold_start_advisor_snapshot_uses_global_prior_quote(self):
        from trade_app.comparison import _quote_window
        data = comparison_dataset().to_dict()
        frozen = evaluate_dataset(load_dataset(data), strategies=["ema"], **self.options())
        fold = frozen["strategies"]["ema"]["folds"][0]
        candidate = fold["proposals"][0]
        window = _quote_window(data, fold)
        self.assertFalse(any(row["time"] <= candidate["decision_at"] for row in window))
        self.assertTrue(any(row["time"] == candidate["decision_at"] - 1 for row in data["quotes"]))
        with patch("trade_app.advisor.generate_advice", return_value={
                "analyst_proposal": {"action": candidate["side"]},
                "challenger": {"approve": True}, "token_usage": []}) as advisor:
            result = compare_strategy(load_dataset(data), "ema", provider="openai", model="mock",
                api_key=API_KEY, max_llm_decisions=20, **self.options())
        decisions = [row for result_fold in result["folds"] for row in result_fold["decisions"]]
        first = next(row for row in decisions if row["decision_at"] == candidate["decision_at"])
        self.assertIsNotNone(first["advice_hash"])
        self.assertNotEqual(first["veto_reasons"].get("C_analyst_agreement"), "advisor_snapshot_unavailable")
        self.assertGreater(advisor.call_count, 0)


if __name__ == "__main__":
    unittest.main()
