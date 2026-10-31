"""写工具（发评论 / 私信 / 写记忆）与自动记忆的门槛测试。

用 AstrBot stub 加载 main.py，不联网、不发真实请求。
"""

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PKG = "astrbot_plugin_bili_whale"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import livingmemory  # noqa: E402


def install_stubs() -> None:
    if "astrbot.api.star" in sys.modules and PKG in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class AstrBotConfig(dict):
        def save_config(self) -> None:
            self["_saved"] = True

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def __init__(self, message_str="", umo="webchat:FriendMessage:1", group=""):
            self.message_str = message_str
            self.unified_msg_origin = umo
            self._group = group
            self.role = "member"

        def plain_result(self, text):
            return ("plain", text)

        def get_group_id(self):
            return self._group

        def is_admin(self):
            return False

    class MessageChain:
        def __init__(self):
            self.parts = []

        def message(self, text):
            self.parts.append(text)
            return self

    class _Filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda fn: fn

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()

    star_mod = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context):
            self.context = context

    star_mod.Context = object
    star_mod.Star = Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod

    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PKG] = pkg


class FakeEngine:
    def __init__(self):
        self.calls = []

    async def add_memory(self, **kwargs):
        self.calls.append(kwargs)
        return 11


class FakeContext:
    def __init__(self, engine=None):
        self.engine = engine
        self.sent = []
        self.platform_manager = SimpleNamespace(platform_insts=[])

    def get_all_stars(self):
        if self.engine is None:
            return []
        plugin = SimpleNamespace(initializer=SimpleNamespace(memory_engine=self.engine))
        return [
            SimpleNamespace(
                name="astrbot_plugin_livingmemory",
                root_dir_name="astrbot_plugin_livingmemory",
                display_name="LivingMemory",
                activated=True,
                star_cls=plugin,
            )
        ]

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

    async def get_current_chat_provider_id(self, umo=None):
        return None

    def get_using_provider(self, umo=None):
        return None


class FakeAPI:
    def __init__(self, verified=True):
        self.verified = verified
        self.posted = []
        self.dms = []

    async def video_info(self, bvid, force=False):
        from astrbot_plugin_bili_whale.core.models import Video

        return Video(bvid=bvid, title="测试视频", aid=42, author="UP")

    async def post_comment(self, aid, message):
        from astrbot_plugin_bili_whale.core.models import CommentPostResult

        self.posted.append((aid, message))
        return CommentPostResult(rpid="999", verified=self.verified, visible_to_self=self.verified, reason="测试")

    async def send_dm(self, mid, text):
        self.dms.append((mid, text))
        return {"msg_key": 1}


def make_plugin(engine=None, memory=None, **llm):
    install_stubs()
    mod = importlib.import_module(f"{PKG}.main")
    # 每个用例独立数据目录，避免 state.json 里的 memory_log 互相影响。
    tmp = tempfile.TemporaryDirectory()
    mod._data_dir = lambda: Path(tmp.name)
    config = {
        "account": {"owner_mid": "123456"},
        "llm": {"enabled": True, "allow_tools": True, "allow_write_tools": False, **llm},
        "memory": {"enabled": True, "auto_write": True, "max_chars": 500, **(memory or {})},
        "push": {"bili_dm": {"enabled": True, "to_mid": ""}},
        "share": {"enabled": True, "auto_send": False},
    }
    plugin = mod.BiliWhalePlugin(FakeContext(engine), mod.AstrBotConfig(config))
    return mod, plugin


def make_event(mod, group=""):
    return mod.AstrMessageEvent("", "webchat:FriendMessage:1", group)


class WriteToolTest(unittest.IsolatedAsyncioTestCase):
    def test_tools_are_registered(self):
        _mod, plugin = make_plugin()
        for name in ("bili_post_comment", "bili_send_dm", "bili_memorize"):
            self.assertTrue(hasattr(plugin, name), name)

    async def test_post_comment_denied_by_default(self):
        mod, plugin = make_plugin()
        plugin._ensure_api = AsyncMock()
        out = await plugin.bili_post_comment(make_event(mod), "BV1xx411c7mD", "好看")
        self.assertIn("写操作默认关闭", out)
        plugin._ensure_api.assert_not_called()

    async def test_send_dm_denied_by_default(self):
        mod, plugin = make_plugin()
        plugin._ensure_api = AsyncMock()
        out = await plugin.bili_send_dm(make_event(mod), "你好")
        self.assertIn("写操作默认关闭", out)
        plugin._ensure_api.assert_not_called()

    async def test_post_comment_verified(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        api = FakeAPI(verified=True)
        plugin._ensure_api = AsyncMock(return_value=api)
        out = await plugin.bili_post_comment(
            make_event(mod), "https://www.bilibili.com/video/BV1xx411c7mD/", "真不错"
        )
        self.assertIn("复查可见", out)
        self.assertEqual(api.posted, [(42, "真不错")])
        self.assertIn("真不错", plugin.store.recent_comments(5))

    async def test_post_comment_not_visible(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        api = FakeAPI(verified=False)
        plugin._ensure_api = AsyncMock(return_value=api)
        out = await plugin.bili_post_comment(make_event(mod), "BV1xx411c7mD", "真不错")
        self.assertIn("复查未通过", out)
        self.assertNotIn("真不错", plugin.store.recent_comments(5))

    async def test_post_comment_group_requires_admin(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        plugin._ensure_api = AsyncMock()
        out = await plugin.bili_post_comment(mod.AstrMessageEvent("", "umo", "group1"), "BV1xx411c7mD", "好看")
        self.assertIn("管理员", out)
        plugin._ensure_api.assert_not_called()

    async def test_send_dm_uses_configured_mid_only(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        api = FakeAPI()
        plugin._ensure_api = AsyncMock(return_value=api)
        out = await plugin.bili_send_dm(make_event(mod), "晚安")
        self.assertIn("123456", out)
        self.assertEqual(api.dms, [(123456, "晚安")])

    async def test_send_dm_requires_enabled(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        plugin.config["push"]["bili_dm"]["enabled"] = False
        plugin._ensure_api = AsyncMock()
        out = await plugin.bili_send_dm(make_event(mod), "晚安")
        self.assertIn("私信推送是关的", out)
        plugin._ensure_api.assert_not_called()

    async def test_memorize_writes_engine(self):
        engine = FakeEngine()
        mod, plugin = make_plugin(engine, allow_write_tools=True)
        out = await plugin.bili_memorize(make_event(mod), "今天看了《A》：很好", bvid="BV1xx411c7mD")
        self.assertIn("已写入 LivingMemory", out)
        self.assertEqual(engine.calls[0]["metadata"]["bvid"], "BV1xx411c7mD")

    async def test_memorize_without_livingmemory(self):
        mod, plugin = make_plugin(allow_write_tools=True)
        out = await plugin.bili_memorize(make_event(mod), "今天看了《A》")
        self.assertIn("LivingMemory", out)

    async def test_memorize_disabled(self):
        engine = FakeEngine()
        mod, plugin = make_plugin(engine, allow_write_tools=True)
        plugin.config["memory"]["enabled"] = False
        out = await plugin.bili_memorize(make_event(mod), "今天看了《A》")
        self.assertIn("已关闭", out)
        self.assertEqual(engine.calls, [])


class AutoMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_remembers_once_per_video_per_day(self):
        engine = FakeEngine()
        _mod, plugin = make_plugin(engine)
        result = SimpleNamespace(memory_note="今天在 B 站看了《A》", memory_bvid="BV1xx411c7mD")
        before = len(engine.calls)
        await plugin._maybe_remember(result, "webchat:FriendMessage:1")
        await plugin._maybe_remember(result, "webchat:FriendMessage:1")
        self.assertEqual(len(engine.calls) - before, 1)

    async def test_respects_daily_cap(self):
        engine = FakeEngine()
        _mod, plugin = make_plugin(engine, {"max_per_day": 1})
        plugin.config["memory"]["auto_write"] = True
        await plugin._maybe_remember(SimpleNamespace(memory_note="A", memory_bvid="BV1aa411c7mD"), "s")
        await plugin._maybe_remember(SimpleNamespace(memory_note="B", memory_bvid="BV1bb411c7mD"), "s")
        self.assertTrue(livingmemory.available(plugin.context))
        written = [call["metadata"]["bvid"] for call in engine.calls]
        self.assertEqual(written[-1:], ["BV1aa411c7mD"])
        self.assertEqual(len(written), 1, written)

    async def test_disabled_memory_skips(self):
        engine = FakeEngine()
        _mod, plugin = make_plugin(engine, {"enabled": False})
        await plugin._maybe_remember(SimpleNamespace(memory_note="A", memory_bvid="BV1aa411c7mD"), "s")
        self.assertEqual(engine.calls, [])

    async def test_without_livingmemory_is_silent(self):
        mod, plugin = make_plugin(None)
        out = await plugin._maybe_remember(
            SimpleNamespace(memory_note="A", memory_bvid="BV1aa411c7mD"), "s"
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
