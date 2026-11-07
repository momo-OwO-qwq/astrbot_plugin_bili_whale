"""B 站 @ 通知轮询的边界测试（不联网）。

覆盖：首次启用只记基线、推送失败可重试、作息窗口外不花模型额度、
认不出 BV 时把原链接带上。
"""

import asyncio
import importlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PKG = "astrbot_plugin_bili_whale"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_tools_write import FakeContext, install_stubs  # noqa: E402


class FakeMentionAPI:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def mentions(self, page_size=10):
        self.calls += 1
        return list(self.items)


def mention(mid, nickname="某人", uri="bilibili://video/170001", title="标题"):
    return {
        "id": mid,
        "user": {"nickname": nickname},
        "item": {"title": title, "source_content": "", "uri": uri},
    }


class MentionsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_stubs()
        self.mod = importlib.import_module(f"{PKG}.main")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod._data_dir = lambda: Path(self.tmp.name)

    def make_plugin(self, rhythm=None, account=None):
        config = {
            "account": {"owner_mid": "123456", "sessdata": "sess", **(account or {})},
            "rhythm": {"mentions_enabled": True, "mentions_interval_min": 3, **(rhythm or {})},
            "push": {"targets": ["webchat:FriendMessage:1"]},
        }
        return self.mod.BiliWhalePlugin(FakeContext(), self.mod.AstrBotConfig(config))

    # ── 作息窗口闸门 ──────────────────────────────────────────────────
    def test_digest_allowed_inside_window(self):
        plugin = self.make_plugin(rhythm={"enabled": True, "window": "20:00-24:00"})
        with patch.object(self.mod, "now_local", return_value=datetime(2026, 1, 1, 21, 0)):
            self.assertTrue(plugin._mentions_may_digest())

    def test_digest_blocked_outside_window(self):
        plugin = self.make_plugin(rhythm={"enabled": True, "window": "20:00-24:00"})
        with patch.object(self.mod, "now_local", return_value=datetime(2026, 1, 1, 3, 0)):
            self.assertFalse(plugin._mentions_may_digest())

    def test_digest_allowed_for_cross_midnight_window(self):
        plugin = self.make_plugin(rhythm={"enabled": True, "window": "22:00-02:00"})
        with patch.object(self.mod, "now_local", return_value=datetime(2026, 1, 1, 1, 0)):
            self.assertTrue(plugin._mentions_may_digest())

    def test_digest_allowed_when_clock_disabled(self):
        plugin = self.make_plugin(rhythm={"enabled": False, "window": "20:00-24:00"})
        with patch.object(self.mod, "now_local", return_value=datetime(2026, 1, 1, 3, 0)):
            self.assertTrue(plugin._mentions_may_digest())

    # ── 定时器生命周期 ────────────────────────────────────────────────
    def test_ticker_needed_when_only_mentions_on(self):
        plugin = self.make_plugin(rhythm={"enabled": False, "mentions_enabled": True})
        self.assertTrue(plugin._automation_enabled())

    def test_ticker_not_needed_when_both_off(self):
        plugin = self.make_plugin(rhythm={"enabled": False, "mentions_enabled": False})
        self.assertFalse(plugin._automation_enabled())

    async def test_mention_poll_runs_as_single_background_task(self):
        plugin = self.make_plugin()
        plugin._poll_mentions = AsyncMock()
        plugin._spawn_mention_poll()
        first = plugin._mention_task
        plugin._spawn_mention_poll()
        self.assertIs(plugin._mention_task, first)
        await asyncio.wait_for(first, timeout=1)

    # ── 轮询 ────────────────────────────────────────────────────────────
    async def test_first_poll_records_baseline_without_pushing(self):
        plugin = self.make_plugin()
        api = FakeMentionAPI([mention(1), mention(2)])
        plugin._ensure_api = AsyncMock(return_value=api)
        plugin._push_all = AsyncMock(return_value=True)

        await plugin._poll_mentions()

        self.assertEqual(plugin.store.get("mentions_seen"), ["1", "2"])
        plugin._push_all.assert_not_awaited()

    async def test_new_mentions_are_pushed_and_marked(self):
        plugin = self.make_plugin()
        plugin.store.set("mentions_seen", ["1"])
        api = FakeMentionAPI([mention(2), mention(1)])
        plugin._ensure_api = AsyncMock(return_value=api)
        plugin._handle_mention = AsyncMock(return_value=(True, True))

        await plugin._poll_mentions()

        plugin._handle_mention.assert_awaited_once()
        self.assertEqual(plugin.store.get("mentions_seen"), ["1", "2"])

    async def test_failed_push_is_not_marked_seen(self):
        plugin = self.make_plugin()
        plugin.store.set("mentions_seen", [])
        plugin._ensure_api = AsyncMock(return_value=FakeMentionAPI([mention(7)]))
        plugin._handle_mention = AsyncMock(return_value=(False, False))

        await plugin._poll_mentions()

        self.assertEqual(plugin.store.get("mentions_seen"), [])
        self.assertEqual(plugin.store.get("mentions_failed"), {"7": 1})

    async def test_mention_gives_up_after_three_failures(self):
        plugin = self.make_plugin()
        plugin.store.set("mentions_seen", [])
        plugin.store.set("mentions_failed", {"7": 3})
        plugin._ensure_api = AsyncMock(return_value=FakeMentionAPI([mention(7)]))
        plugin._handle_mention = AsyncMock(return_value=(False, False))

        await plugin._poll_mentions()

        plugin._handle_mention.assert_not_awaited()
        self.assertEqual(plugin.store.get("mentions_seen"), ["7"])

    # ── 单条处理 ────────────────────────────────────────────────────────
    async def test_unresolved_video_link_is_forwarded(self):
        plugin = self.make_plugin()
        plugin._push_all = AsyncMock(return_value=True)
        with patch.object(self.mod, "resolve_bvid", AsyncMock(return_value=None)):
            ok, digested = await plugin._handle_mention(mention(3))

        self.assertTrue(ok)
        self.assertFalse(digested)
        text = plugin._push_all.await_args.args[0]
        self.assertIn("bilibili://video/170001", text)

    async def test_digest_skipped_when_allow_digest_false(self):
        plugin = self.make_plugin()
        plugin._push_all = AsyncMock(return_value=True)
        plugin._make_runner = AsyncMock()
        with patch.object(self.mod, "resolve_bvid", AsyncMock(return_value="BV1S6xY65Eey")):
            ok, digested = await plugin._handle_mention(mention(4), allow_digest=False)

        self.assertTrue(ok)
        self.assertFalse(digested)
        plugin._make_runner.assert_not_awaited()
        self.assertIn("上限", plugin._push_all.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
