"""AstrBot 插件入口：指令、主动推送、模型调用。

内核在 ``core/`` 里，这里只做三件事：
1. 把 AstrBot 的配置 / 模型 / 消息接口接到内核上；
2. 注册指令与一个 LLM Tool；
3. 用 asyncio 定时器驱动"生物钟"（不依赖 AstrBot 的调度器）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
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
    from .core.credential import describe_sessdata, inspect_sessdata
    from .core.errors import BiliAuthError, BiliError, BiliRisk, BudgetError
    from .core import livingmemory, memory_policy, qrlogin
    from .core.prompts import CAPABILITY_PROMPT
    from .core.report import build_dm_text
    from .core.runner import Runner
    from .core.settings import migrate_output, migrate_push, migrate_share, output_setting
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
        human_count,
        human_duration,
        now_local,
        today_str,
        truncate,
    )
except ImportError:  # 兼容"以单文件方式"加载 main.py 的宿主
    import sys

    _here = str(Path(__file__).resolve().parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    from core import planner, quota as quota_mod
    from core.bili_api import BiliAPI
    from core.credential import describe_sessdata, inspect_sessdata
    from core.errors import BiliAuthError, BiliError, BiliRisk, BudgetError
    from core import livingmemory, memory_policy, qrlogin
    from core.prompts import CAPABILITY_PROMPT
    from core.report import build_dm_text
    from core.runner import Runner
    from core.settings import migrate_output, migrate_push, migrate_share, output_setting
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
        human_count,
        human_duration,
        now_local,
        today_str,
        truncate,
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
/bili 登录          扫码登录（推荐，不用手抄 cookie）
/bili 体检          登录态 / 依赖 / 模型 / 配额自检
/bili 额度          看今天读/写/分享额度与熔断状态
/bili 分享 BV...    开启自动发送时直接发；加「预览」只看不发
/bili 确认          发送上一条预览（预览确认模式用）
/bili 取消          取消待确认的分享
/bili 分享设置      查看/修改：允许分享、自动发送、手动刷自动
/bili 版本          看插件版本 + main.py 指纹 + 配置/数据路径
/bili 重载          重连 B 站会话（改完 cookie 用）
/bili 重置计划      清掉今天的计划，按新作息重排

自动刷由 WebUI 插件配置里的「作息窗口」驱动；
每个窗口里随机一个时刻醒来，有概率今天不看，但日记照写。
评论分享默认关闭；打开「自动发送」后不再逐条确认，关闭时才需要 /bili 确认。

模型工具：bili_post_comment / bili_send_dm / bili_memorize 默认关闭，
需在「自然语言对话 → 允许模型真实发评论 / 私信」打开。"""


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
    if command_name in {"bili", "b站", "养鱼", "鲸鱼", "刷b站"}:
        return rest.strip()
    return text


def _filter_deco(name: str):
    """取 AstrBot 的 filter 装饰器；缺失时退化成空装饰器，插件仍能加载。"""
    maker = getattr(filter, name, None)

    def factory(*args, **kwargs):
        if maker is None:
            return lambda fn: fn
        try:
            return maker(*args, **kwargs)
        except TypeError:
            return lambda fn: fn

    return factory


_on_llm_request = _filter_deco("on_llm_request")


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


@register(PLUGIN_NAME, "momo-OwO-qwq", "养鱼刷B站：生物钟 + 真读字幕 + 日报/日记 + 克制的分享", VERSION)
class BiliWhalePlugin(Star):
    """养鱼刷B站：按生物钟替你刷 B 站，读字幕、写日报和两本日记。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            if migrate_output(self.config) or migrate_push(self.config) or migrate_share(self.config):
                self._save_config()
        except Exception:
            logger.warning("[bili-whale] 合并旧版 report/diary 设置失败", exc_info=True)
        self.data_dir = _data_dir()
        self.store = Store(self.data_dir)
        self._api: BiliAPI | None = None
        self._cookie_snapshot: dict[str, str] = {}
        self._run_lock = asyncio.Lock()
        self._api_lock = asyncio.Lock()
        self._ticker: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._qr_task: asyncio.Task | None = None
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
        if self._qr_task:
            self._qr_task.cancel()
            await asyncio.gather(self._qr_task, return_exceptions=True)
            self._qr_task = None
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
                    timeout = as_int(cfg_get(self.config, "rhythm.run_timeout_min", 20), 20, 1, 120) * 60
                    result = await asyncio.wait_for(runner.run(allow_share=True), timeout=timeout)
            except asyncio.TimeoutError:
                message = "⏱️ 这一轮超过时间上限，已中止；日报可能只写了一半。"
                logger.warning("[bili-whale] 到点执行超时")
                result = None
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

    async def _maybe_remember(self, result: Any, umo: str) -> None:
        """选择性写长期记忆：分数/概率/有趣程度由 runner 判定，这里只管落盘和限额。"""
        policy = self._memory_policy()
        if not policy.enabled or not policy.auto_write:
            return
        note = str(getattr(result, "memory_note", "") or "")
        bvid = str(getattr(result, "memory_bvid", "") or "")
        if not note or not bvid:
            return
        if not livingmemory.available(self.context):
            logger.debug("[bili-whale] 没有可用的 LivingMemory，跳过记忆写入")
            return
        day = today_str()
        log = self.store.get("memory_log")
        if not isinstance(log, dict):
            log = {}
            self.store.state["memory_log"] = log
        if memory_policy.already_logged(log, day, bvid):
            return
        if memory_policy.today_count(log, day) >= policy.max_per_day:
            return
        out = await livingmemory.write_memory(
            self.context,
            content=note,
            session_id=umo or None,
            importance=policy.importance,
            metadata={"kind": "bili_watch", "bvid": bvid, "date": day},
            max_chars=policy.max_chars,
        )
        if not out.get("ok"):
            logger.info("[bili-whale] 记忆写入失败：%s", out.get("reason"))
            return
        memory_policy.record(log, day, bvid)
        self.store.save()

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
                if as_bool(output_setting(self.config, "push_full_report"), False) and result.report_path:
                    try:
                        text = Path(result.report_path).read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                    if text:
                        await self._push(text, umo=target, max_chunks=8)
                if as_bool(output_setting(self.config, "push_diary"), False):
                    for kind in ("owner", "self"):
                        text = self.store.read_diary(kind)
                        if text:
                            title = "给主人的信" if kind == "owner" else "自己的日记"
                            await self._push(f"📔 {title}\n\n{text}", umo=target, max_chunks=4)
        elif not manual and as_bool(cfg_get(self.config, "push.on_wake", True), True):
            logger.warning("[bili-whale] 没有 AstrBot 推送目标：在目标会话发 /bili 订阅，或在配置里填 push.targets")

        await self._push_bili_dm(result, manual=manual)
        await self._maybe_remember(result, manual_umo or self._primary_umo() or "")

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
        provider_id = str(cfg_get(self.config, "llm.provider_id", "") or "").strip() or None
        try:
            if not provider_id and umo and hasattr(self.context, "get_current_chat_provider_id"):
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
    @filter.command("bili", alias={"养鱼", "鲸鱼"})
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
            "体检", "自检", "check", "诊断", "登录", "login",
            "推送", "推送平台", "platforms", "targets",
            "分享", "share", "分享设置", "评论分享设置", "share-settings", "确认", "confirm", "取消", "cancel",
            "重载", "reload", "状态", "status", "情况",
            "重置计划", "重排", "replan", "版本", "version",
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
            if event.get_group_id() and as_bool(output_setting(self.config, "diary_private_only"), True):
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

        if lower in {"登录", "login"}:
            if self._run_lock.locked():
                yield event.plain_result("🐟 正在刷，等这一轮结束再扫码登录。")
                return
            if self._qr_task and not self._qr_task.done():
                self._qr_task.cancel()
            try:
                url, key = await asyncio.to_thread(qrlogin.generate)
                png = self.data_dir / "login-qr.png"
                png.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(qrlogin.render, url, str(png))
            except Exception as exc:
                yield event.plain_result(f"❌ 申请二维码失败：{exc}")
                return
            try:
                yield event.image_result(str(png))
            except Exception:
                pass
            yield event.plain_result(
                "📱 用 B 站 App 扫这张码（我的 → 右上角 扫一扫）\n"
                f"· 扫完在手机上点「确认登录」，这边会自动写 cookie\n"
                f"· 3 分钟内有效；图片没发出去可以直接打开：{url}\n"
                "· 确认时选「记住我 / 长期」那张"
            )
            self._qr_task = asyncio.create_task(self._qr_watch(key, umo))
            return

        if lower in {"体检", "自检", "check", "诊断"}:
            yield event.plain_result(await self._health_text())
            return

        if lower in {"版本", "version"}:
            yield event.plain_result(self._version_text())
            return

        if lower in {"额度", "quota"}:
            guard = TrafficGuard(self.store, lambda: self.config)
            yield event.plain_result("📊 " + guard.status_text())
            return

        if lower in {"分享设置", "评论分享设置", "share-settings"}:
            share = self.config.get("share")
            if not isinstance(share, dict):
                share = {}
                self.config["share"] = share
            action = arg.strip()
            if not action:
                lines = [
                    "⚙️ 评论分享设置",
                    f"允许评论分享：{'开' if quota_mod.share_config_enabled(self.config) else '关'}",
                    f"自动发送（免确认）：{'开' if quota_mod.share_auto_enabled(self.config) else '关'}",
                    f"手动刷也自动发送：{'开' if self._auto_share_on_manual() else '关'}",
                    "",
                    "用法：",
                    "/bili 分享设置 开｜关       打开/关闭评论分享",
                    "/bili 分享设置 自动｜预览   自动发送 / 只生成预览",
                    "/bili 分享设置 手动刷 开｜关 控制 /bili 现在刷 是否自动评论",
                ]
                yield event.plain_result("\n".join(lines))
                return
            parts = action.split()
            changed = False
            if parts[0] in {"开", "开启", "on", "true", "1"}:
                share["enabled"] = True
                changed = True
            elif parts[0] in {"关", "关闭", "off", "false", "0"}:
                share["enabled"] = False
                changed = True
            elif parts[0] in {"自动", "auto"}:
                share["enabled"] = True
                share["auto_send"] = True
                changed = True
            elif parts[0] in {"预览", "manual", "confirm"}:
                share["enabled"] = True
                share["auto_send"] = False
                changed = True
            elif parts[0] in {"手动刷", "手动"} and len(parts) > 1:
                sub_action = parts[1]
                if sub_action in {"开", "开启", "on", "true", "1"}:
                    share["auto_on_manual"] = True
                    changed = True
                elif sub_action in {"关", "关闭", "off", "false", "0"}:
                    share["auto_on_manual"] = False
                    changed = True
            if not changed:
                yield event.plain_result("没看懂这个设置。发 /bili 分享设置 看用法。")
                return
            self._save_config()
            yield event.plain_result(
                "✓ 已保存\n"
                f"允许评论分享：{'开' if quota_mod.share_config_enabled(self.config) else '关'}｜"
                f"自动发送：{'开' if quota_mod.share_auto_enabled(self.config) else '关'}｜"
                f"手动刷自动：{'开' if self._auto_share_on_manual() else '关'}"
            )
            return

        if lower in {"分享", "share"}:
            if not quota_mod.share_config_enabled(self.config):
                yield event.plain_result("评论分享是关的（share.enabled = false）。先发 /bili 分享设置 开，或在插件配置里打开。")
                return
            bvid = extract_bvid(arg)
            if not bvid:
                yield event.plain_result("用法：/bili 分享 BV1xx...")
                return
            preview = ("预览" in arg) or ("preview" in arg.lower())
            auto = quota_mod.share_auto_enabled(self.config)
            yield event.plain_result(f"🐟 正在为 {bvid} 写分享评论…")
            try:
                runner = await self._make_runner(umo)
                payload = await runner.prepare_share(bvid)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
                return
            payload["umo"] = umo
            link = f"https://www.bilibili.com/video/{bvid}/"
            if auto and not preview:
                try:
                    post = await runner.confirm_share(payload)
                except Exception as exc:
                    yield event.plain_result(_err_text(exc))
                    return
                where = f"（rpid {post.rpid}）" if post.rpid else ""
                if post.verified:
                    yield event.plain_result(f"✓ 已自动发出并复查可见{where}：{link}")
                else:
                    yield event.plain_result(
                        f"⚠️ 评论接口已接受，但复查未通过{where}：{post.reason}\n"
                        f"这次不当作「已发」，也不会自动重发；稍后去视频页确认：{link}"
                    )
                return
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
                post = await runner.confirm_share(pending)
            except Exception as exc:
                yield event.plain_result(_err_text(exc))
                return
            where = f"（rpid {post.rpid}）" if post.rpid else ""
            link = f"https://www.bilibili.com/video/{pending['bvid']}/"
            if post.verified:
                yield event.plain_result(f"✓ 已发且复查可见{where}：{link}")
            else:
                yield event.plain_result(
                    f"⚠️ 评论接口已接受，但复查未通过{where}：{post.reason}\n"
                    f"这次不当作「已发」；不要重复发，稍后去视频页确认：{link}"
                )
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
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        code = extract_bvid(bvid)
        if not code:
            return "这不像一个 BV 号。"
        if not event.get_group_id() or self._can_manage(event):
            self.store.record_umo(event.unified_msg_origin)
        try:
            runner = await self._make_runner(event.unified_msg_origin)
            text, _ = await runner.digest(code)
            return self._tool_clip(text)
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    def _llm_enabled(self) -> bool:
        return as_bool(cfg_get(self.config, "llm.enabled", True), True)

    def _llm_tools_allowed(self) -> bool:
        return self._llm_enabled() and as_bool(cfg_get(self.config, "llm.allow_tools", True), True)

    def _llm_write_allowed(self) -> bool:
        return self._llm_tools_allowed() and as_bool(
            cfg_get(self.config, "llm.allow_write_tools", False), False
        )

    def _memory_policy(self) -> Any:
        return memory_policy.MemoryPolicy.from_config(self.config)

    def _write_denied_text(self) -> str:
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭（llm.allow_tools）。"
        return (
            "写操作默认关闭：需要先在插件配置「自然语言对话」里打开「允许模型真实发评论 / 私信」。"
        )

    def _tool_clip(self, text: str) -> str:
        return truncate(text, as_int(cfg_get(self.config, "llm.max_chars", 900), 900, 200, 4000))

    @_on_llm_request()
    async def inject_bili_capability(self, event: AstrMessageEvent, req) -> None:
        """把 B 站能力写进正常对话的系统提示。"""
        if not self._llm_enabled() or not as_bool(cfg_get(self.config, "llm.inject_prompt", True), True):
            return
        try:
            req.system_prompt = f"{getattr(req, 'system_prompt', '') or ''}{CAPABILITY_PROMPT}"
        except Exception:
            logger.debug("[bili-whale] 注入能力提示失败", exc_info=True)

    @filter.llm_tool(name="bili_capability")
    async def bili_capability(self, event: AstrMessageEvent) -> str:
        """说明这台机器人能用 B 站做什么、怎么触发。

        当用户问「你能看 B 站吗」「会刷视频吗」「有什么 B 站功能」时调用。
        """
        return self._tool_clip(CAPABILITY_PROMPT.strip())

    @filter.llm_tool(name="bili_account")
    async def bili_account(self, event: AstrMessageEvent) -> str:
        """查询当前 B 站账号的登录状态、昵称和 mid。"""
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        try:
            api = await self._ensure_api()
            info = await api.login_info()
            if not info.get("is_login"):
                return "B 站未登录：让用户发 /bili 登录 扫码，或填 cookie。"
            return self._tool_clip(f"登录账号：{info.get('uname')}（mid {info.get('mid')}）")
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    @filter.llm_tool(name="bili_search")
    async def bili_search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        count: int = 8,
        order: str = "pubdate",
    ) -> str:
        """按关键词搜索 B 站视频，返回标题 / BV / UP / 播放 / 时长。

        Args:
            keyword(string): 搜索关键词，例如「编程 入门」
            count(number): 返回几条，默认 8，最多 20
            order(string): 排序：pubdate 最新 / click 播放 / dm 弹幕 / totalrank 综合
        """
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        kw = clean_text(keyword, limit=60)
        if not kw:
            return "没给关键词。"
        try:
            api = await self._ensure_api()
            videos = await api.search(
                kw,
                page=1,
                page_size=max(1, min(20, int(count or 8))),
                order=order,
            )
            if not videos:
                return "没搜到。"
            lines = []
            for i, video in enumerate(videos, 1):
                lines.append(
                    f"{i}. {video.title}（{video.bvid}）· UP {video.author} · "
                    f"{human_count(video.play)} 播放 · {human_duration(video.duration)}"
                )
            return self._tool_clip("\n".join(lines))
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    @filter.llm_tool(name="bili_comments")
    async def bili_comments(self, event: AstrMessageEvent, bvid: str, count: int = 10) -> str:
        """读取一条 B 站视频的热门评论。

        Args:
            bvid(string): B 站 BV 号
            count(number): 取几条，默认 10，最多 20
        """
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        code = extract_bvid(bvid)
        if not code:
            return "这不像一个 BV 号。"
        try:
            api = await self._ensure_api()
            video = await api.video_info(code)
            comments = await api.hot_comments(video, limit=max(1, min(20, int(count or 10))))
            if not comments:
                return "没读到热评。"
            lines = [f"{video.title} · {video.bvid}"]
            for comment in comments:
                lines.append(f"- {comment.user}（{comment.like} 赞）：{comment.text}")
            return self._tool_clip("\n".join(lines))
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    @filter.llm_tool(name="bili_diary")
    async def bili_diary(self, event: AstrMessageEvent, days: int = 1) -> str:
        """读取最近几天的 B 站日记（给主人看的信 + 自己的日记）。

        Args:
            days(number): 读最近几天，默认 1，最多 7
        """
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        if event.get_group_id() and not self._can_manage(event):
            return "日记只在私聊或管理员会话里读。"
        try:
            import datetime as _dt

            count = max(1, min(7, int(days or 1)))
            parts: list[str] = []
            for offset in range(count):
                day = (_dt.date.today() - _dt.timedelta(days=offset)).isoformat()
                owner = self.store.read_diary("owner", day)
                own = self.store.read_diary("self", day)
                if owner:
                    parts.append(f"## {day} 给主人的信\n{owner}")
                if own:
                    parts.append(f"## {day} 自己的日记\n{own}")
            return self._tool_clip("\n\n".join(parts) or "最近没有日记。")
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    @filter.llm_tool(name="bili_status")
    async def bili_status(self, event: AstrMessageEvent) -> str:
        """查看 B 站养鱼的账号、作息、今日计划、额度和推送目标。"""
        if not self._llm_tools_allowed():
            return "自然语言工具已关闭。"
        if event.get_group_id() and not self._can_manage(event):
            return "状态里的账号/路径只在私聊或管理员会话里看。"
        try:
            return self._tool_clip(await self._status_text())
        except Exception as exc:
            return self._tool_clip(_err_text(exc))

    @filter.llm_tool(name="bili_post_comment")
    async def bili_post_comment(self, event: AstrMessageEvent, bvid: str, message: str) -> str:
        """在某条 B 站视频下发一条真实评论（会真的发布到 B 站，需显式开启）。

        Args:
            bvid(string): B 站 BV 号，例如 BV1xx411c7mD
            message(string): 评论正文，1~500 字，不要带引号或 Markdown
        """
        if not self._llm_write_allowed():
            return self._write_denied_text()
        if event.get_group_id() and not self._can_manage(event):
            return "群聊里只有管理员能让模型发评论。"
        if event.get_group_id() and not quota_mod.share_config_enabled(self.config):
            return "评论分享总开关是关的（share.enabled）。"
        code = extract_bvid(bvid)
        if not code:
            return "这不像一个 BV 号。"
        text = clean_text(message, limit=500, keep_newlines=False)
        if not text:
            return "评论内容是空的。"
        try:
            api = await self._ensure_api()
            video = await api.video_info(code)
            aid = video.aid or (await api.video_info(code, force=True)).aid
            post = await api.post_comment(aid, text)
        except Exception as exc:
            return self._tool_clip(_err_text(exc))
        if post.verified:
            try:
                self.store.add_comment(video.bvid, text)
            except OSError:
                logger.warning("[bili-whale] 评论已发出，但写评论台账失败")
            return self._tool_clip(f"✓ 已发且复查可见（rpid {post.rpid}）：{video.link}")
        return self._tool_clip(
            f"⚠️ 接口已接受，但复查未通过（rpid {post.rpid}）：{post.reason}。"
            "不要重复发，稍后再去视频页确认。"
        )

    @filter.llm_tool(name="bili_send_dm")
    async def bili_send_dm(self, event: AstrMessageEvent, message: str) -> str:
        """给配置好的 B 站收件人发一条真实私信（不是发到聊天里，需显式开启）。

        Args:
            message(string): 私信正文，1~300 字
        """
        if not self._llm_write_allowed():
            return self._write_denied_text()
        if event.get_group_id() and not self._can_manage(event):
            return "群聊里只有管理员能让模型发私信。"
        if not as_bool(cfg_get(self.config, "push.bili_dm.enabled", False), False):
            return "B 站私信推送是关的（push.bili_dm.enabled）。"
        text = clean_text(message, limit=300, keep_newlines=True)
        if not text:
            return "私信内容是空的。"
        to_mid = str(
            cfg_get(self.config, "push.bili_dm.to_mid", "")
            or cfg_get(self.config, "account.owner_mid", "")
        ).strip()
        if not to_mid:
            return "还没配置私信收件人（push.bili_dm.to_mid / account.owner_mid）。"
        try:
            api = await self._ensure_api()
            await api.send_dm(int(to_mid), text)
        except Exception as exc:
            return self._tool_clip(_err_text(exc))
        return self._tool_clip(f"✓ 已发 B 站私信给 mid {to_mid}")

    @filter.llm_tool(name="bili_memorize")
    async def bili_memorize(
        self,
        event: AstrMessageEvent,
        content: str,
        bvid: str = "",
        importance: float = 0.65,
    ) -> str:
        """把一条值得记住的事写进 LivingMemory 长期记忆。

        Args:
            content(string): 要记住的内容，一句话说清
            bvid(string): 相关视频的 BV 号，可留空
            importance(number): 重要性 0~1，默认 0.65
        """
        if not self._llm_write_allowed():
            return self._write_denied_text()
        if event.get_group_id() and not self._can_manage(event):
            return "群聊里只有管理员能写入长期记忆。"
        policy = self._memory_policy()
        if not policy.enabled:
            return "记忆联动已关闭（memory.enabled）。"
        text = clean_text(content, limit=max(50, policy.max_chars), keep_newlines=False)
        if not text:
            return "要记住的内容是空的。"
        code = extract_bvid(bvid) if bvid else ""
        meta = {"kind": "bili_note", "date": today_str()}
        if code:
            meta["bvid"] = code
        out = await livingmemory.write_memory(
            self.context,
            content=text,
            session_id=event.unified_msg_origin,
            importance=importance,
            metadata=meta,
            max_chars=policy.max_chars,
        )
        if not out.get("ok"):
            return f"没能写入长期记忆：{out.get('reason')}"
        return f"✓ 已写入 LivingMemory（id {out.get('id')}）"

    # ── 指令实现细节 ─────────────────────────────────────────────────────
    def _auto_share_on_manual(self) -> bool:
        return quota_mod.share_auto_enabled(self.config) and as_bool(
            cfg_get(self.config, "share.auto_on_manual", True), True
        )

    async def _manual_run(self, umo: str) -> None:
        cooldown = as_int(cfg_get(self.config, "rhythm.manual_cooldown_min", 10), 10, 0, 120)
        last = float(self.store.get("last_manual_at") or 0)
        if cooldown and last and time.time() - last < cooldown * 60:
            left = max(1, int((cooldown * 60 - (time.time() - last)) // 60) + 1)
            await self._push(f"🐟 手动刷刚跑过，再等约 {left} 分钟（防手滑狂点）。", umo=umo)
            return
        async with self._run_lock:
            self.store.state["last_manual_at"] = time.time()
            self.store.save()
            try:
                runner = await self._make_runner(umo)
                manual_max = min(
                    as_int(cfg_get(self.config, "content.deep_read", 3), 3, 1, 8),
                    as_int(cfg_get(self.config, "rhythm.manual_max", 3), 3, 1, 8),
                )
                timeout = as_int(cfg_get(self.config, "rhythm.run_timeout_min", 20), 20, 1, 120) * 60
                result = await asyncio.wait_for(
                    runner.run(allow_share=self._auto_share_on_manual(), deep_limit=manual_max),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._push("⏱️ 手动这一轮超过时间上限，已中止。", umo=umo)
                return
            except Exception as exc:
                await self._push(_err_text(exc), umo=umo)
                return
        if quota_mod.share_auto_enabled(self.config) and not self._auto_share_on_manual():
            result.summary += "\n（自动发送已关闭「手动刷」；想发用 /bili 分享 BV…）"
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
        share_on = "开" if quota_mod.share_config_enabled(self.config) else "关"
        auto_on = "开" if quota_mod.share_auto_enabled(self.config) else "关"
        manual_auto = "开" if self._auto_share_on_manual() else "关"
        lines.append(f"评论分享：{share_on}｜自动发送：{auto_on}｜手动刷自动：{manual_auto}")
        policy = self._memory_policy()
        if not policy.enabled:
            memory_line = "记忆联动：已关闭"
        else:
            log = self.store.get("memory_log")
            used = memory_policy.today_count(log if isinstance(log, dict) else {}, today_str())
            memory_line = (
                f"记忆联动：LivingMemory {livingmemory.status_text(self.context)}"
                f"｜今日已记 {used}/{policy.max_per_day}"
            )
        lines.append(memory_line)
        lines.append(
            "写工具："
            + ("开" if self._llm_write_allowed() else "关")
            + "（模型可发评论/私信/写记忆）"
        )
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

    async def _qr_watch(self, key: str, umo: str) -> None:
        deadline = time.monotonic() + 180
        scanned = False
        while time.monotonic() < deadline:
            await asyncio.sleep(2.5)
            try:
                data = await asyncio.to_thread(qrlogin.poll, key)
            except Exception:
                continue
            try:
                code = int(data.get("code", -1))
            except (TypeError, ValueError):
                code = -1
            if code == qrlogin.STATE_OK:
                try:
                    await self._finish_qr_login(data, umo)
                except Exception as exc:
                    await self._push(f"❌ 扫上了，但写 cookie 时出错：{exc}", umo=umo)
                return
            if code == qrlogin.STATE_EXPIRED:
                await self._push("⏰ 二维码过期了，再发一次 /bili 登录 换一张。", umo=umo)
                return
            if code == qrlogin.STATE_SCANNED and not scanned:
                scanned = True
                await self._push("👀 已扫码，等你在手机上点「确认登录」。", umo=umo)
        await self._push("⏰ 3 分钟没确认，这张码作废了；再发一次 /bili 登录。", umo=umo)

    async def _finish_qr_login(self, data: dict[str, Any], umo: str) -> None:
        cred = qrlogin.credentials(data)
        if not cred.get("sessdata"):
            url = str(data.get("url") or "")
            if url.startswith("http"):
                try:
                    more = await asyncio.to_thread(qrlogin.fetch_callback, url)
                except Exception:
                    more = {}
                for k, v in more.items():
                    cred.setdefault(k, v)
        if not cred.get("sessdata"):
            await self._push(
                "❌ 手机上确认了，但响应里没有 SESSDATA。\n"
                f"· 现场：{qrlogin.diagnose(data)}\n"
                "可以再扫一次，或手动填 cookie。",
                umo=umo,
            )
            return
        if not cred.get("bili_jct"):
            try:
                jct = await asyncio.to_thread(qrlogin.refresh_jct, cred["sessdata"])
            except Exception:
                jct = ""
            if jct:
                cred["bili_jct"] = jct
        account = self.config.get("account")
        if not isinstance(account, dict):
            account = {}
            self.config["account"] = account
        for key in ("sessdata", "bili_jct", "buvid3"):
            value = cred.get(key)
            if value:
                account[key] = value
        self._save_config()
        await self._reset_api()
        note = describe_sessdata(inspect_sessdata(cred["sessdata"]))
        if not cred.get("bili_jct"):
            note += " · 没拿到 bili_jct：只读可用，发评论/私信会被拒"
        try:
            info = await self._ensure_api().login_info()
            if info.get("is_login"):
                head = f"✓ 扫码登录成功：{info.get('uname')}（mid {info.get('mid')}）"
            else:
                head = "⚠️ 已写入 cookie，但 B 站仍说未登录"
        except Exception as exc:
            head = f"⚠️ 已写入 cookie，但验证失败：{exc}"
        await self._push(f"{head}\n· {note}", umo=umo)

    async def _health_text(self) -> str:
        lines = ["🩺 体检", ""]
        cookie = self._cookie_dict()
        lines.append("cookie：" + ("✓ 已填三个字段" if all(cookie.values()) else "✗ 缺字段（SESSDATA / bili_jct / buvid3）"))
        if cookie.get("sessdata"):
            lines.append("SESSDATA：" + describe_sessdata(inspect_sessdata(cookie["sessdata"])))
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

    def _version_text(self) -> str:
        path = Path(__file__).resolve()
        try:
            raw = path.read_bytes()
            digest = hashlib.sha1(raw).hexdigest()[:12]
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines = [
                f"🐟 养鱼刷B站 v{VERSION}",
                f"main.py：{len(raw)} 字节 · {mtime} · sha1:{digest}",
                f"路径：{path}",
                f"数据：{self.data_dir}",
            ]
            config_path = getattr(self.config, "config_path", "")
            if config_path:
                lines.append(f"配置：{config_path}")
            return "\n".join(lines)
        except Exception as exc:
            return f"🐟 养鱼刷B站 v{VERSION}\n读不到 main.py 指纹：{exc}"

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
