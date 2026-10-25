import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.models import Video
from core.store import Store


class StoreTest(unittest.TestCase):
    def test_roundtrip_and_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.set("last_umo", "demo:FriendMessage:1")
            video = Video(bvid="BV1xx411c7mD", title="标题", author="UP")
            store.mark_seen(video, "深读")
            self.assertTrue(store.is_seen(video.bvid))
            self.assertEqual(store.seen_count(), 1)
            again = Store(tmp)
            self.assertEqual(again.get("last_umo"), "demo:FriendMessage:1")
            self.assertTrue(again.is_seen(video.bvid))
            self.assertEqual(again.quota, {})

    def test_pending_share_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.set_pending_share({"bvid": "BV1xx411c7mD", "created_at": "2020-01-01T00:00:00"})
            self.assertIsNone(store.get_pending_share())
            store.set_pending_share({"bvid": "BV1xx411c7mD", "created_at": "2026-10-25T12:00:00"})
            self.assertIsNotNone(store.get_pending_share(now=datetime(2026, 10, 25, 12, 10)))
            store.clear_pending_share()
            self.assertIsNone(store.get_pending_share())

    def test_diary_and_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.append_diary("owner", "第一封信", "2026-10-25")
            store.append_diary("owner", "第二封信", "2026-10-25")
            text = store.read_diary("owner", "2026-10-25")
            self.assertIn("第一封信", text)
            self.assertIn("第二封信", text)
            self.assertIn("给主人的信", text)
            path = store.report_path("2026-10-25")
            path.write_text("# 日报\n", encoding="utf-8")
            self.assertEqual(store.read_report("2026-10-25"), "# 日报\n")
            self.assertEqual(store.list_reports(), ["2026-10-25"])

    def test_corrupt_state_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            store = Store(tmp)
            self.assertEqual(store.get("plan"), {})
            self.assertTrue((Path(tmp) / "state.broken.json").exists())


if __name__ == "__main__":
    unittest.main()
