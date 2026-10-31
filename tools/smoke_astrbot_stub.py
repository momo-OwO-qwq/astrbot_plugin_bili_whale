#!/usr/bin/env python3
"""不启动 AstrBot，用最小 stub 跑一遍 main.py。

用途：在没有安装 AstrBot 的开发机上，检查插件能否 import、指令能否生成回复、
配置文件能否保存。**不联网、不读真实 cookie**。

用法:
    python3 tools/smoke_astrbot_stub.py
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_stubs() -> None:
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
        def __init__(self, message_str: str, umo: str = "stub:FriendMessage:1", group: str = ""):
            self.message_str = message_str
            self.unified_msg_origin = umo
            self._group = group

        def plain_result(self, text: str):
            return ("plain", text)

        def get_group_id(self):
            return self._group

    class MessageChain:
        def __init__(self):
            self.parts = []

        def message(self, text: str):
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

    class _Meta:
        id = "webchat"
        name = "webchat"
        support_proactive_message = True

    class _Platform:
        def meta(self):
            return _Meta()

    class _PlatformManager:
        platform_insts = [_Platform()]

    class _PersonaManager:
        def get_persona_v3_by_id(self, persona_id):
            return {"name": persona_id, "prompt": "STUB_PERSONA"}

        async def get_default_persona_v3(self, umo=None):
            return {"name": "default", "prompt": "STUB_DEFAULT_PERSONA"}

        async def resolve_selected_persona(self, **kwargs):
            return None, {"name": "session", "prompt": "STUB_SESSION_PERSONA"}, None, False

    class Context:
        def __init__(self):
            self.sent = []
            self.platform_manager = _PlatformManager()
            self.persona_manager = _PersonaManager()

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain.parts))

        async def get_current_chat_provider_id(self, umo=None):
            return None

        def get_using_provider(self, umo=None):
            return None

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        return lambda cls: cls

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod


def load_plugin():
    pkg = types.ModuleType("astrbot_plugin_bili_whale")
    pkg.__path__ = [str(ROOT)]
    sys.modules["astrbot_plugin_bili_whale"] = pkg
    return importlib.import_module("astrbot_plugin_bili_whale.main")


async def collect(gen):
    return [item async for item in gen]


async def main() -> int:
    install_stubs()
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            mod = load_plugin()
            config = {
                "account": {"sessdata": "", "bili_jct": "", "buvid3": "", "owner_name": ""},
                "rhythm": {"enabled": False, "window": "20:00-24:00", "wakes_per_day": 1},
                "content": {"keywords": ["测试"]},
                "diary": {"enabled": True, "private_only": True},
                "share": {"mode": "off"},
                "push": {"session": "", "on_wake": True},
            }
            # main.py 里 import 的是 astrbot.api.star.Context，这里从 stub 取
            from astrbot.api.star import Context

            context = Context()
            plugin = mod.BiliWhalePlugin(context, mod.AstrBotConfig(config))
            event = mod.AstrMessageEvent("/bili 帮助")

            cases = ["/bili", "/bili 帮助", "/bili 额度", "/bili 状态", "/bili 日报", "/bili 日记"]
            for text in cases:
                event.message_str = text
                replies = await collect(plugin.bili(event))
                assert replies, f"{text} 没有回复"
                print(f"OK  {text:12s} -> {str(replies[0][1])[:60].replace(chr(10), ' / ')}")

            event.message_str = "/bili 订阅"
            replies = await collect(plugin.bili(event))
            assert "stub:FriendMessage:1" in plugin.config["push"]["targets"], replies
            assert plugin.config.get("_saved") is True

            event.message_str = "/bili 推送"
            replies = await collect(plugin.bili(event))
            assert replies and "推送目标" in replies[0][1], replies
            assert "webchat" in replies[0][1], replies

            persona = await plugin._resolve_persona("webchat:FriendMessage:1")
            assert persona == "STUB_SESSION_PERSONA", persona
            print("OK  persona ->", persona)

            # auto 模式下 /bili 分享 不应生成待确认项，而是直接走 confirm_share
            from astrbot_plugin_bili_whale.core.models import CommentPostResult

            class _FakeRunner:
                async def prepare_share(self, bvid):
                    return {
                        "bvid": bvid,
                        "aid": 1,
                        "title": "测试视频",
                        "score": 16,
                        "verdict": "值得看",
                        "reason": "它讲清楚了原理",
                        "comment": "@阿鲸 第三分钟那个例子很实在",
                        "created_at": "2026-10-31T00:00:00",
                    }

                async def confirm_share(self, payload):
                    return CommentPostResult(
                        rpid="12345", verified=True, visible_to_self=True, reason="公开可见"
                    )

            async def _fake_runner(umo=None):
                return _FakeRunner()

            plugin._make_runner = _fake_runner
            plugin.config["share"] = {
                "enabled": True,
                "auto_send": True,
                "auto_on_manual": True,
                "min_score": 13,
                "max_per_day": 3,
                "same_video_days": 7,
                "min_reason_len": 8,
                "max_chars": 40,
                "style_gate": True,
            }
            event.message_str = "/bili 分享 BV1xx411c7mD"
            replies = await collect(plugin.bili(event))
            assert any("自动发出" in str(item[1]) for item in replies), replies
            assert not plugin.store.get_pending_share(), "auto 模式不应留待确认"
            print("OK  auto-share ->", str(replies[-1][1])[:40])

            event.message_str = "/bili 分享设置"
            replies = await collect(plugin.bili(event))
            assert "自动发送（免确认）：开" in replies[0][1], replies
            print("OK  share-settings ->", replies[0][1].splitlines()[2])

            event.message_str = "/bili 分享设置 预览"
            replies = await collect(plugin.bili(event))
            assert "自动发送：关" in replies[0][1], replies
            event.message_str = "/bili 分享设置 自动"
            replies = await collect(plugin.bili(event))
            assert "自动发送：开" in replies[0][1], replies
            print("OK  share-settings toggle")

            for tool in ("bili_post_comment", "bili_send_dm", "bili_memorize"):
                assert hasattr(plugin, tool), tool
            denied = await plugin.bili_post_comment(event, "BV1xx411c7mD", "好看")
            assert "写操作默认关闭" in denied, denied
            print("OK  write-tools gated")

            # 无 cookie 时不应发网络请求；_push 走 stub 的 send_message
            ok = await plugin._push("测试推送")
            assert ok and context.sent

            await plugin.terminate()
            print("OK  terminate")
            return 0
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
