import copy
import unittest

from trade_app.decision_trace import build_replay_trace, verify_trace
from trade_app.datasets import load_dataset
from trade_app.replay import run_replay
from tests.test_replay import BASE, dataset, proposal


class DecisionTraceTests(unittest.TestCase):
    def test_trace_is_anchored_ordered_and_point_in_time(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 5, 100, 100.1), (BASE + 6, 101, 101.1)])
        payload = ds.to_dict()
        payload["evidence"] = [{"id": "future", "kind": "news", "source": "test", "symbol": "TEST",
                               "published_at": BASE + 2, "available_at": BASE + 3,
                               "fetched_at": BASE + 4, "value": "later"}]
        ds = load_dataset(payload)
        supplied = proposal(at=BASE + 1)
        result = run_replay(ds, [supplied])
        trace = build_replay_trace(ds, result)
        self.assertTrue(verify_trace(trace))
        accepted = next(event for event in trace["events"] if event["kind"] == "proposal_accepted")
        self.assertEqual(accepted["source_snapshot"]["latest_quote"]["time"], BASE)
        self.assertEqual(accepted["source_snapshot"]["available_evidence_ids"], [])
        self.assertEqual(accepted["evidence_reference_status"], "available_evidence_only")
        self.assertIn("risk_snapshot", accepted)
        self.assertLessEqual([event["time"] for event in trace["events"]],
                             sorted(event["time"] for event in trace["events"]))

    def test_chain_detects_event_provenance_and_unanchored_top_level_edits(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 5, 100, 100.1)])
        trace = build_replay_trace(ds, run_replay(ds, [proposal()]))
        for mutate in (lambda value: value["events"][0].update(kind="changed"),
                       lambda value: value.update(run_hash="f" * 64),
                       lambda value: value.update(unanchored=True)):
            modified = copy.deepcopy(trace)
            mutate(modified)
            self.assertFalse(verify_trace(modified))

    def test_pending_order_does_not_claim_a_fill(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 5, 100, 100.1), (BASE + 6, 100, 100.1)])
        pending = proposal(kind="LIMIT", entry=99, sl=97, tp=103)
        result = run_replay(ds, [pending], close_at_end=False)
        trace = build_replay_trace(ds, result)
        self.assertTrue(verify_trace(trace))
        kinds = [event["kind"] for event in trace["events"]]
        self.assertIn("order_pending_or_unknown", kinds)
        self.assertNotIn("order_filled", kinds)

    def test_pending_fill_and_same_quote_stop_have_causal_event_order(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 2, 100, 100.1),
                      (BASE + 5, 98, 98.1), (BASE + 6, 98, 98.1)])
        limit = proposal(at=BASE + 1, kind="LIMIT", entry=99, sl=98.5, tp=103)
        result = run_replay(ds, [limit])
        trace = build_replay_trace(ds, result)
        at_fill = [event["kind"] for event in trace["events"] if event["time"] == BASE + 5]
        self.assertLess(at_fill.index("fill_revalidation"), at_fill.index("order_filled"))
        self.assertLess(at_fill.index("order_filled"), at_fill.index("position_entry_deal"))
        self.assertLess(at_fill.index("position_entry_deal"), at_fill.index("position_exit_deal"))
        self.assertLess(at_fill.index("position_exit_deal"), at_fill.index("position_closed"))

    def test_swap_deal_does_not_replace_original_fill_timestamp(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 100, 100, 100.1),
                      (BASE + 7200, 100, 100.1)])
        result = run_replay(ds, [proposal()], close_at_end=True)
        trace = build_replay_trace(ds, result)
        order_filled = next(event for event in trace["events"] if event["kind"] == "order_filled")
        self.assertEqual(order_filled["time"], BASE + 100)
        self.assertTrue(any(event["kind"] == "swap_accrual" for event in trace["events"]))

    def test_no_eligible_quote_rejection_cannot_precede_its_source_snapshot(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 5, 100, 100.1)])
        result = run_replay(ds, [proposal(at=BASE + 100)])
        trace = build_replay_trace(ds, result)
        rejected = next(event for event in trace["events"] if event["kind"] == "proposal_rejected")
        self.assertEqual(rejected["evaluated_at"], BASE + 5)
        self.assertEqual(rejected["time"], BASE + 100)
        self.assertLessEqual(rejected["source_snapshot"]["as_of"], rejected["time"])
        self.assertNotIn("order_filled", [event["kind"] for event in trace["events"]])

    def test_quote_dependent_risk_rejection_uses_evaluation_quote_and_decision_snapshot(self):
        ds = dataset([(BASE, 100, 100.1), (BASE + 5, 100, 101)])
        result = run_replay(ds, [proposal(at=BASE + 1)])
        trace = build_replay_trace(ds, result)
        rejected = next(event for event in trace["events"] if event["kind"] == "proposal_rejected")
        self.assertEqual(rejected["stage"], "acceptance")
        self.assertEqual(rejected["time"], BASE + 5)
        self.assertEqual(rejected["evaluated_at"], BASE + 5)
        self.assertEqual(rejected["source_snapshot"]["as_of"], BASE + 1)


if __name__ == "__main__":
    unittest.main()
