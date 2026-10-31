"""持久化：一个 state.json + 日报/日记的普通 Markdown 文件。

为什么不用数据库：这些数据人应该能直接打开看，插件坏了也能拷走。
写入用"临时文件 + os.replace"，避免断电/崩溃留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Video
from .util import iso, now_local, parse_iso, today_str


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": iso(),
        "plan": {},
        "history": {},
        "quota": {},
        "seen": {},
        "pending_share": {},
        "memory_log": {},
        "last_umo": "",
        "last_run": "",
        "runs": 0,
    }


class Store:
    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)
        self.report_dir = self.base / "reports"
        self.diary_dir = self.base / "diary"
        self.comments_path = self.base / "comments.md"
        self.base.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.diary_dir / "owner").mkdir(parents=True, exist_ok=True)
        (self.diary_dir / "self").mkdir(parents=True, exist_ok=True)
        self.path = self.base / "state.json"
        self.state = _default_state()
        self.load()

    # ── 基础读写 ──────────────────────────────────────────────────────────
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.state
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 坏文件不要直接覆盖：留一份，再从空状态开始。
            try:
                shutil.copy2(self.path, self.path.with_suffix(".broken.json"))
            except OSError:
                pass
            return self.state
        if isinstance(data, dict):
            merged = _default_state()
            merged.update(data)
            self.state = merged
        return self.state

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.state[key] = value
        if save:
            self.save()

    @property
    def quota(self) -> dict[str, Any]:
        quota = self.state.get("quota")
        if not isinstance(quota, dict):
            quota = {}
            self.state["quota"] = quota
        return quota

    # ── 判重 ─────────────────────────────────────────────────────────────
    def is_seen(self, bvid: str, days: int = 30, now: datetime | None = None) -> bool:
        if not bvid:
            return False
        seen = self.state.get("seen")
        if not isinstance(seen, dict):
            return False
        row = seen.get(bvid)
        if not isinstance(row, dict):
            return False
        at = parse_iso(row.get("at"))
        if not at:
            return True
        return at >= (now or now_local()) - timedelta(days=max(1, days))

    def mark_seen(self, video: Video, status: str = "深读") -> None:
        seen = self.state.setdefault("seen", {})
        if not isinstance(seen, dict):
            seen = {}
            self.state["seen"] = seen
        seen[video.bvid] = {
            "title": video.title,
            "author": video.author,
            "at": iso(),
            "status": status,
        }
        self.prune_seen()
        self.save()

    def prune_seen(self, days: int = 180) -> None:
        seen = self.state.get("seen")
        if not isinstance(seen, dict):
            return
        cutoff = now_local() - timedelta(days=days)
        self.state["seen"] = {
            bvid: row
            for bvid, row in seen.items()
            if (parse_iso((row or {}).get("at")) or now_local()) >= cutoff
        }

    def seen_count(self) -> int:
        seen = self.state.get("seen")
        return len(seen) if isinstance(seen, dict) else 0

    # ── 评论台账（发布前检查用）─────────────────────────────────────────────
    def recent_comments(self, limit: int = 30) -> list[str]:
        """最近自己发过的评论正文；读不到就返回空列表。"""
        try:
            if not self.comments_path.exists():
                return []
            rows: list[str] = []
            for line in self.comments_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue
                body = line.split("·", 1)[1].strip() if "·" in line else line[2:].strip()
                if body:
                    rows.append(body)
            return rows[-max(1, int(limit)):]
        except (OSError, ValueError):
            return []

    def add_comment(self, bvid: str, text: str, day: str | None = None) -> None:
        """记一条评论台账（一行：日期 BVID · 正文）。"""
        body = " ".join(str(text or "").split())
        if not body:
            return
        if not self.comments_path.exists():
            self.comments_path.write_text("# 评论台账（发布前检查用）\n\n", encoding="utf-8")
        with self.comments_path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {day or today_str()} {bvid or '-'} · {body}\n")

    # ── 待确认的分享 ──────────────────────────────────────────────────────
    def set_pending_share(self, payload: dict[str, Any]) -> None:
        self.state["pending_share"] = payload
        self.save()

    def get_pending_share(self, max_age_sec: int = 1800, now: datetime | None = None) -> dict[str, Any] | None:
        pending = self.state.get("pending_share")
        if not isinstance(pending, dict) or not pending.get("bvid"):
            return None
        created = parse_iso(pending.get("created_at"))
        now = now or now_local()
        if not created or (now - created).total_seconds() > max_age_sec:
            return None
        return pending

    def clear_pending_share(self) -> None:
        self.state["pending_share"] = {}
        self.save()

    # ── 文件路径 ──────────────────────────────────────────────────────────
    def report_path(self, day: str | None = None) -> Path:
        return self.report_dir / f"{day or today_str()}.md"

    def diary_path(self, kind: str, day: str | None = None) -> Path:
        kind = "self" if kind in {"self", "自己的", "own"} else "owner"
        return self.diary_dir / kind / f"{day or today_str()}.md"

    def read_report(self, day: str | None = None) -> str:
        path = self.report_path(day)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def read_diary(self, kind: str, day: str | None = None) -> str:
        path = self.diary_path(kind, day)
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def append_diary(self, kind: str, text: str, day: str | None = None) -> Path:
        path = self.diary_path(kind, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if path.exists() else f"# {day or today_str()} · {'给主人的信' if kind != 'self' else '自己的日记'}\n\n"
        with path.open("a", encoding="utf-8") as fh:
            if header:
                fh.write(header)
            elif path.stat().st_size > 0:
                fh.write("\n\n---\n\n")
            fh.write(text.strip() + "\n")
        return path

    def list_reports(self, limit: int = 10) -> list[str]:
        if not self.report_dir.exists():
            return []
        files = sorted(self.report_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.stem for p in files[:limit]]

    def touch_run(self) -> None:
        self.state["last_run"] = iso()
        self.state["runs"] = int(self.state.get("runs", 0)) + 1
        self.save()

    def record_umo(self, umo: str) -> None:
        if umo and self.state.get("last_umo") != umo:
            self.state["last_umo"] = umo
            self.save()
