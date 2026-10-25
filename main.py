"""AstrBot 插件入口：指令、主动推送、模型调用。

内核在 ``core/`` 里，这里只做三件事：
1. 把 AstrBot 的配置 / 模型 / 消息接口接到内核上；
2. 注册指令与一个 LLM Tool；
3. 用 asyncio 定时器驱动"生物钟"（不依赖 AstrBot 的调度器）。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:  # 正常情况：插件作为包加载，使用相对导入。
    from .core import planner, quota as quota_mod
    from .core.bili_api import BiliAPI
    from .core.errors import BiliAuthError, BiliError, BiliRisk, BudgetError
    from .core.report import build_dm_text
    from .core.runner import Runner
    from .core.store import Store
    from .core.traffic import TrafficGuard
    from .core.util import (
        as_bool,
        as_float,
        as_int,
        as_str_list,
        cfg_get,
        chunk_text,
        clean_text,
        extract_bvid,
        now_local,
        today_str,
    )
except ImportError:  # 兼容"以单文件方式"加载 main.py 的宿主
    import sys

    _here = str(Path(__file__).resolve().parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from core import planner, quota as quota_mod
    from core.bili_api import BiliAPI
    from core.errors import BiliAuthError, BiliError, BiliRisk, BudgetError
    from core.report import build_dm_text
    from core.runner import Runner
    from core.store import Store
    from core.traffic import TrafficGuard
    from core.util import (
        as_bool,
        as_float,
        as_int,
        as_str_list,
        cfg_get,
        chunk_text,
        clean_text,
        extract_bvid,
        now_local,
        today_str,
    )

PLUGIN_NAME = "astrbot_plugin_bili_whale"
VERSION = "0.2.0"

HELP_TEXT = """🐟 养鱼刷B站 —— 指令

/bili 状态          看账号、作息、今天的计划、额度
/bili 现在刷        立刻刷一轮（不占用生物钟次数）
/bili 看 BV1xx...   看懂一条视频（字幕+热评+弹幕）
/bili 搜 关键词     搜几条视频
/bili 日报 [日期]   看日报（默认今天）
/bili 日记 [日期]   看两本日记（私聊里更安全）
/bili 订阅          把当前会话加入 AstrBot 推送目标
/bili 退订          把当前会话从推送目标里移除
/bili 推送          看 AstrBot 已配置平台 + 当前推送目标
/bili 体检          登录态 / 依赖 / 模型 / 配额自检
/bili 额度          看今天读/写/分享额度与熔断状态
/bili 分享 BV...    生成一条 @ 你的评论预览（不会直接发）
/bili 确认          发送上一条预览
/bili 取消          取消待确认的分享
/bili 重载          重连 B 站会话（改完 cookie 用）
/bili 重置计划      清掉今天的计划，按新作息重排

自动刷由 WebUI 插件配置里的「作息窗口」驱动；
每个窗口里随机一个时刻醒来，有概率今天不看，但日记照写。
分享默认关闭；打开后也只在你确认或「自动分享」模式下才会发。"""


def _data_dir() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    except Exception:
        base = Path("data") / "plugin_data" / PLUGIN_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _command_tail(message: str) -> str:
    """兼容 AstrBot 不同版本：message_str 可能带指令，也可能已经被剥离。"""
    text = (message or "").strip()
    text = re.sub(r"^[/!！]", "", text).strip()
    if not text:
        return ""
    first, _, rest = text.partition(" ")
    command_name = first.lower().split("@", 1)[0]
    if command_name in {"bili", "b站", "养鱼", "刷b站"}:
        return rest.strip()
    return text


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, BiliAuthError):
        return f"🔑 {exc}\n去 WebUI → 插件配置 → B 站账号，重新填 SESSDATA / bili_jct / buvid3。"
    if isinstance(exc, BiliRisk):
        return f"⛔ {exc}\n已自动熔断，别连续重试。"
    if isinstance(exc, BudgetError):
        return f"🛑 {exc}"
    if isinstance(exc, BiliError):
        return f"❌ {exc}"
    logger.exception("bili-whale command failed")
    return f"❌ 出了个内部错误（{type(exc).__name__}），详情看 AstrBot 日志。"


@register(PLUGIN_NAME, "c1overzz & Alterssss", "养鱼刷B站：生物钟 + 真读字幕 + 日报/日记 + 克制的分享", VERSION)
class BiliWhalePlugin(Star):
    """养鱼刷B站：按生物钟替你刷 B 站，读字幕、写日报和两本日记。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.data_dir = _data_dir()
        self.store = Store(self.data_dir)
        self._api: BiliAPI | None = None
        self._cookie_snapshot: dict[str, str] = {}
        self._run_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._ticker: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._warned_no_cookie = False
        self._bad_window_warned = False
        self._start_ticker()

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def _start_ticker(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # AstrBot 在异步上下文里实例化插件；真遇到同步实例化就只保留手动指令。
            logger.warning("[bili-whale] 没有运行中的事件循环，自动生物钟未启动")
            return
        self._ticker = loop.create_task(self._ticker_loop(), name="bili-whale-ticker")

    async def _ticker_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[bili-whale] tick failed")
            await asyncio.sleep(45)

    async def initialize(self) -> None:
        """AstrBot 实例化完成后调用；迟到的定时器在这里补起。"""
        if (self._ticker is None or self._ticker.done()) and as_bool(
            cfg_get(self.config, "rhythm.enabled", True), True
        ):
            self._start_ticker()

    async def terminate(self) -> None:
        """插件卸载/停用时收尾，避免定时器和 aiohttp 会话泄漏。"""
        ticker, self._ticker = self._ticker, None
        if ticker:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        self._tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._reset_api()

    # ── 生物钟 ───────────────────────────────────────────────────────────
    async def _tick(self) -> None:
        if not as_bool(cfg_get(self.config, "rhythm.enabled", True), True):
            return
        if not self._cookie_dict().get("sessdata"):
            if not self._warned_no_cookie:
                logger.warning("[bili-whale] 还没填 B 站 cookie，生物钟空转；填好后会自动开始")
                self._warned_no_cookie = True
            return
        self._warned_no_cookie = False

        now = now_local()
        spec = str(cfg_get(self.config, "rhythm.window", "20:00-24:00") or "20:00-24:00")
        wakes = as_int(cfg_get(self.config, "rhythm.wakes_per_day", 1), 1, 0, 12)
        state = self.store.state
        plan = state.get("plan") or {}

        # 用户改了作息窗口：旧计划如果属于一个已经不存在的窗口，就作废重排，
        # 免得"明明改到 22 点，它却按昨天的 20 点醒来"。
        try:
            labels = {window.label for window in planner.parse_windows(spec)}
        except ValueError as exc:
            if not self._bad_window_warned:
                logger.error("[bili-whale] 作息窗口配置有误：%s", exc)
                self._bad_window_warned = True
            return
        if (
            plan
            and not plan.get("fired")
            and plan.get("window")
            and labels
            and plan.get("window") not in labels
        ):
            logger.info("[bili-whale] 作息窗口已变，旧计划作废：%s", plan.get("window"))
            state["plan"] = {}
            plan = {}

        if plan.get("action") == "done" and plan.get("date") == today_str(now):
            return
        if planner.is_pending(plan, now) and not planner.is_due(plan, now):
            return

        if plan and not plan.get("fired"):
            if planner.is_expired(plan, now):
                planner.complete_plan(state, ran=False, now=now)
                self.store.save()
                logger.warning("[bili-whale] 计划时间已过（插件当时没运行？），跳过这一次")
                return
            if planner.is_due(plan, now):
                if self._run_lock.locked():
                    return
                await self._execute_plan(plan)
                return
            return

        try:
            new_plan = planner.make_plan(
                state,
                spec,
                wakes_per_day=wakes,
                skip_chance=as_float(cfg_get(self.config, "rhythm.skip_chance", 0.3), 0.3, 0.0, 1.0),
                min_gap_min=as_int(cfg_get(self.config, "rhythm.min_gap_min", 10), 10, 1, 720),
                now=now,
            )
        except ValueError as exc:
            if not self._bad_window_warned:
                logger.error("[bili-whale] 作息窗口配置有误：%s", exc)
                self._bad_window_warned = True
            return
        self._bad_window_warned = False
        self.store.save()
        if new_plan.get("action") in {"wake", "skip"}:
            logger.info(
                "[bili-whale] 下一次：%s（%s）",
                new_plan.get("at"),
                "刷一轮" if new_plan.get("action") == "wake" else "今天不想看",
            )

    async def _execute_plan(self, plan: dict[str, Any]) -> None:
        async with self._run_lock:
            try:
                runner = await self._make_runner()
                if plan.get("action") == "skip":
                    result = await runner.run_skip("这一次是生物钟掷到的「今天不想看」。")
                else:
                    result = await runner.run(allow_share=True)
            except Exception as exc:  # 计划必须收尾，否则每 45 秒重试一次
                logger.exception("[bili-whale] 到点执行失败")
                result = None
                message = _err_text(exc)
            finally:
                planner.complete_plan(self.store.state, ran=True)
                # 同一天还有次数就继续排下一次
                try:
                    spec = str(cfg_get(self.config, "rhythm.window", "20:00-24:00") or "20:00-24:00")
                    planner.make_plan(
                        self.store.state,
                        spec,
                        wakes_per_day=as_int(cfg_get(self.config, "rhythm.wakes_per_day", 1), 1, 0, 12),
                        skip_chance=as_float(cfg_get(self.config, "rhythm.skip_chance", 0.3), 0.3, 0.0, 1.0),
                        min_gap_min=as_int(cfg_get(self.config, "rhythm.min_gap_min", 10), 10, 1, 720),
                    )
                except Exception:
                    logger.exception("[bili-whale] 排下一次计划失败")
                self.store.save()
        if result is not None:
            await self._push_result(result)
        else:
            await self._push(message)

    async def _push_result(self, result: Any, manual_umo: str | None = None) -> None:
        """把一轮结果发到 AstrBot 目标，并按配置发 B 站私信。

        - AstrBot：手动跑只回到触发会话；到点跑发给 push.targets 里所有目标
          （旧的 push.session 仍兼容；一个都没配时退回最后一次会话）。
        - B 站私信：独立开关，只发给 owner；默认只在到点跑时发。
        """
        manual = bool(manual_umo)
        if manual:
            targets = [manual_umo] if manual_umo else []
        elif as_bool(cfg_get(self.config, "push.on_wake", True), True):
            targets = self._target_umos()
        else:
            targets = []

        if targets:
            for target in targets:
                await self._push(result.summary, umo=target)
                if as_bool(cfg_get(self.config, "report.push_full", False), False) and result.report_path:
                    try:
                        text = Path(result.report_path).read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                    if text:
                        await self._push(text, umo=target, max_chunks=8)
                if as_bool(cfg_get(self.config, "diary.push", False), False):
                    for kind in ("owner", "self"):
                        text = self.store.read_diary(kind)
                        if text:
                            title = "给主人的信" if kind == "owner" else "自己的日记"
                            await self._push(f"📔 {title}\n\n{text}", umo=target, max_chunks=4)
        elif not manual and as_bool(cfg_get(self.config, "push.on_wake", True), True):
            logger.warning("[bili-whale] 没有 AstrBot 推送目标：在目标会话发 /bili 订阅，或在配置里填 push.targets")

        await self._push_bili_dm(result, manual=manual)

    async def _push_bili_dm(self, result: Any, manual: bool = False) -> None:
        """B 站私信推送：只发给 owner，默认不参与手动刷。"""
        if not as_bool(cfg_get(self.config, "push.bili_dm.enabled", False), False):
            return
        if manual and not as_bool(cfg_get(self.config, "push.bili_dm.on_manual", False), False):
            return
        to_mid = str(
            cfg_get(self.config, "push.bili_dm.to_mid", "")
            or cfg_get(self.config, "account.owner_mid", "")
        ).strip()
        if not to_mid:
            logger.warning("[bili-whale] B 站私信推送已开启，但没填 owner_mid / push.bili_dm.to_mid")
            return
        limits = quota_mod.Limits.from_config(self.config)
        decision = quota_mod.can_dm(self.store.quota, limits)
        if not decision:
            logger.info("[bili-whale] 跳过 B 站私信推送：%s", decision.reason)
            return
        try:
            api = await self._ensure_api()
            await api.send_dm(int(to_mid), build_dm_text(result))
            quota_mod.record_dm(self.store.quota)
            self.store.save()
            logger.info("[bili-whale] 已把摘要私信推送给 mid %s", to_mid)
        except BiliRisk as exc:
            logger.warning("[bili-whale] B 站私信被限流：%s", exc)
        except BiliError as exc:
            logger.warning("[bili-whale] B 站私信没发出去：%s", exc)
        except Exception:
            logger.exception("[bili-whale] B 站私信推送失败")

    # ── AstrBot 接入：配置 / 模型 / 推送 ─────────────────────────────────
    def _cookie_dict(self) -> dict[str, str]:
        return {
            "sessdata": str(cfg_get(self.config, "account.sessdata", "") or "").strip(),
            "bili_jct": str(cfg_get(self.config, "account.bili_jct", "") or "").strip(),
            "buvid3": str(cfg_get(self.config, "account.buvid3", "") or "").strip(),
        }

    async def _ensure_api(self) -> BiliAPI:
        cookie = self._cookie_dict()
        async with self._api_lock:
            if self._api is not None and cookie == self._cookie_snapshot:
                return self._api
            if self._api is not None:
                old, self._api = self._api, None
                self._cookie_snapshot = {}
                try:
                    await old.close()
                except Exception:
                    logger.warning("[bili-whale] 关闭旧网络会话时出错", exc_info=True)
            guard = TrafficGuard(self.store, lambda: self.config)
            dev_id = str(self.store.get("bili_dev_id") or "").strip()
            if not dev_id:
                dev_id = "WEB-" + str(uuid.uuid4()).upper()
                self.store.set("bili_dev_id", dev_id)
            api = BiliAPI(cookie, guard, logger, dev_id=dev_id)
            await api.start()
            self._api = api
            self._cookie_snapshot = cookie
            return api

    async def _reset_api(self) -> None:
        async with self._api_lock:
            api, self._api = self._api, None
            self._cookie_snapshot = {}
        if api is not None:
            try:
                await api.close()
            except Exception:
                logger.warning("[bili-whale] 关闭网络会话时出错", exc_info=True)

    async def _make_runner(self, umo: str | None = None) -> Runner:
        api = await self._ensure_api()
        persona = await self._resolve_persona(umo or self._primary_umo())
        return Runner(
            self.config,
            api,
            self.store,
            chat=self._chat,
            logger=logger,
            persona_text=persona,
        )

    def _configured_targets(self) -> list[str]:
        """AstrBot 推送目标：push.targets + 旧版 push.session。"""
        raw = as_str_list(cfg_get(self.config, "push.targets", []))
        legacy = str(cfg_get(self.config, "push.session", "") or "").strip()
        if legacy:
            raw.append(legacy)
        out: list[str] = []
        for item in raw:
            target = str(item or "").strip()
            # UMO 形如 aiocqhttp:FriendMessage:12345；session_id 里允许有冒号。
            if target and target.count(":") >= 2 and target not in out:
                out.append(target)
        return out

    def _target_umos(self) -> list[str]:
        targets = self._configured_targets()
        if targets:
            return targets
        last = str(self.store.get("last_umo") or "").strip()
        return [last] if last else []

    def _primary_umo(self) -> str:
        targets = self._configured_targets()
        return targets[0] if targets else str(self.store.get("last_umo") or "")

    async def _resolve_persona(self, umo: str | None) -> str:
        """从 AstrBot 人格系统取当前生效的人格；失败时退回插件自定义人设。"""
        custom = clean_text(cfg_get(self.config, "persona.text", ""), limit=3000, keep_newlines=True)
        source = str(cfg_get(self.config, "persona.source", "astrbot") or "astrbot").strip().lower()
        if source == "custom":
            return custom
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return custom
        persona = None
        try:
            persona_id = str(cfg_get(self.config, "persona.persona_id", "") or "").strip()
            if persona_id:
                getter = getattr(manager, "get_persona_v3_by_id", None)
                persona = getter(persona_id) if callable(getter) else None
            elif umo:
                resolver = getattr(manager, "resolve_selected_persona", None)
                if callable(resolver):
                    try:
                        _, persona, _, _ = await resolver(
                            umo=umo,
                            conversation_persona_id=None,
                            platform_name=umo.split(":", 1)[0],
                            provider_settings=None,
                        )
                    except Exception:
                        persona = None
            if not persona:
                default_getter = getattr(manager, "get_default_persona_v3", None)
                if callable(default_getter):
                    persona = await default_getter(umo or None)
            prompt = persona.get("prompt") if isinstance(persona, dict) else ""
            prompt = clean_text(prompt, limit=4000, keep_newlines=True)
            if prompt:
                logger.debug(
                    "[bili-whale] 使用 AstrBot 人格：%s（%s 字）",
                    persona.get("name") if isinstance(persona, dict) else "?",
                    len(prompt),
                )
                return prompt
        except Exception:
            logger.warning("[bili-whale] 读取 AstrBot 人格失败，退回自定义人设", exc_info=True)
        return custom

    async def _chat(self, system: str, prompt: str) -> str | None:
        """优先用 AstrBot 的 llm_generate；失败再退回 provider.text_chat。"""
        umo = self._primary_umo() or None
        provider_id = None
        try:
            if umo and hasattr(self.context, "get_current_chat_provider_id"):
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        except Exception:
            provider_id = None
        if provider_id and hasattr(self.context, "llm_generate"):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt, system_prompt=system
                )
                text = getattr(resp, "completion_text", None) or getattr(resp, "text", None)
                if text:
                    return str(text)
            except TypeError:
                # 老版本 llm_generate 不认 system_prompt
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id, prompt=f"{system}\n\n{prompt}"
                    )
                    text = getattr(resp, "completion_text", None) or getattr(resp, "text", None)
                    if text:
                        return str(text)
                except Exception as exc:
                    logger.warning("[bili-whale] llm_generate 失败：%s", exc)
            except Exception as exc:
                logger.warning("[bili-whale] llm_generate 失败：%s", exc)

        provider = await self._get_provider(umo)
        if provider is None:
            return None
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                system_prompt=system,
                session_id=umo or "bili-whale",
            )
            text = getattr(resp, "completion_text", None) or getattr(resp, "text", None)
            return str(text) if text else None
        except TypeError:
            try:
                resp = await provider.text_chat(
                    prompt=f"{system}\n\n{prompt}", session_id=umo or "bili-whale"
                )
                text = getattr(resp, "completion_text", None) or getattr(resp, "text", None)
                return str(text) if text else None
            except Exception as exc:
                logger.warning("[bili-whale] provider.text_chat 失败：%s", exc)
        except Exception as exc:
            logger.warning("[bili-whale] provider.text_chat 失败：%s", exc)
        return None

    async def _provider_hint(self) -> str:
        umo = self._primary_umo() or None
        try:
            if umo and hasattr(self.context, "get_current_chat_provider_id"):
                pid = await self.context.get_current_chat_provider_id(umo=umo)
                if pid:
                    return f"✓ {pid}"
        except Exception:
            pass
        provider = await self._get_provider(umo)
        if provider is not None:
            meta = getattr(provider, "meta", None)
            ident = getattr(meta, "id", None) if not callable(meta) else None
            return f"✓ {ident or getattr(provider, 'provider_id', '已配置')}"
        return "✗ 没取到（检查 AstrBot 的「服务提供商」）"

    async def _get_provider(self, umo: str | None) -> Any:
        """新版本用 async 接口；老版本退回已弃用的同步接口。"""
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                return await getter(umo=umo)
            except TypeError:
                try:
                    return await getter(umo)
                except Exception:
                    return None
            except Exception:
                return None
        try:
            return self.context.get_using_provider(umo=umo) if umo else self.context.get_using_provider()
        except TypeError:
            try:
                return self.context.get_using_provider()
            except Exception:
                return None
        except Exception:
            return None

    async def _push(self, text: str, umo: str | None = None, max_chunks: int = 10) -> bool:
        if not text:
            return False
        target = umo or self._primary_umo()
        if not target:
            logger.warning("[bili-whale] 没有可用的会话，消息发不出去：%s", text[:80])
            return False
        ok = True
        for chunk in chunk_text(text, size=1500, max_chunks=max_chunks):
            try:
                await self.context.send_message(target, MessageChain().message(chunk))
            except Exception:
                logger.exception("[bili-whale] 推送失败")
                ok = False
                break
        return ok

    def _save_config(self) -> None:
        try:
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
        except Exception:
            logger.warning("[bili-whale] 保存配置失败", exc_info=True)

    def _push_config(self) -> dict[str, Any]:
        push = self.config.get("push")
        if not isinstance(push, dict):
            push = {}
            self.config["push"] = push
        if not isinstance(push.get("targets"), list):
            push["targets"] = []
        return push

    def _targets_text(self) -> str:
        lines = ["📡 AstrBot 已配置的平台"]
        try:
            platforms = list(self.context.platform_manager.platform_insts)
        except Exception:
            platforms = []
        if not platforms:
            lines.append("（没读到平台适配器；确认 AstrBot 里至少启用了一个消息平台）")
        else:
            for platform in platforms:
                try:
                    meta = platform.meta()
                except Exception:
                    continue
                proactive = getattr(meta, "support_proactive_message", True)
                lines.append(
                    f"- {getattr(meta, 'id', '?')} ｜ {getattr(meta, 'name', '?')} ｜ "
                    f"主动推送：{'✓' if proactive else '✗'}"
                )
        targets = self._configured_targets()
        lines.extend(["", f"📌 当前推送目标（{len(targets)} 个）"])
        if targets:
            lines.extend(f"- {target}" for target in targets)
        else:
            last = str(self.store.get("last_umo") or "").strip()
            lines.append("- （没有配置；将退回最后一次会话" + (f"：{last}）" if last else "）"))
        dm_enabled = as_bool(cfg_get(self.config, "push.bili_dm.enabled", False), False)
        dm_to = str(
            cfg_get(self.config, "push.bili_dm.to_mid", "")
            or cfg_get(self.config, "account.owner_mid", "")
        ).strip()
        lines.extend(
            [
                "",
                f"B站私信推送：{'开' if dm_enabled else '关'}"
                + (f"（收件人 mid {dm_to}）" if dm_enabled and dm_to else ""),
                "在目标会话发 /bili 订阅 即可加入；/bili 退订 移出当前会话。",
            ]
        )
        return "\n".join(lines)

    # ── 指令 ─────────────────────────────────────────────────────────────
    @filter.command("bili")
    async def bili(self, event: AstrMessageEvent):
        """养鱼刷B站：状态 / 刷一轮 / 看懂视频 / 日报 / 日记 / 配额 / 分享。"""
        if (self._ticker is None or self._ticker.done()) and as_bool(
            cfg_get(self.config, "rhythm.enabled", True), True
        ):
            # 兼容"插件在同步上下文里被实例化"的情况：第一次收到指令时补起定时器。
            self._start_ticker()
        tail = _command_tail(event.message_str)
        umo = event.unified_msg_origin
        if not tail or tail.lower() in {"帮助", "help", "?", "菜单"}:
            yield event.plain_result(HELP_TEXT)
            return
        sub, _, arg = tail.partition(" ")
        sub = sub.strip()
        arg = arg.strip()
        lower = sub.lower()

        # 会动账号 / 花钱 / 改推送目标的动作：群聊里只让 AstrBot 管理员来。
        manage_only = {
            "现在刷", "刷", "run", "刷一轮",
            "订阅", "subscribe", "退订", "取消订阅", "unsubscribe",
            "体检", "自检", "check", "诊断",
            "推送", "推送平台", "platforms", "targets",
            "分享", "share", "确认", "confirm", "取消", "cancel",
            "重载", "reload", "状态", "status", "情况",
            "重置计划", "重排", "replan",
        }
        if lower in manage_only and not self._can_manage(event):
            yield event.plain_result("🔒 这个操作会动账号 / 花钱 / 改推送，群聊里只允许管理员用；私聊可以直接用。")
            return
        # 只有私聊或管理员在群里的操作才更新"默认推送会话"，
        # 免得群里随便一个人发条查询就把主动推送改到他那儿。
        if not event.get_group_id() or self._can_manage(event):
            self.store.record_umo(umo)

        if lower in {"状态", "status", "情况"}:
            yield event.plain_result(await self._status_text())
            return

        if lower in {"现在刷", "刷", "run", "刷一轮"}:
            if self._run_lock.locked():
                yield event.plain_result("🐟 已经在刷了，等这一轮结束。")
                return
            task = asyncio.create_task(self._manual_run(umo))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            yield event.plain_result("🐟 开始了，这一轮可能要几分钟；刷完把结果发这里。")
            return

        if lower in {"看", "看懂", "视频", "digest", "读"}:
            bvid = extract_bvid(arg)
            if not bvid:
                yield event.plain_result("用法：/bili 看 BV1xx...")
                return
            yield event.plain_result(f"🐟 正在读 {bvid}（字幕 + 热评 + 弹幕）…")
            try:
                runner = await self._make_runner(umo)
                text, _ = await runner.digest(bvid)
                for chunk in chunk_text(text, size=1500, max_chunks=6):
                    yield event.plain_result(chunk)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
            return

        if lower in {"搜", "搜索", "search"}:
            if not arg:
                yield event.plain_result("用法：/bili 搜 关键词")
                return
            try:
                runner = await self._make_runner(umo)
                videos = await runner.search(arg)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
                return
            if not videos:
                yield event.plain_result("没搜到。")
                return
            lines = [f"🔍 「{arg}」Top {min(10, len(videos))}："]
            for idx, video in enumerate(videos[:10], 1):
                from .core.util import human_count, human_duration

                lines.append(
                    f"{idx}. {video.title}\n   {video.bvid} · UP {video.author} · "
                    f"{human_count(video.play)} 播放 · {human_duration(video.duration)}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if lower in {"日报", "报告", "report"}:
            text = self._report_text(arg)
            if not text:
                available = self.store.list_reports(8)
                hint = "最近有：" + "、".join(available) if available else "还没有日报，先 /bili 现在刷。"
                yield event.plain_result(f"这一天没有日报。{hint}")
                return
            for chunk in chunk_text(text, size=1500, max_chunks=10):
                yield event.plain_result(chunk)
            return

        if lower in {"日记", "diary"}:
            if event.get_group_id() and as_bool(cfg_get(self.config, "diary.private_only", True), True):
                yield event.plain_result("🔒 日记只在私聊里看（群聊容易漏隐私）。")
                return
            text = self._diary_text(arg)
            if not text:
                yield event.plain_result("这一天还没有日记。")
                return
            for chunk in chunk_text(text, size=1500, max_chunks=8):
                yield event.plain_result(chunk)
            return

        if lower in {"订阅", "subscribe"}:
            push = self._push_config()
            targets = as_str_list(push.get("targets", []))
            if umo not in targets:
                targets.append(umo)
            push["targets"] = targets
            push["on_wake"] = True
            self._save_config()
            yield event.plain_result(
                f"✓ 已把当前会话加入 AstrBot 推送目标（共 {len(self._configured_targets())} 个）。"
                "到点刷完会发到所有目标。"
            )
            return

        if lower in {"退订", "取消订阅", "unsubscribe"}:
            push = self._push_config()
            targets = [item for item in as_str_list(push.get("targets", [])) if item != umo]
            push["targets"] = targets
            if str(push.get("session") or "").strip() == umo:
                push["session"] = ""
            if not targets and not str(push.get("session") or "").strip():
                push["on_wake"] = False
            if str(self.store.get("last_umo") or "") == umo:
                self.store.set("last_umo", "")
            self._save_config()
            yield event.plain_result(
                f"✓ 已把当前会话移出推送目标（还剩 {len(self._configured_targets())} 个）。"
                "没有目标时不会再主动推。"
            )
            return

        if lower in {"推送", "推送平台", "platforms", "targets"}:
            yield event.plain_result(self._targets_text())
            return

        if lower in {"体检", "自检", "check", "诊断"}:
            yield event.plain_result(await self._health_text())
            return

        if lower in {"额度", "quota"}:
            guard = TrafficGuard(self.store, lambda: self.config)
            yield event.plain_result("📊 " + guard.status_text())
            return

        if lower in {"分享", "share"}:
            if not quota_mod.share_config_enabled(self.config):
                yield event.plain_result("分享总开关是关的（share.mode = off）。要用先在插件配置里改成 manual 或 auto。")
                return
            bvid = extract_bvid(arg)
            if not bvid:
                yield event.plain_result("用法：/bili 分享 BV1xx...")
                return
            yield event.plain_result(f"🐟 正在为 {bvid} 写分享评论…")
            try:
                runner = await self._make_runner(umo)
                payload = await runner.prepare_share(bvid)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
                return
            payload["umo"] = umo
            self.store.set_pending_share(payload)
            yield event.plain_result(
                "将发这条公开评论（30 分钟内有效）：\n\n"
                f"{payload['comment']}\n\n"
                f"评分 {payload['score']}/20 · {payload['verdict']}\n"
                "确认：/bili 确认\n取消：/bili 取消"
            )
            return

        if lower in {"确认", "confirm"}:
            pending = self.store.get_pending_share()
            if not pending:
                yield event.plain_result("没有待确认的分享（或者已经超过 30 分钟）。")
                return
            if pending.get("umo") and pending.get("umo") != umo:
                yield event.plain_result("这条分享是在别的会话发起的，回那个会话确认。")
                return
            try:
                runner = await self._make_runner(umo)
                rpid = await runner.confirm_share(pending)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
                return
            where = f"（rpid {rpid}）" if rpid else ""
            yield event.plain_result(f"✓ 已发{where}：https://www.bilibili.com/video/{pending['bvid']}/")
            return

        if lower in {"取消", "cancel"}:
            self.store.clear_pending_share()
            yield event.plain_result("✓ 已取消。")
            return

        if lower in {"重置计划", "重排", "replan"}:
            if self._run_lock.locked():
                yield event.plain_result("🐟 正在刷，等这一轮结束再重排。")
                return
            self.store.state["plan"] = {}
            self.store.save()
            yield event.plain_result("✓ 今天的计划已清空；下一次 tick 会按当前作息窗口重排。")
            return

        if lower in {"重载", "reload"}:
            if self._run_lock.locked():
                yield event.plain_result("🐟 正在刷，等这一轮结束再重载。")
                return
            await self._reset_api()
            yield event.plain_result("✓ 已重连 B 站会话；改过 cookie 的话，下一条指令就会用新的。")
            return

        # /bili BV1xx... 直接当"看懂"
        bvid = extract_bvid(tail)
        if bvid:
            yield event.plain_result(f"🐟 正在读 {bvid}…")
            try:
                runner = await self._make_runner(umo)
                text, _ = await runner.digest(bvid)
                for chunk in chunk_text(text, size=1500, max_chunks=6):
                    yield event.plain_result(chunk)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
            return

        yield event.plain_result(f"没看懂这个子命令：{sub}\n\n" + HELP_TEXT)

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        """群聊里的敏感操作只给管理员；私聊默认放行。"""
        if not as_bool(cfg_get(self.config, "security.admin_only", True), True):
            return True
        if not event.get_group_id():
            return True
        role = getattr(event, "role", None)
        if isinstance(role, str):
            return role == "admin"
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    # ── 一个 LLM Tool：让聊天里的 agent 也能"看懂一条视频" ───────────────
    @filter.llm_tool(name="bili_video_digest")
    async def bili_video_digest(self, event: AstrMessageEvent, bvid: str) -> str:
        """看懂一条 B 站视频：读官方字幕、热评和弹幕热区，然后总结。

        Args:
            bvid(string): B 站 BV 号，例如 BV1xx411c7mD
        """
        code = extract_bvid(bvid)
        if not code:
            return "这不像一个 BV 号。"
        if not event.get_group_id() or self._can_manage(event):
            self.store.record_umo(event.unified_msg_origin)
        try:
            runner = await self._make_runner(event.unified_msg_origin)
            text, _ = await runner.digest(code)
            return text
        except Exception as exc:
            return _err_text(exc)

    # ── 指令实现细节 ─────────────────────────────────────────────────────
    async def _manual_run(self, umo: str) -> None:
        async with self._run_lock:
            try:
                runner = await self._make_runner(umo)
                result = await runner.run(allow_share=False)
            except Exception as exc:
                await self._push(_err_text(exc), umo=umo)
                return
        if quota_mod.share_auto_enabled(self.config) and not result.shared:
            result.summary += "\n（手动刷默认不自动发评论；想分享用 /bili 分享 BV…）"
        await self._push_result(result, manual_umo=umo)

    async def _status_text(self) -> str:
        now = now_local()
        lines = ["🐟 养鱼刷B站", ""]
        cookie = self._cookie_dict()
        if cookie.get("sessdata"):
            try:
                api = await self._ensure_api()
                info = await api.login_info()
                if info.get("is_login"):
                    lines.append(f"账号：✓ {info.get('uname')}（mid {info.get('mid')}）")
                    owner_mid = str(cfg_get(self.config, "account.owner_mid", "") or "").strip()
                    if owner_mid and str(info.get("mid")) != owner_mid:
                        lines.append(f"⚠️ 登录的 UID 和配置的 owner_mid（{owner_mid}）不一致")
                else:
                    lines.append("账号：✗ cookie 在，但 B 站说未登录（可能过期了）")
            except Exception as exc:
                lines.append(f"账号：✗ 检查失败（{exc}）")
        else:
            lines.append("账号：✗ 还没填 cookie")
        try:
            spec = str(cfg_get(self.config, "rhythm.window", "20:00-24:00") or "20:00-24:00")
            wakes = as_int(cfg_get(self.config, "rhythm.wakes_per_day", 1), 1, 0, 12)
            lines.append(planner.plan_status_text(self.store.state, spec, wakes, now))
        except Exception as exc:
            lines.append(f"作息：✗ 配置有误（{exc}）")
        guard = TrafficGuard(self.store, lambda: self.config)
        lines.extend(["", guard.status_text()])
        lines.append(f"模型：{await self._provider_hint()}")
        lines.append(f"分享模式：{cfg_get(self.config, 'share.mode', 'off')}")
        targets = self._configured_targets()
        lines.append(f"AstrBot 推送目标：{len(targets)} 个" + (f"（{targets[0]}）" if targets else ""))
        source = str(cfg_get(self.config, "persona.source", "astrbot") or "astrbot")
        persona_id = str(cfg_get(self.config, "persona.persona_id", "") or "").strip()
        lines.append(
            "人格源："
            + ("AstrBot" + (f"（{persona_id}）" if persona_id else "（跟随会话/默认）") if source == "astrbot" else "插件自定义")
        )
        dm_enabled = as_bool(cfg_get(self.config, "push.bili_dm.enabled", False), False)
        dm_to = str(
            cfg_get(self.config, "push.bili_dm.to_mid", "") or cfg_get(self.config, "account.owner_mid", "")
        ).strip()
        lines.append(f"B站私信推送：{'开' if dm_enabled else '关'}" + (f"（mid {dm_to}）" if dm_enabled and dm_to else ""))
        reports = self.store.list_reports(3)
        if reports:
            lines.append("最近日报：" + "、".join(reports))
        lines.append("数据目录：" + str(self.data_dir))
        return "\n".join(lines)

    async def _health_text(self) -> str:
        lines = ["🩺 体检", ""]
        cookie = self._cookie_dict()
        lines.append("cookie：" + ("✓ 已填三个字段" if all(cookie.values()) else "✗ 缺字段（SESSDATA / bili_jct / buvid3）"))
        if cookie.get("sessdata"):
            try:
                api = await self._ensure_api()
                info = await api.login_info()
                if info.get("is_login"):
                    lines.append(f"登录态：✓ {info.get('uname')}（mid {info.get('mid')}）")
                    owner_mid = str(cfg_get(self.config, "account.owner_mid", "") or "").strip()
                    if owner_mid and str(info.get("mid")) != owner_mid:
                        lines.append(f"⚠️ 登录的 UID 和配置的 owner_mid（{owner_mid}）不一致")
                else:
                    lines.append("登录态：✗ 未登录（cookie 过期 / 复制不完整）")
                lines.append("发评论权限：" + ("✓ 有 bili_jct" if api.has_write_cookie else "✗ 缺 bili_jct"))
            except Exception as exc:
                lines.append(f"B 站接口：✗ {exc}")
        else:
            lines.append("B 站接口：未检查（先填 cookie）")
        lines.append(f"模型：{await self._provider_hint()}")
        lines.append(f"AstrBot 推送目标：{len(self._configured_targets())} 个")
        try:
            platforms = list(self.context.platform_manager.platform_insts)
            lines.append(f"AstrBot 平台适配器：{len(platforms)} 个")
        except Exception:
            lines.append("AstrBot 平台适配器：✗ 读取失败")
        persona = await self._resolve_persona(self._primary_umo())
        lines.append(f"人格：{'✓ 取到 ' + str(len(persona)) + ' 字' if persona else '✗ 没取到（会用中性提示词）'}")
        if as_bool(cfg_get(self.config, "push.bili_dm.enabled", False), False):
            dm_to = str(
                cfg_get(self.config, "push.bili_dm.to_mid", "") or cfg_get(self.config, "account.owner_mid", "")
            ).strip()
            if not dm_to:
                lines.append("B站私信：✗ 没填 owner_mid / to_mid")
            elif not cookie.get("sessdata"):
                lines.append("B站私信：未检查（先填 cookie）")
            else:
                try:
                    api = await self._ensure_api()
                    info = await api.login_info()
                    if str(info.get("mid") or "") == dm_to:
                        lines.append("B站私信：✗ 收件人就是登录账号（不能给自己发）")
                    else:
                        rel = await api.relation(int(dm_to))
                        attr = int(rel.get("attribute", -1) or -1)
                        lines.append(
                            "B站私信："
                            + ("✓ 互关，可发" if attr == 6 else f"✗ 未互关（attribute={attr}），双方手动关注后才能发")
                        )
                except Exception as exc:
                    lines.append(f"B站私信：✗ 检查失败（{exc}）")
        guard = TrafficGuard(self.store, lambda: self.config)
        lines.append("")
        lines.extend(guard.status_text().splitlines())
        lines.append("")
        lines.append(f"数据目录：{self.data_dir}")
        lines.append(f"已看过：{self.store.seen_count()} 条")
        report_dir = self.store.report_dir
        report_count = len(list(report_dir.glob("*.md"))) if report_dir.exists() else 0
        lines.append(f"日报：{report_count} 份")
        lines.append("调度器：" + ("✓ 运行中" if self._ticker and not self._ticker.done() else "✗ 没起来（看日志）"))
        return "\n".join(lines)

    def _report_text(self, day: str) -> str:
        day = self._normalize_day(day)
        return self.store.read_report(day)

    def _diary_text(self, day: str) -> str:
        day = self._normalize_day(day)
        owner = self.store.read_diary("owner", day)
        own = self.store.read_diary("self", day)
        parts = []
        if owner:
            parts.append("📔 给主人的信\n\n" + owner)
        if own:
            parts.append("📔 自己的日记\n\n" + own)
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _normalize_day(value: str) -> str:
        value = (value or "").strip()
        if not value:
            return today_str()
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            return today_str()
