import unittest

from core.memory_policy import (
    MemoryPolicy,
    already_logged,
    build_note,
    decide,
    pick,
    record,
    today_count,
)
from core.memory_policy import attach
from core.models import DeepResult, RunResult, Video

RNG_MISS = lambda: 0.99
RNG_HIT = lambda: 0.0


def result(score=15, interesting=0.0, verdict=None, bvid="BV1xx411c7mD", title="测试视频"):
    item = DeepResult(
        video=Video(bvid=bvid, title=title, author="UP"),
        score=score,
        verdict=verdict or ("值得看" if score >= 13 else "可以看" if score >= 8 else "不值当"),
        why="它讲清楚了原理",
        one_liner="可以看看",
        interesting=interesting,
    )
    return item


class MemoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.policy = MemoryPolicy(min_score=13, strong_score=16, chance=0.35, interesting_threshold=0.8)

    def test_below_min_score_not_saved(self):
        self.assertEqual(decide(result(12), self.policy, RNG_HIT), "")

    def test_not_worth_watching_never_saved(self):
        self.assertEqual(decide(result(19, verdict="不值当"), self.policy, RNG_HIT), "")

    def test_strong_score_always_saved(self):
        self.assertEqual(decide(result(16), self.policy, RNG_MISS), "strong")

    def test_middle_score_uses_chance(self):
        self.assertEqual(decide(result(14), self.policy, RNG_MISS), "")
        self.assertEqual(decide(result(14), self.policy, RNG_HIT), "chance")

    def test_interesting_overrides_chance(self):
        self.assertEqual(decide(result(9, interesting=0.85), self.policy, RNG_MISS), "interesting")

    def test_zero_threshold_not_treated_as_interesting(self):
        policy = MemoryPolicy(min_score=13, strong_score=16, chance=0.0, interesting_threshold=0.0)
        self.assertEqual(decide(result(14, interesting=0.0), policy, RNG_HIT), "")

    def test_chance_bounds(self):
        always = MemoryPolicy(min_score=13, strong_score=16, chance=1.0)
        never = MemoryPolicy(min_score=13, strong_score=16, chance=0.0)
        self.assertEqual(decide(result(14), always, RNG_MISS), "chance")
        self.assertEqual(decide(result(14), never, RNG_HIT), "")

    def test_pick_prefers_strong(self):
        rows = [result(14, bvid="BV1aa411c7mD"), result(17, bvid="BV1bb411c7mD")]
        best, reason = pick(rows, self.policy, RNG_MISS)
        self.assertIsNotNone(best)
        self.assertEqual(best.video.bvid, "BV1bb411c7mD")
        self.assertEqual(reason, "strong")

    def test_pick_disabled(self):
        policy = MemoryPolicy(enabled=False)
        self.assertEqual(pick([result(18)], policy, RNG_MISS), (None, ""))
        policy = MemoryPolicy(auto_write=False)
        self.assertEqual(pick([result(18)], policy, RNG_MISS), (None, ""))

    def test_policy_from_config(self):
        cfg = {"memory": {"min_score": 10, "strong_score": 8, "chance": 5, "max_per_day": 999}}
        policy = MemoryPolicy.from_config(cfg)
        self.assertEqual(policy.strong_score, 10)  # 不能低于 min_score
        self.assertEqual(policy.chance, 1.0)
        self.assertEqual(policy.max_per_day, 50)

    def test_build_note(self):
        note = build_note(result(17), "strong", max_chars=500)
        self.assertIn("BV1xx411c7mD", note)
        self.assertIn("高光", note)

    def test_attach_fills_run_result(self):
        run = RunResult()
        reason = attach(run, [result(18)], self.policy, RNG_MISS)
        self.assertEqual(reason, "strong")
        self.assertEqual(run.memory_bvid, "BV1xx411c7mD")
        self.assertIn("今天在 B 站看了", run.memory_note)

    def test_attach_nothing_when_not_qualified(self):
        run = RunResult()
        self.assertEqual(attach(run, [result(5)], self.policy, RNG_HIT), "")
        self.assertEqual(run.memory_note, "")

    def test_log_helpers(self):
        log = {}
        self.assertEqual(today_count(log, "2026-10-31"), 0)
        record(log, "2026-10-31", "BV1xx411c7mD")
        record(log, "2026-10-31", "BV1xx411c7mD")
        self.assertEqual(today_count(log, "2026-10-31"), 1)
        self.assertTrue(already_logged(log, "2026-10-31", "BV1xx411c7mD"))
        self.assertFalse(already_logged(log, "2026-10-31", "BV1xx411c7mE"))


if __name__ == "__main__":
    unittest.main()
