import unittest

from trade_app.event_filter import event_blackout


BASE = 1_700_000_000


def event(identifier, event_at, symbol="TEST", published=BASE - 100, available=BASE - 90, impact="high"):
    return {"id": identifier, "kind": "calendar", "symbol": symbol, "published_at": published,
            "available_at": available, "event_at": event_at, "impact": impact, "value": identifier}


class EventFilterTests(unittest.TestCase):
    def test_half_open_boundaries_and_disabled_window(self):
        row = event("e1", BASE + 60)
        self.assertFalse(event_blackout([row], "TEST", BASE + 29, 0, 0)["blocked"])
        self.assertTrue(event_blackout([row], "TEST", BASE, 1, 1)["blocked"])
        self.assertTrue(event_blackout([row], "TEST", BASE + 59, 1, 1)["blocked"])
        self.assertFalse(event_blackout([row], "TEST", BASE + 120, 1, 1)["blocked"])
        self.assertFalse(event_blackout([row], "TEST", BASE + 60, 0, 0)["blocked"])

    def test_future_release_and_other_symbol_do_not_block(self):
        future = event("future", BASE + 50, published=BASE + 1, available=BASE + 2)
        other = event("other", BASE + 50, symbol="OTHER")
        result = event_blackout([future, other], "TEST", BASE, 2, 2)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["status"], "not_configured")

    def test_overlaps_have_stable_order_and_latest_endpoint(self):
        rows = [event("b", BASE + 100), event("a", BASE + 90), event("inactive", BASE + 1000)]
        result = event_blackout(rows, "TEST", BASE + 100, 1, 2)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["event_ids"], ["a", "b"])
        self.assertEqual(result["next_allowed_at"], BASE + 220)

    def test_known_calendar_without_active_event_is_not_implied_safe_data(self):
        result = event_blackout([event("known", BASE + 100)], "TEST", BASE, 0, 0)
        self.assertFalse(result["blocked"])
        self.assertEqual(result["status"], "known_calendar")
        self.assertEqual(result["next_allowed_at"], None)

    def test_next_allowed_extends_through_connected_known_windows_only(self):
        rows = [event("active", BASE + 60), event("connected", BASE + 120),
                event("gap", BASE + 300)]
        result = event_blackout(rows, "TEST", BASE, 1, 0)
        self.assertEqual(result["event_ids"], ["active"])
        self.assertEqual(result["blocking_window_event_ids"], ["active", "connected"])
        self.assertEqual(result["next_allowed_at"], BASE + 120)

    def test_window_bounds_are_checked(self):
        with self.assertRaises(ValueError):
            event_blackout([], "TEST", BASE, 1441, 0)


if __name__ == "__main__":
    unittest.main()
