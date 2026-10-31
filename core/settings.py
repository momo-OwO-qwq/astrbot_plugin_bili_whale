"""配置迁移：合并旧分组，并给 report/diary/share 这类改名提供兼容读取。

AstrBot 会按 `_conf_schema.json` 自动保留旧键、隐藏旧分组；插件启动时把旧值搬进
新键，界面上就只剩清晰的新分组。新键已经有非默认值时以新键为准。
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping

OUTPUT_DEFAULTS: dict[str, Any] = {
    "report_detail": "full",
    "push_full_report": False,
    "diary_enabled": True,
    "push_diary": False,
    "diary_private_only": True,
}

SHARE_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "auto_send": False,
    "auto_on_manual": True,
}

# 新键 -> (旧分组, 旧键)
_LEGACY_MAP: dict[str, tuple[str, str]] = {
    "report_detail": ("report", "detail"),
    "push_full_report": ("report", "push_full"),
    "diary_enabled": ("diary", "enabled"),
    "push_diary": ("diary", "push"),
    "diary_private_only": ("diary", "private_only"),
}


def _section(cfg: MutableMapping[str, Any], name: str) -> dict[str, Any]:
    value = cfg.get(name)
    if not isinstance(value, dict):
        value = {}
        cfg[name] = value
    return value


def migrate_output(config: MutableMapping[str, Any]) -> bool:
    """返回是否发生了迁移；可重复调用。"""
    output = _section(config, "output")
    changed = False
    for new_key, (old_group, old_key) in _LEGACY_MAP.items():
        old = config.get(old_group)
        if not isinstance(old, Mapping):
            continue
        old_value = old.get(old_key)
        if old_value is None:
            continue
        current = output.get(new_key)
        if (new_key not in output or current == OUTPUT_DEFAULTS[new_key]) and old_value != OUTPUT_DEFAULTS[new_key]:
            output[new_key] = old_value
            changed = True
    # 把旧分组同步成默认值：否则用户以后把新值调回默认时，下一次启动又被旧值盖回去。
    for new_key, (old_group, old_key) in _LEGACY_MAP.items():
        old = config.get(old_group)
        if isinstance(old, MutableMapping) and old_key in old:
            old[old_key] = OUTPUT_DEFAULTS[new_key]
    return changed


def migrate_share(config: MutableMapping[str, Any]) -> bool:
    """把旧 share.mode 迁移成 enabled + auto_send；可重复调用。"""
    share = _section(config, "share")
    mode = str(share.get("mode") or "off").strip().lower()
    if mode not in {"off", "manual", "auto"}:
        return False
    enabled = mode != "off"
    auto = mode == "auto"
    changed = False
    if "enabled" not in share or share.get("enabled") == SHARE_DEFAULTS["enabled"]:
        if share.get("enabled") != enabled:
            share["enabled"] = enabled
            changed = True
    if "auto_send" not in share or share.get("auto_send") == SHARE_DEFAULTS["auto_send"]:
        if share.get("auto_send") != auto:
            share["auto_send"] = auto
            changed = True
    # 迁移标记：旧 mode 归零，避免用户以后改新开关又被旧值覆盖。
    if share.get("mode") != "off":
        share["mode"] = "off"
        changed = True
    return changed


def migrate_push(config: MutableMapping[str, Any]) -> bool:
    """旧版单目标 push.session 合并进 push.targets，并把旧字段清空。"""
    push = _section(config, "push")
    legacy = str(push.get("session") or "").strip()
    if not legacy:
        return False
    targets = push.get("targets")
    if not isinstance(targets, list):
        targets = []
    if legacy not in targets:
        targets.append(legacy)
    push["targets"] = targets
    push["session"] = ""
    return True


def output_setting(config: Mapping[str, Any], key: str) -> Any:
    """读 output 配置；旧配置没迁移成功时也能回退。"""
    output = config.get("output")
    if isinstance(output, Mapping) and key in output:
        return output.get(key)
    old_group, old_key = _LEGACY_MAP.get(key, ("", ""))
    old = config.get(old_group)
    if isinstance(old, Mapping) and old_key in old:
        return old.get(old_key)
    return OUTPUT_DEFAULTS.get(key)
