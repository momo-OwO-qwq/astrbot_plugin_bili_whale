"""和 LivingMemory 插件的桥接。

为什么不直接 import：
- LivingMemory 是独立插件，用户可能没装、没启用、或换了版本；
- 这里只按“插件实例 + memory_engine.add_memory”这个最小契约做鸭子类型访问，
  拿不到就返回明确原因，绝不让刷视频/推送因此失败。
"""

from __future__ import annotations

from typing import Any

from .util import clean_text, truncate

LIVINGMEMORY_NAMES = ("livingmemory", "living_memory", "astrbot_plugin_livingmemory")


def _star_list(context: Any) -> list[Any]:
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return []
    try:
        rows = getter()
    except Exception:
        return []
    return list(rows or [])


def _looks_like_livingmemory(star: Any) -> bool:
    for attr in ("root_dir_name", "name", "display_name", "module_path"):
        value = str(getattr(star, attr, "") or "").lower()
        if any(name in value for name in LIVINGMEMORY_NAMES):
            return True
    return False


def find_livingmemory(context: Any) -> Any | None:
    """找到已激活的 LivingMemory 插件实例；找不到返回 None。"""
    for star in _star_list(context):
        if not _looks_like_livingmemory(star):
            continue
        if not bool(getattr(star, "activated", True)):
            continue
        instance = getattr(star, "star_cls", None)
        if instance is not None:
            return instance
    return None


def get_memory_engine(context: Any) -> Any | None:
    plugin = find_livingmemory(context)
    if plugin is None:
        return None
    initializer = getattr(plugin, "initializer", None)
    engine = getattr(initializer, "memory_engine", None) if initializer else None
    return engine if engine is not None else None


def available(context: Any) -> bool:
    engine = get_memory_engine(context)
    return engine is not None and callable(getattr(engine, "add_memory", None))


def status_text(context: Any) -> str:
    plugin = find_livingmemory(context)
    if plugin is None:
        return "未找到"
    return "✓" if available(context) else "已加载但未初始化"


async def write_memory(
    context: Any,
    *,
    content: str,
    session_id: str,
    importance: float = 0.65,
    metadata: dict[str, Any] | None = None,
    max_chars: int = 500,
) -> dict[str, Any]:
    """写入一条长期记忆，返回 {"ok": bool, "id"/"reason": ...}。"""
    text = truncate(clean_text(content, keep_newlines=False), max(50, int(max_chars)))
    if not text:
        return {"ok": False, "reason": "记忆内容为空"}
    engine = get_memory_engine(context)
    if engine is None:
        return {"ok": False, "reason": "没有找到 LivingMemory（未安装或未启用）"}
    add = getattr(engine, "add_memory", None)
    if not callable(add):
        return {"ok": False, "reason": "LivingMemory 还没初始化好，稍后再试"}
    payload = dict(metadata or {})
    payload.setdefault("source", "astrbot_plugin_bili_whale")
    try:
        memory_id = await add(
            content=text,
            session_id=session_id or None,
            importance=max(0.0, min(1.0, float(importance))),
            metadata=payload,
        )
    except Exception as exc:  # 桥接层不能把宿主插件的异常抛给刷视频流程
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    return {"ok": memory_id is not None, "id": memory_id}


__all__ = [
    "available",
    "find_livingmemory",
    "get_memory_engine",
    "status_text",
    "write_memory",
]
