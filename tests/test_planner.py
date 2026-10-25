import unittest
from datetime import datetime

from core import planner


def rng(*values):
    seq = list(values)
    calls = {"n": 0}

    def _next():
        value = seq[calls["n"] % len(seq)]
        calls["n"] += 1
        return value

    return _next


class PlannerTest(unittest.TestCase):
    def test_parse_multiple_windows_and_wrap(self):
        windows = planner.parse_windows("12:00-14:00, 23:30-02:00")
        self.assertEqual([w.start for w in windows], [720, 1410])
        self.assertEqual(windows[1].end, 26 * 60)

    def test_invalid_window(self):
        with self.assertRaises(ValueError):
            planner.parse_windows("25:00-26:00")
        with self.assertRaises(ValueError):
            planner.parse_windows("20:00")

    def test_plan_falls_inside_window(self):
        now = datetime(2026, 10, 25, 20, 10)
        state = {}
        plan = planner.make_plan(
            state, "20:00-24:00", wakes_per_day=1, skip_chance=0.0,
            min_gap_min=10, now=now, rng=rng(0.5, 0.9),
        )
        self.assertEqual(plan["action"], "wake")
        at = planner.parse_iso(plan["at"])
        self.assertGreaterEqual(at, datetime(2026, 10, 25, 20, 11))
        self.assertLessEqual(at, datetime(2026, 10, 25, 23, 59))
        self.assertTrue(planner.is_pending(plan, now))

    def test_pending_plan_is_not_redrawn(self):
        now = datetime(2026, 10, 25, 20, 10)
        state = {}
        first = planner.make_plan(state, "20:00-24:00", 1, 0.0, 10, now=now, rng=rng(0.1, 0.9))
        second = planner.make_plan(state, "20:00-24:00", 1, 0.0, 10, now=now, rng=rng(0.9, 0.1))
        self.assertEqual(first["at"], second["at"])

    def test_skip_plan(self):
        now = datetime(2026, 10, 25, 20, 10)
        plan = planner.make_plan({}, "20:00-24:00", 1, 1.0, 10, now=now, rng=rng(0.1, 0.0))
        self.assertEqual(plan["action"], "skip")

    def test_min_gap_blocks_window(self):
        now = datetime(2026, 10, 25, 20, 55)
        state = {"history": {"date": "2026-10-25", "wakes": 0, "used": {}, "last_woke": "2026-10-25T20:50:00"}}
        plan = planner.make_plan(state, "20:00-21:00", 2, 0.0, 10, now=now, rng=rng(0.5, 0.5))
        self.assertEqual(plan["action"], "done")
        self.assertIn("排不下", plan["reason"])

    def test_wakes_exhausted(self):
        now = datetime(2026, 10, 25, 20, 10)
        state = {"history": {"date": "2026-10-25", "wakes": 1, "used": {}, "last_woke": ""}}
        plan = planner.make_plan(state, "20:00-24:00", 1, 0.0, 10, now=now, rng=rng(0.5, 0.5))
        self.assertEqual(plan["action"], "done")

    def test_complete_plan_counts_wake(self):
        now = datetime(2026, 10, 25, 20, 10)
        state = {}
        planner.make_plan(state, "20:00-24:00", 2, 0.0, 1, now=now, rng=rng(0.5, 0.9))
        plan = planner.complete_plan(state, ran=True, now=now)
        self.assertTrue(plan["fired"])
        self.assertTrue(plan["woke"])
        self.assertEqual(state["history"]["wakes"], 1)
        self.assertEqual(state["history"]["last_woke"], "2026-10-25T20:10:00")

    def test_cross_midnight_window(self):
        now = datetime(2026, 10, 26, 0, 30)
        plan = planner.make_plan({}, "23:00-02:00", 1, 0.0, 10, now=now, rng=rng(0.5, 0.9))
        self.assertEqual(plan["action"], "wake")
        at = planner.parse_iso(plan["at"])
        self.assertLessEqual(at, datetime(2026, 10, 26, 1, 59))
        self.assertGreater(at, now)

    def test_expired_plan(self):
        now = datetime(2026, 10, 25, 23, 0)
        plan = {"at": "2026-10-25T20:00:00", "fired": False}
        self.assertTrue(planner.is_expired(plan, now, grace_min=90))


if __name__ == "__main__":
    unittest.main()
