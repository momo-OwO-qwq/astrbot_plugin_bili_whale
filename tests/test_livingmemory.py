import unittest
from types import SimpleNamespace

from core import livingmemory


class FakeEngine:
    def __init__(self, fail: bool = False, return_id: int | None = 7):
        self.calls = []
        self.fail = fail
        self.return_id = return_id

    async def add_memory(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.calls.append(kwargs)
        return self.return_id


def context_with(engine=None, *, activated=True, with_plugin=True):
    stars = []
    if with_plugin:
        plugin = SimpleNamespace(initializer=SimpleNamespace(memory_engine=engine))
        stars.append(
            SimpleNamespace(
                name="astrbot_plugin_livingmemory",
                root_dir_name="astrbot_plugin_livingmemory",
                display_name="LivingMemory",
                activated=activated,
                star_cls=plugin if activated else None,
            )
        )
    return SimpleNamespace(get_all_stars=lambda: stars)


class LivingMemoryBridgeTest(unittest.IsolatedAsyncioTestCase):
    def test_find_plugin(self):
        ctx = context_with(FakeEngine())
        self.assertIsNotNone(livingmemory.find_livingmemory(ctx))
        self.assertTrue(livingmemory.available(ctx))
        self.assertEqual(livingmemory.status_text(ctx), "✓")

    def test_missing_plugin(self):
        ctx = context_with(with_plugin=False)
        self.assertIsNone(livingmemory.find_livingmemory(ctx))
        self.assertFalse(livingmemory.available(ctx))
        self.assertEqual(livingmemory.status_text(ctx), "未找到")

    def test_not_activated(self):
        ctx = context_with(FakeEngine(), activated=False)
        self.assertIsNone(livingmemory.find_livingmemory(ctx))

    def test_engine_missing(self):
        ctx = context_with(None)
        self.assertFalse(livingmemory.available(ctx))
        self.assertEqual(livingmemory.status_text(ctx), "已加载但未初始化")

    async def test_write_success(self):
        engine = FakeEngine()
        ctx = context_with(engine)
        out = await livingmemory.write_memory(
            ctx, content="今天看了《A》：很好", session_id="webchat:FriendMessage:1", importance=0.7
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["id"], 7)
        call = engine.calls[0]
        self.assertEqual(call["session_id"], "webchat:FriendMessage:1")
        self.assertEqual(call["metadata"]["source"], "astrbot_plugin_bili_whale")

    async def test_write_truncates(self):
        engine = FakeEngine()
        ctx = context_with(engine)
        await livingmemory.write_memory(
            ctx, content="字" * 2000, session_id="s", max_chars=100
        )
        self.assertLessEqual(len(engine.calls[0]["content"]), 100)

    async def test_write_empty(self):
        out = await livingmemory.write_memory(context_with(FakeEngine()), content="  ", session_id="s")
        self.assertFalse(out["ok"])

    async def test_write_without_plugin(self):
        out = await livingmemory.write_memory(
            context_with(with_plugin=False), content="今天看了《A》", session_id="s"
        )
        self.assertFalse(out["ok"])
        self.assertIn("LivingMemory", out["reason"])

    async def test_write_failure_is_swallowed(self):
        ctx = context_with(FakeEngine(fail=True))
        out = await livingmemory.write_memory(ctx, content="今天看了《A》", session_id="s")
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["reason"])


if __name__ == "__main__":
    unittest.main()
