import unittest

from core.settings import migrate_output, migrate_push, migrate_share, output_setting


class SettingsMigrationTest(unittest.TestCase):
    def test_migrates_old_report_and_diary(self):
        cfg = {
            "report": {"detail": "thin", "push_full": True},
            "diary": {"enabled": False, "push": True, "private_only": False},
        }
        self.assertTrue(migrate_output(cfg))
        self.assertEqual(cfg["output"]["report_detail"], "thin")
        self.assertTrue(cfg["output"]["push_full_report"])
        self.assertFalse(cfg["output"]["diary_enabled"])
        self.assertTrue(cfg["output"]["push_diary"])
        self.assertFalse(cfg["output"]["diary_private_only"])
        self.assertFalse(migrate_output(cfg))
        # 用户以后把新值调回默认，也不该被旧值再次盖回去
        cfg["output"]["report_detail"] = "full"
        self.assertFalse(migrate_output(cfg))
        self.assertEqual(cfg["output"]["report_detail"], "full")

    def test_new_values_win(self):
        cfg = {
            "output": {"report_detail": "full", "diary_enabled": True},
            "report": {"detail": "thin"},
            "diary": {"enabled": False},
        }
        migrate_output(cfg)
        # 新键仍是默认值 -> 旧值搬过来
        self.assertEqual(cfg["output"]["report_detail"], "thin")
        self.assertFalse(cfg["output"]["diary_enabled"])
        self.assertEqual(output_setting(cfg, "report_detail"), "thin")

    def test_migrates_legacy_push_session(self):
        cfg = {"push": {"session": "webchat:FriendMessage:1", "targets": []}}
        self.assertTrue(migrate_push(cfg))
        self.assertEqual(cfg["push"]["targets"], ["webchat:FriendMessage:1"])
        self.assertEqual(cfg["push"]["session"], "")
        self.assertFalse(migrate_push(cfg))

    def test_migrates_old_share_mode(self):
        for mode, enabled, auto in (
            ("off", False, False),
            ("manual", True, False),
            ("auto", True, True),
        ):
            cfg = {"share": {"mode": mode}}
            self.assertTrue(migrate_share(cfg), mode)
            self.assertEqual(cfg["share"]["enabled"], enabled, mode)
            self.assertEqual(cfg["share"]["auto_send"], auto, mode)
            self.assertEqual(cfg["share"]["mode"], "off", mode)
            self.assertFalse(migrate_share(cfg), mode)

    def test_share_new_values_win(self):
        cfg = {"share": {"mode": "manual", "enabled": True, "auto_send": True}}
        migrate_share(cfg)
        self.assertTrue(cfg["share"]["enabled"])
        self.assertTrue(cfg["share"]["auto_send"])

    def test_output_setting_falls_back(self):
        cfg = {"report": {"detail": "none"}}
        self.assertEqual(output_setting(cfg, "report_detail"), "none")
        self.assertEqual(output_setting(cfg, "push_full_report"), False)


if __name__ == "__main__":
    unittest.main()
