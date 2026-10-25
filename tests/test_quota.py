import unittest
from datetime import datetime, timedelta

from core import quota


NOW = datetime(2026, 10, 25, 12, 0)
LIMITS = quota.Limits()


def fresh():
    return {"date": "2026-10-25", "hour": "2026102512", "reads_hour": 0, "reads_day": 0,
            "writes_hour": 0, "writes_day": 0, "shares_day": 0, "share_bv": {}}


class QuotaTest(unittest.TestCase):
    def test_read_budget(self):
        q = fresh()
        self.assertTrue(quota.can_read(q, LIMITS, NOW))
        quota.record_read(q, NOW)
        self.assertEqual(q["reads_day"], 1)
        q["reads_hour"] = LIMITS.read_per_hour
        decision = quota.can_read(q, LIMITS, NOW)
        self.assertFalse(decision)
        self.assertIn("小时", decision.reason)

    def test_hour_and_day_rollover(self):
        q = fresh()
        q["reads_day"] = 5
        quota.normalize(q, datetime(2026, 10, 26, 9, 0))
        self.assertEqual(q["reads_day"], 0)
        self.assertEqual(q["hour"], "2026102609")

    def test_risk_ban_and_recovery(self):
        q = fresh()
        minutes = quota.hit_risk(q, -412, "IP 限流", NOW)
        self.assertEqual(minutes, 300)
        self.assertFalse(quota.can_read(q, LIMITS, NOW))
        self.assertFalse(quota.can_write(q, LIMITS, NOW))
        later = NOW + timedelta(minutes=6)
        self.assertTrue(quota.can_read(q, LIMITS, later))
        quota.note_success(q, later)
        self.assertEqual(q["ban_until"], "")

    def test_backoff_escalates(self):
        q = fresh()
        a = quota.hit_risk(q, -412, "x", NOW)
        b = quota.hit_risk(q, -412, "x", NOW)
        c = quota.hit_risk(q, -412, "x", NOW)
        d = quota.hit_risk(q, -412, "x", NOW)
        self.assertEqual([a, b, c, d], [300, 600, 1200, 1800])

    def test_write_cooldown(self):
        q = fresh()
        self.assertTrue(quota.can_write(q, LIMITS, NOW))
        quota.record_write(q, NOW)
        self.assertFalse(quota.can_write(q, LIMITS, NOW + timedelta(seconds=30)))
        self.assertTrue(quota.can_write(q, LIMITS, NOW + timedelta(seconds=121)))

    def test_share_rules(self):
        q = fresh()
        self.assertTrue(quota.can_share(q, LIMITS, "BV1xx411c7mD", 16, "这条讲了具体的东西", NOW))
        self.assertFalse(quota.can_share(q, LIMITS, "BV1xx411c7mD", 10, "分数不够的理由", NOW))
        self.assertFalse(quota.can_share(q, LIMITS, "BV1xx411c7mD", 16, "太短", NOW))
        quota.record_share(q, "BV1xx411c7mD", NOW)
        self.assertFalse(quota.can_share(q, LIMITS, "BV1xx411c7mD", 16, "这条讲了具体的东西", NOW))
        self.assertFalse(quota.can_share(q, LIMITS, "BV1xx411c7mD", 16, "这条讲了具体的东西", NOW + timedelta(days=6)))
        self.assertTrue(quota.can_share(q, LIMITS, "BV1xx411c7mD", 16, "这条讲了具体的东西", NOW + timedelta(days=8)))

    def test_daily_share_cap(self):
        q = fresh()
        q["shares_day"] = LIMITS.share_max_per_day
        self.assertFalse(quota.can_share(q, LIMITS, "BV1xx411c7mD", 20, "这条讲了具体的东西", NOW))

    def test_dm_daily_cap_and_reset(self):
        q = fresh()
        for _ in range(LIMITS.dm_per_day):
            self.assertTrue(quota.can_dm(q, LIMITS, NOW))
            quota.record_dm(q, NOW)
        self.assertFalse(quota.can_dm(q, LIMITS, NOW))
        later = NOW + timedelta(days=1)
        self.assertTrue(quota.can_dm(q, LIMITS, later))
        self.assertEqual(q.get("dm_day"), 0)


if __name__ == "__main__":
    unittest.main()
