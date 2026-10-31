"""一轮"刷 B 站"的流水线。

流程（对应 scroll 的"搜 → 读字幕 → 写报告" + whale 的"挑、克制、日记"）：

1. 搜关键词，按时间/时长/判重过滤；
2. 让模型从候选里挑最多 N 条深读（模型不可用时按热度/新鲜度兜底）；
3. 逐条读：官方字幕 + 热评 + 弹幕热区；
4. 模型给评分/档位/理由；没有字幕证据时**代码强制**最多「可以看」；
5. 写日报（每条 ``### BV号``）、两本日记；
6. 按配置决定要不要在评论区 @ 使用者（默认关，硬闸门在 quota）。

模型只负责"判断和说话"，搜索、读、判重、限额、发送都在代码里。
"""

from __future__ import annotations

import math
import time
from typing import Any, Awaitable, Callable, Mapping

from . import quota as quota_mod
from .bili_api import BiliAPI
from .errors import BiliAuthError, BiliError, BiliRisk, BudgetError
from .models import DeepResult, Material, RunResult, Video
from .prompts import (
    DEFAULT_KEYWORDS,
    coerce_deep,
    coerce_diary,
    coerce_screen,
    comment_prompt,
    deep_prompt,
    diary_prompt,
    fallback_comment,
    fallback_deep,
    parse_json_obj,
    sanitize_comment,
    screen_prompt,
    system_prompt,
)
from .report import VERDICT_ORDER, append_report, build_digest_text, build_report, build_skip_note, build_summary
from .store import Store
from .util import (
    as_bool,
    as_int,
    as_str_list,
    cfg_get,
    iso,
    now_local,
    today_str,
)

ChatFn = Callable[[str, str], Awaitable[str | None]]


class Runner:
    def __init__(
        self,
        cfg: Mapping[str, Any] | Any,
        api: BiliAPI,
        store: Store,
        chat: ChatFn | None = None,
        logger: Any = None,
        persona_text: str = "",
    ):
        self.cfg = cfg
        self.api = api
        self.store = store
        self.chat = chat
        self.logger = logger
        # AstrBot 人格正文（由 main.py 解析），空则退回插件自己的 persona 配置。
        self.persona_text = persona_text

    def _system(self) -> str:
        return system_prompt(self.cfg, self.persona_text or None)

    # ── 对外：完整一轮 ───────────────────────────────────────────────────
    async def run(self, allow_share: bool = True, deep_limit: int | None = None) -> RunResult:
        result = RunResult()
        day = today_str()
        try:
            info = await self.api.login_info()
            if not info.get("is_login"):
                raise BiliAuthError("B 站 cookie 未登录或已过期")
            candidates = await self.collect_candidates()
            result.scanned = len(candidates)
            if not candidates:
                result.summary = "🐟 今天没有搜到新的视频（关键词都翻过一遍了）。"
                return result

            picks = await self._screen(candidates, deep_limit)
            deep_results: list[DeepResult] = []
            unreadable = 0
            limit = max(1, min(8, int(deep_limit or self._deep_limit())))
            for video in picks[:limit]:
                item = await self.deep_read(video)
                if item is None:
                    unreadable += 1
                    self.store.mark_seen(video, "跳过-读不到内容")
                    continue
                deep_results.append(item)
                self.store.mark_seen(video, f"深读-{item.normalized().verdict}")
            result.deep = len(deep_results)

            visible = [r for r in deep_results if r.normalized().verdict != "不值当"]
            visible.sort(key=lambda r: (VERDICT_ORDER.get(r.normalized().verdict, 9), -r.normalized().score))
            result.picked = len(visible)

            closing = await self._write_diaries(day, visible)
            if visible:
                detail = str(cfg_get(self.cfg, "report.detail", "full") or "full")
                report = build_report(
                    day,
                    visible,
                    scanned=result.scanned,
                    deep_count=result.deep,
                    detail=detail,
                    closing=closing,
                    skipped_no_subtitle=unreadable,
                )
                if report:
                    path = append_report(self.store.report_path(day), report)
                    result.report_path = str(path)
            else:
                # 深读了但都不值得：留一行痕迹，别让人以为插件没跑。
                note = build_skip_note(day, "今天深读的几条都不值得推荐；理由见日记。")
                path = append_report(self.store.report_path(day), note)
                result.report_path = str(path)

            if allow_share and quota_mod.share_auto_enabled(self.cfg):
                shared, share_note = await self.share_best(visible)
                result.shared = shared
                if share_note:
                    result.summary = share_note
            result.summary = (result.summary + "\n" if result.summary else "") + build_summary(result, visible)
            if unreadable:
                result.summary += f"\n另有 {unreadable} 条读不到内容，跳过了。"
            if visible and not result.shared and quota_mod.share_manual_enabled(self.cfg):
                result.summary += "\n（分享模式 manual：想发的话用 /bili 分享 BV… 预览）"
            self.store.touch_run()
            return result
        except BiliAuthError as exc:
            result.error = str(exc)
            result.summary = f"❌ {exc}\n去 WebUI → 插件配置里重新填 SESSDATA / bili_jct / buvid3，然后发 /bili 体检。"
            return result
        except BiliRisk as exc:
            result.error = str(exc)
            result.summary = f"⛔ {exc}\n已自动停手，别再连续重试；等熔断结束或明天再看。"
            return result
        except BudgetError as exc:
            result.error = str(exc)
            result.summary = f"🛑 {exc}\n今天先到这儿；额度可以在插件配置里调，但小号别调太高。"
            return result
        except BiliError as exc:
            result.error = str(exc)
            result.summary = f"❌ {exc}"
            return result
        except Exception as exc:  # 兜底：不让插件把 AstrBot 带崩
            if self.logger:
                self.logger.exception("bili-whale run failed")
            result.error = f"{type(exc).__name__}: {exc}"
            result.summary = "❌ 刷 B 站时出了个内部错误，详情看 AstrBot 日志。"
            return result

    async def run_skip(self, reason: str = "") -> RunResult:
        result = RunResult(skip=True, summary="🐟 今天不想看 B 站，但日记还是写了。")
        day = today_str()
        try:
            closing = await self._write_diaries(day, [], skip=True)
            note = build_skip_note(day, reason or "今天就是不想看，没有理由。")
            if closing:
                note += f"\n> {closing}\n"
            append_report(self.store.report_path(day), note)
            result.report_path = str(self.store.report_path(day))
            self.store.touch_run()
        except Exception as exc:
            if self.logger:
                self.logger.exception("bili-whale skip diary failed")
            result.error = str(exc)
            result.summary = f"🐟 今天不想看；日记没写成（{type(exc).__name__}）。"
        return result

    # ── 搜索与筛选 ───────────────────────────────────────────────────────
    async def collect_candidates(self) -> list[Video]:
        raw_keywords = as_str_list(cfg_get(self.cfg, "content.keywords", []), [])[:6]
        source = str(cfg_get(self.cfg, "content.source", "auto") or "auto").strip().lower()
        if source not in {"auto", "keywords", "recommend"}:
            source = "auto"
        if source == "auto":
            source = "recommend" if not raw_keywords else "keywords"
        days = as_int(cfg_get(self.cfg, "content.search_days", 7), 7, 1, 90)
        pages = as_int(cfg_get(self.cfg, "content.search_pages", 2), 2, 1, 5)
        order = str(cfg_get(self.cfg, "content.search_order", "pubdate") or "pubdate")
        min_duration = as_int(cfg_get(self.cfg, "content.min_duration_sec", 60), 60, 0, 86_400)
        max_duration = as_int(cfg_get(self.cfg, "content.max_duration_sec", 7200), 7200, 60, 86_400)
        excludes = [x.lower() for x in as_str_list(cfg_get(self.cfg, "content.exclude_keywords", []))]
        cutoff = time.time() - days * 86_400
        by_bvid: dict[str, Video] = {}
        last_error: Exception | None = None

        def accept(video: Video, apply_age: bool = True) -> bool:
            if not video.bvid or video.bvid in by_bvid:
                return False
            if self.store.is_seen(video.bvid, days=30):
                return False
            if apply_age and video.pubdate and video.pubdate < cutoff:
                return False
            if video.duration and video.duration < min_duration:
                return False
            if video.duration and video.duration > max_duration:
                return False
            title = video.title.lower()
            if any(word and word in title for word in excludes):
                return False
            by_bvid[video.bvid] = video
            return True

        if source == "recommend":
            try:
                rows = await self.api.recommend(40)
                for video in rows:
                    # 推荐流本来就可能推旧视频，不按发布时间砍
                    accept(video, apply_age=False)
            except (BiliRisk, BudgetError):
                raise
            except BiliError as exc:
                last_error = exc
                if self.logger:
                    self.logger.warning("bili recommend failed: %s", exc)
            # 推荐流失败且没有关键词时，退回内置关键词，至少别白跑
            if not by_bvid and not raw_keywords:
                source = "keywords"

        if source == "keywords":
            keywords = raw_keywords or DEFAULT_KEYWORDS
            for keyword in keywords:
                for page in range(1, pages + 1):
                    try:
                        rows = await self.api.search(keyword, page=page, order=order)
                    except (BiliRisk, BudgetError):
                        raise
                    except BiliError as exc:
                        last_error = exc
                        if self.logger:
                            self.logger.warning("bili search failed for %s p%s: %s", keyword, page, exc)
                        break
                    if not rows:
                        break
                    for video in rows:
                        accept(video)
                    if len(by_bvid) >= 80:
                        break
                if len(by_bvid) >= 80:
                    break

        if not by_bvid and last_error:
            raise BiliError(f"候选接口都没成功：{last_error}")
        rows = list(by_bvid.values())
        if source == "keywords":
            rows.sort(key=lambda v: (v.pubdate, v.play), reverse=True)
        return rows[:40]

    async def _screen(self, candidates: list[Video], want: int | None = None) -> list[Video]:
        want = max(1, min(8, int(want or self._deep_limit())))
        if len(candidates) <= want:
            return candidates[:want]
        picks: list[str] = []
        if self.chat:
            text = await self._ask(self._system(), screen_prompt(candidates, want, self.cfg))
            picks = coerce_screen(parse_json_obj(text), candidates, want)
        by_bvid = {v.bvid: v for v in candidates}
        chosen = [by_bvid[b] for b in picks if b in by_bvid]
        if len(chosen) < want:
            chosen_ids = {v.bvid for v in chosen}
            for video in sorted(candidates, key=_heuristic_score, reverse=True):
                if video.bvid not in chosen_ids:
                    chosen.append(video)
                    chosen_ids.add(video.bvid)
                if len(chosen) >= want:
                    break
        return chosen[:want]

    def _deep_limit(self) -> int:
        return as_int(cfg_get(self.cfg, "content.deep_read", 3), 3, 1, 8)

    # ── 单条深读 ─────────────────────────────────────────────────────────
    async def _gather_material(self, video: Video, comments_limit: int = 10) -> Material:
        info = video
        try:
            info = await self.api.video_info(video.bvid)
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError as exc:
            if self.logger:
                self.logger.warning("video info failed %s: %s", video.bvid, exc)
        material = Material(video=info)
        try:
            material.subtitle = await self.api.subtitles(info, preferred_lang=self._subtitle_lang())
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError as exc:
            material.subtitle_error = str(exc)
        try:
            material.comments = await self.api.hot_comments(info, limit=comments_limit)
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError as exc:
            if self.logger:
                self.logger.warning("comments failed %s: %s", info.bvid, exc)
        if as_bool(cfg_get(self.cfg, "content.danmaku", True), True):
            try:
                material.danmaku = await self.api.danmaku_hotspots(info.cid, info.duration)
            except BiliRisk:
                raise
            except BudgetError:
                raise
            except BiliError:
                material.danmaku = []
        return material

    async def deep_read(self, video: Video, use_llm: bool = True) -> DeepResult | None:
        material = await self._gather_material(video, comments_limit=10)
        # 什么都没读到：不硬凑，记一笔"跳过"。
        if not material.has_subtitle and not material.danmaku and len(material.comments) < 3:
            return None
        if use_llm and self.chat:
            text = await self._ask(self._system(), deep_prompt(material, self.cfg))
            result = coerce_deep(parse_json_obj(text), material)
        else:
            result = fallback_deep(material)
        return result.normalized()

    def _subtitle_lang(self) -> str:
        return str(cfg_get(self.cfg, "content.subtitle_lang", "ai-zh") or "ai-zh")

    # ── 日记 / 报告 ──────────────────────────────────────────────────────
    async def _write_diaries(self, day: str, results: list[DeepResult], skip: bool = False) -> str:
        if not as_bool(cfg_get(self.cfg, "diary.enabled", True), True):
            return ""
        fallback = "今天刷了一圈，没什么特别想说的。" if not skip else "今天不想看，就这样。"
        data = None
        if self.chat:
            text = await self._ask(self._system(), diary_prompt(day, results, self.cfg, skip=skip))
            data = parse_json_obj(text)
        diary = coerce_diary(data, fallback)
        stamp = now_local().strftime("%H:%M")
        try:
            self.store.append_diary("owner", f"_{stamp}_\n\n{diary['letter']}", day)
            self.store.append_diary("self", f"_{stamp}_\n\n{diary['diary']}", day)
        except OSError as exc:
            if self.logger:
                self.logger.warning("write diary failed: %s", exc)
        return diary["closing"]

    # ── 分享（评论区 @ 你）───────────────────────────────────────────────
    async def prepare_share(self, bvid: str) -> dict[str, Any]:
        if not quota_mod.share_config_enabled(self.cfg):
            raise BiliError("分享已在配置里关闭（share.mode = off）")
        video = await self.api.video_info(bvid)
        result = await self.deep_read(video, use_llm=True)
        if result is None:
            raise BiliError("这条视频读不到内容（没字幕/弹幕/评论），不适合分享")
        result = result.normalized()
        if result.verdict == "不值当":
            raise BiliError("这条读下来判为「不值当」，不值得占用你的注意力去分享")
        limits = quota_mod.Limits.from_config(self.cfg)
        decision = quota_mod.can_share(
            self.store.quota, limits, video.bvid, result.score, result.why
        )
        if not decision:
            raise BiliError(decision.reason)
        owner = str(cfg_get(self.cfg, "account.owner_name", "") or "").strip()
        if not owner:
            raise BiliError("还没填「你的 B 站昵称」，没法 @ 你；去插件配置里补上")
        comment = ""
        if self.chat:
            text = await self._ask(self._system(), comment_prompt(result, self.cfg))
            comment = sanitize_comment(text or "", owner)
        if not comment:
            comment = fallback_comment(result, owner)
        return {
            "bvid": video.bvid,
            "aid": video.aid,
            "title": video.title,
            "score": result.score,
            "verdict": result.verdict,
            "reason": result.why,
            "comment": comment,
            "created_at": iso(),
        }

    async def confirm_share(self, payload: dict[str, Any]) -> str:
        if not quota_mod.share_config_enabled(self.cfg):
            raise BiliError("分享已在配置里关闭（share.mode = off）")
        limits = quota_mod.Limits.from_config(self.cfg)
        bvid = str(payload.get("bvid") or "")
        score = int(payload.get("score") or 0)
        reason = str(payload.get("reason") or "")
        if str(payload.get("verdict") or "") == "不值当":
            raise BiliError("待确认的分享已被判为「不值当」，不发了")
        decision = quota_mod.can_share(self.store.quota, limits, bvid, score, reason)
        if not decision:
            raise BiliError(decision.reason)
        write_decision = quota_mod.can_write(self.store.quota, limits)
        if not write_decision:
            raise BiliError(write_decision.reason)
        aid = int(payload.get("aid") or 0)
        if not aid:
            aid = (await self.api.video_info(bvid)).aid
        rpid = await self.api.post_comment(aid, str(payload.get("comment") or ""))
        quota_mod.record_share(self.store.quota, bvid)
        self.store.clear_pending_share()
        return rpid

    async def share_best(self, results: list[DeepResult]) -> tuple[int, str]:
        if not results:
            return 0, ""
        worthy = [r for r in results if r.normalized().verdict == "值得看"]
        if not worthy:
            return 0, ""
        best = max(worthy, key=lambda r: r.normalized().score).normalized()
        limits = quota_mod.Limits.from_config(self.cfg)
        if best.score < limits.min_score:
            return 0, ""
        owner = str(cfg_get(self.cfg, "account.owner_name", "") or "").strip()
        if not owner:
            return 0, "（想分享但没填你的 B 站昵称，跳过）"
        decision = quota_mod.can_share(self.store.quota, limits, best.video.bvid, best.score, best.why)
        if not decision:
            return 0, f"（这次没分享：{decision.reason}）"
        try:
            write_decision = quota_mod.can_write(self.store.quota, limits)
            if not write_decision:
                return 0, f"（这次没分享：{write_decision.reason}）"
            comment = ""
            if self.chat:
                text = await self._ask(self._system(), comment_prompt(best, self.cfg))
                comment = sanitize_comment(text or "", owner)
            if not comment:
                comment = fallback_comment(best, owner)
            aid = best.video.aid or (await self.api.video_info(best.video.bvid)).aid
            rpid = await self.api.post_comment(aid, comment)
            quota_mod.record_share(self.store.quota, best.video.bvid)
            self.store.save()
            where = f"（rpid {rpid}）" if rpid else ""
            return 1, f"已在这条视频评论区 @ 你{where}：{best.video.title}"
        except BiliRisk:
            raise
        except BudgetError:
            raise
        except BiliError as exc:
            if self.logger:
                self.logger.warning("share failed: %s", exc)
            return 0, f"（这次没分享成功：{exc}）"

    # ── 手动：看懂一条 / 搜一下 ──────────────────────────────────────────
    async def digest(self, bvid: str) -> tuple[str, DeepResult]:
        video = await self.api.video_info(bvid)
        material = await self._gather_material(video, comments_limit=6)
        if not material.has_subtitle and not material.danmaku and len(material.comments) < 3:
            raise BiliError("这条视频没字幕、没弹幕、评论也太少，读不到内容")
        if self.chat:
            text = await self._ask(self._system(), deep_prompt(material, self.cfg))
            result = coerce_deep(parse_json_obj(text), material)
        else:
            result = fallback_deep(material)
        result = result.normalized()
        self.store.mark_seen(material.video, "手动-看懂")
        return build_digest_text(material, result), result

    async def search(self, keyword: str) -> list[Video]:
        return await self.api.search(keyword, page=1, page_size=20)

    # ── 模型调用 ─────────────────────────────────────────────────────────
    async def _ask(self, system: str, prompt: str) -> str | None:
        if not self.chat:
            return None
        try:
            text = await self.chat(system, prompt)
        except Exception as exc:
            if self.logger:
                self.logger.warning("LLM call failed: %s", exc)
            return None
        return text if isinstance(text, str) else None


def _heuristic_score(video: Video) -> float:
    now = time.time()
    age_days = max(0.0, (now - (video.pubdate or 0)) / 86_400) if video.pubdate else 30.0
    recency = max(0.0, 7.0 - age_days)
    return recency * 2 + math.log10(max(1, video.play)) + (1.0 if video.duration >= 300 else 0.0)
