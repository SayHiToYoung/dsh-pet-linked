# -*- coding: utf-8 -*-
"""配置读取与持久化；兼容旧版平铺 chat_* 字段的迁移。"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from . import catalog


DEFAULT_ANIMATION_GAP_SECONDS = 0.0
DEFAULT_SELF_TALK_MIN_INTERVAL = 20.0
DEFAULT_SELF_TALK_MAX_INTERVAL = 60.0
DEFAULT_SELF_TALK_DURATION_SECONDS = 3.2
DEFAULT_SELF_TALK_TEXTS = [
    "\u597d\u5973\u5b69\u2026\u2026",
    "\u597d\u6a21\u578b\u2026\u2026",
    "\u6b27\u9cb8\u9cb8\u2026\u2026",
    "\u4eca\u5929\u4e5f\u8981\u8ba4\u771f\u5de5\u4f5c\u5440\u3002",
    "\u518d\u966a\u4f60\u4e00\u4f1a\u513f\u3002",
]
DEFAULT_SELF_TALK_BUBBLE_STYLE = "classic_top"
SELF_TALK_BUBBLE_STYLES = {
    "classic_top", "paper_left", "glass_right", "soft_blue_top", "breath_bubble",
}
DEFAULT_CONTEXT_MENU_APPEARANCE = {
    "theme": "system",
    "density": "standard",
    "corner_radius": 12,
    "ui_font": "system",
    "ui_font_size": 13,
    "translucent": True,
    "opacity": 0.94,
    "light_background": "#ffffff",
    "light_foreground": "#171717",
    "light_hover": "#eeeeee",
    "dark_background": "#252525",
    "dark_foreground": "#f3f3f3",
    "dark_hover": "#3a3a3a",
}
DEFAULT_MENU_EASTER_EGG = {
    "enabled": True,
    "title": "厉害了我的鲸",
    "hint": "请点击",
    "avatar": "assets/big_blue_fat_fish/ojingjing.jpg",
    "image_dir": "assets/big_blue_fat_fish",
}
DEFAULT_QUICK_LAUNCH_APPS = [
    {"name": "默认浏览器", "path": "", "kind": "default_browser"},
]


def _clean_color(value, default):
    value = str(value or "").strip()
    if len(value) == 7 and value.startswith("#"):
        try:
            int(value[1:], 16)
            return value.lower()
        except ValueError:
            pass
    return default


def _clean_menu_appearance(value):
    value = value if isinstance(value, dict) else {}
    defaults = DEFAULT_CONTEXT_MENU_APPEARANCE
    theme = str(value.get("theme", "system"))
    density = str(value.get("density", "standard"))
    try:
        radius = int(value.get("corner_radius", 12))
    except (TypeError, ValueError):
        radius = 12
    try:
        font_size = int(value.get("ui_font_size", 13))
    except (TypeError, ValueError):
        font_size = 13
    result = {
        "theme": theme if theme in {"system", "light", "dark"} else "system",
        "density": density if density in {"compact", "standard", "spacious"} else "standard",
        "corner_radius": max(6, min(18, radius)),
        "ui_font": str(value.get("ui_font") or "system")[:80],
        "ui_font_size": max(10, min(18, font_size)),
        "translucent": bool(value.get("translucent", True)),
        "opacity": _float_or_default(value.get("opacity"), 0.94, 0.72, 1.0),
    }
    for key in (
        "light_background", "light_foreground", "light_hover",
        "dark_background", "dark_foreground", "dark_hover",
    ):
        result[key] = _clean_color(value.get(key), defaults[key])
    return result


def _normalize_fun_asset_path(candidate: str, default: str) -> str:
    """绝对路径若指向应用内置 assets 目录，归一化为相对路径。

    旧版设置对话框会把默认相对路径固化成安装目录绝对路径；portable
    目录一移动/自更新即失效。此处在加载时统一还原为 assets/... 相对值。
    """
    candidate = str(candidate or "").strip()
    if not candidate:
        return default
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        return candidate
    assets_root = Path(__file__).resolve().parents[1] / "assets"
    try:
        rel = path.resolve().relative_to(assets_root.resolve())
        # 统一正斜杠：配置值与 legacy 迁移比较、跨平台一致
        return str(Path("assets") / rel).replace("\\", "/")
    except ValueError:
        return candidate


def _clean_menu_easter_egg(value):
    value = value if isinstance(value, dict) else {}
    defaults = DEFAULT_MENU_EASTER_EGG
    avatar = _normalize_fun_asset_path(
        str(value.get("avatar") or defaults["avatar"]).strip()[:500], defaults["avatar"]
    )
    image_dir = _normalize_fun_asset_path(
        str(value.get("image_dir") or defaults["image_dir"]).strip()[:500], defaults["image_dir"]
    )
    return {
        "enabled": bool(value.get("enabled", defaults["enabled"])),
        "title": str(value.get("title") or defaults["title"]).strip()[:40],
        "hint": str(value.get("hint") or defaults["hint"]).strip()[:20],
        "avatar": avatar,
        "image_dir": image_dir,
    }


def _clean_quick_launch_apps(value):
    if not isinstance(value, list):
        return [dict(item) for item in DEFAULT_QUICK_LAUNCH_APPS]
    cleaned = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "application")
        path = str(item.get("path") or "").strip()
        name = str(item.get("name") or "").strip()[:60]
        if kind == "default_browser":
            cleaned.append({"name": name or "默认浏览器", "path": "", "kind": "default_browser"})
        elif path and name:
            cleaned.append({"name": name, "path": path, "kind": "application"})
    return cleaned


def _default_chat_data():
    return {
        "enabled": True,
        "active_provider": "openai-main",
        "default_system_prompt": "\u4f60\u662f\u4e00\u53ea\u53ef\u7231\u7684\u684c\u9762\u5ba0\u7269\uff0c\u8bf7\u7528\u81ea\u7136\u3001\u53cb\u5584\u7684\u4e2d\u6587\u548c\u7528\u6237\u4ea4\u6d41\u3002",
        "history_message_limit": 40,
        "history_char_limit": 24000,
        "providers": {
            "openai-main": {
                "name": "DeepSeek",
                "base_url": "https://api.deepseek.com",
                "chat_path": "/v1/chat/completions",
                "model": "deepseek-v4-flash",
                "api_key_ref": "provider/openai-main",
                "api_key": "",
                "timeout": 60.0,
                "temperature": 0.7,
                "max_tokens": 2048,
            }
        },
    }


def _merge_chat_data(raw):
    result = _default_chat_data()
    raw = raw if isinstance(raw, dict) else {}
    result.update({k: v for k, v in raw.items() if k != "providers"})
    incoming = raw.get("providers")
    if isinstance(incoming, dict) and incoming:
        providers = {}
        for provider_id, provider in incoming.items():
            if isinstance(provider, dict):
                base = dict(_default_chat_data()["providers"].get("openai-main", {}))
                base.update(provider)
                # 非 openai-main provider 未显式写 api_key_ref 时按自身归位，
                # 避免沿用 openai-main 的钥匙串条目（密钥串用/查错 key）
                if not str(base.get("api_key_ref") or "").strip():
                    base["api_key_ref"] = f"provider/{provider_id}"
                providers[str(provider_id)] = base
    else:
        providers = dict(result["providers"])
    result["providers"] = providers or _default_chat_data()["providers"]
    active = str(result.get("active_provider") or "")
    result["active_provider"] = active if active in result["providers"] else next(iter(result["providers"]))
    return result


def _default_base():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home())
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


def _app_dir_name() -> str:
    """打包变体的独立数据目录名；源码运行时回退到共享目录。

    构建脚本（scripts/build_onedir.ps1）会在打包前生成
    packaging/build_variant.py（VARIANT = "webm-chat" 等），
    使 Chat / 无 Chat 等变体各自使用独立的配置目录、会话与自启项。
    """
    try:
        from build_variant import VARIANT  # 仅打包产物中存在
        name = str(VARIANT).strip()
        if name:
            return f"dsh-pet-standalone-{name}"
    except Exception:
        pass
    return "dsh-pet-standalone"


APP_DIR_NAME = _app_dir_name()


def _float_or_default(value, default, minimum, maximum):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _clean_self_talk_texts(value):
    if not isinstance(value, list):
        return list(DEFAULT_SELF_TALK_TEXTS)
    texts = []
    for item in value:
        text = str(item).strip()
        if text and text not in texts:
            texts.append(text[:120])
    return texts or list(DEFAULT_SELF_TALK_TEXTS)


def _default_proactive_screen_data() -> dict:
    """主动识屏配置默认值（上游 proactive.py 移植）。"""
    return {
        "enabled": False,
        "dry_run": False,
        "preset": "balanced",
        "allow_when_mouse_through": True,
        "whitelist": [],
        "dwell_seconds": 45,
        "require_idle": False,
        "min_idle_seconds": 30,
        "cooldown_minutes": 5,
        "daily_cap": 15,
        "min_request_interval_seconds": 60,
        "change_threshold": 8,
        "prefer_free_provider": True,
        "pre_cue": True,
    }


def _merge_proactive_screen_data(raw: Any) -> dict:
    result = _default_proactive_screen_data()
    if isinstance(raw, dict):
        result.update(raw)
    return result


def _default_agent_link_data() -> dict:
    """多 Agent 联动配置默认值（上游 agent_link.py 移植）。"""
    return {
        "dsh": False,
        "claude": False,
        "cursor": False,
        "opencode": False,
        # 自定义联动 Agent（协议见 docs/AGENT_LINK_PROTOCOL.md §4）：只读监听
        # 用户指定的事件文件，不写外部配置、无需授权弹窗，默认空
        "custom_agents": [],
        # 联动气泡：开始干活提醒（可选，默认关）、任务完成通知（默认开）
        "notify_state": False,
        "notify_done": True,
        # 过程汇报（可选，默认关）：Agent 干活中报「正在读文件/跑命令/改代码…」
        "notify_activity": False,
        # 音效配置
        "sound_enabled": False,
        "sound_start_path": "builtin:agent-start",
        "sound_done_path": "builtin:agent-done",
        "sound_error_path": "builtin:agent-error",
        "sound_volume": 0.65,
        "sound_cooldown_seconds": 2.0,
        "sound_start_enabled": True,
        "sound_done_enabled": True,
        "sound_error_enabled": True,
    }


# 内置联动 Agent 键：custom_agents 的 key 不得与之重复
_AGENT_LINK_BUILTIN_KEYS = ("dsh", "claude", "cursor", "opencode")
# 自定义联动 Agent 条目上限（防配置文件被塞爆）
_CUSTOM_AGENT_MAX = 8


def _clean_custom_agents(raw: Any) -> list[dict]:
    """清洗自定义联动 Agent 列表（agent_link.custom_agents）。

    条目 {key, name, path}：key 为小写标识（不得与内置键/其他条目重复），
    name 为显示名（缺省用 key），path 为事件文件路径（支持 ~，允许暂不存在）。
    非法条目直接丢弃，超出上限截断。"""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if len(result) >= _CUSTOM_AGENT_MAX:
            break
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", key):
            continue
        if key in _AGENT_LINK_BUILTIN_KEYS or key in seen:
            continue
        path = str(item.get("path") or "").strip()[:500]
        if not path:
            continue
        name = str(item.get("name") or "").strip()[:50] or key
        seen.add(key)
        result.append({"key": key, "name": name, "path": path})
    return result


def _clean_agent_link_data(raw: Any) -> dict:
    defaults = _default_agent_link_data()
    if not isinstance(raw, dict):
        return dict(defaults)
    result = dict(defaults)
    # 保留传入的额外合法键（例如 thinking_text, thinking_texts 等）
    result.update(raw)
    result["custom_agents"] = _clean_custom_agents(raw.get("custom_agents"))
    for key in (
        "dsh", "claude", "cursor", "opencode", "notify_state", "notify_done", "notify_activity",
        "sound_enabled", "sound_start_enabled", "sound_done_enabled", "sound_error_enabled",
    ):
        if key in raw:
            result[key] = bool(raw[key])
    for key in ("sound_start_path", "sound_done_path", "sound_error_path"):
        if key in raw:
            val = str(raw[key] or "").strip()[:500]
            result[key] = val or defaults[key]
    if "sound_volume" in raw:
        result["sound_volume"] = _float_or_default(raw.get("sound_volume"), defaults["sound_volume"], 0.0, 1.0)
    if "sound_cooldown_seconds" in raw:
        result["sound_cooldown_seconds"] = _float_or_default(
            raw.get("sound_cooldown_seconds"), defaults["sound_cooldown_seconds"], 0.0, 30.0
        )
    return result


class Config:
    def __init__(self, base=None):
        base = Path(base) if isinstance(base, str) else (base or _default_base())
        self.dir = base / APP_DIR_NAME
        self.path = self.dir / "config.json"
        self._migrate_legacy_config(base)
        self.data = {
            "version": 4,
            "rx": None,
            "ry": None,
            "screen_name": None,
            "facing": "left",
            "scale": catalog.DEFAULT_SCALE,
            "on_top": True,
            "show_dock_icon": True,
            "no_move": False,
            "character": catalog.DEFAULT_CHARACTER,
            "playback_speed": 1.0,
            "animation_gap_seconds": DEFAULT_ANIMATION_GAP_SECONDS,
            "self_talk_enabled": False,
            "self_talk_min_interval": DEFAULT_SELF_TALK_MIN_INTERVAL,
            "self_talk_max_interval": DEFAULT_SELF_TALK_MAX_INTERVAL,
            "self_talk_duration_seconds": DEFAULT_SELF_TALK_DURATION_SECONDS,
            "self_talk_texts": list(DEFAULT_SELF_TALK_TEXTS),
            "self_talk_image_dir": "assets/big_blue_fat_fish",
            "self_talk_bubble_style": DEFAULT_SELF_TALK_BUBBLE_STYLE,
            "mouse_through": False,
            "drag_physics": False,
            "context_menu_template": "modern",
            "context_menu_appearance": dict(DEFAULT_CONTEXT_MENU_APPEARANCE),
            "menu_easter_egg": dict(DEFAULT_MENU_EASTER_EGG),
            "quick_launch_apps": [dict(item) for item in DEFAULT_QUICK_LAUNCH_APPS],
            "auto_hide_fullscreen": True,  # 全屏应用自动隐藏（Windows）
            "click_sound_enabled": True,   # 点击 Q 弹音效
            "click_sound_path": "",        # 自定义点击音效文件绝对路径（空=内置默认）
            "click_show_balance": False,   # 点击显示 DeepSeek 余额
            "click_show_self_talk": False, # 点击随机显示自定义自言自语
            "balance_refresh_minutes": 0,  # DeepSeek 余额自动刷新间隔（分钟，0=关闭）
            "autostart_wanted": False,     # 用户曾开启过开机自启（用于启动自检：被安全软件清理时提醒）
            "stream_capture_mode": False,  # 直播捕获兼容模式（Windows：Tool 窗口直播姬/OBS 枚举不到）
            "chat_background": "",  # 肥鱼牌小手机背景：空=纯色；builtin:* = 内置主题；否则为图片路径
            "modern_chat_background": "",  # 肥鱼版 DeepSeek 背景：空=纯色；否则为自定义图片路径
            "chat_background_opacity": 100,
            "chat_background_fill": "cover",
            "modern_chat_background_opacity": 100,
            "modern_chat_background_fill": "cover",
            "modern_chat_card_opacity": 84,
            "chat_bg_crops": {},    # 每个背景的用户自定义取景框 {背景标识: [x,y,w,h] 归一化}
            "chat_ui_style": "modern",  # modern / classic（仅聊天窗口保留双实现）
            "chat": _default_chat_data(),
            "token_pricing": {},       # 用户覆盖价格表：{模型前缀: {"peak": {...}, "off": {...}}}（USD/百万）
            "token_peak_hours": [],    # 用户覆盖高峰窗口：[[起, 止], ...]（UTC 小时）；空=官方窗口
            "token_period": "all",     # Token 花费时间筛选：all / day / week / month
            "proactive_care_enabled": True,   # 主动关怀总开关（久坐/深夜/卡住/欢迎回来）
            "proactive_care_thresholds": {},  # 主动关怀阈值覆盖（秒）：{long_work_sec/night_work_sec/stuck_sec/away_sec/min_gap_sec}；空=内置默认
            "context_aware_enabled": True,    # 情境感知总开关（监听前台应用/进程）
            "context_focus_enabled": False,   # 「别打扰我」手动开关：强制 focus 情境（躲起来）
            "context_rules": {},              # 用户覆盖规则：{meeting/gaming/work: [关键词...]}；空=内置默认
            "meeting_care_enabled": True,     # 会议关怀总开关（按开会时长分档反馈）
            "meeting_care_thresholds": [],    # 会议关怀档位（分钟）：[30,60,120]；空=内置默认
            "memory_collection_enabled": True,       # 桌面活动记忆总开关
            "memory_collect_window_titles": True,    # macOS 需辅助功能权限；无权限自动降级
            "memory_idle_seconds": 180,              # 键鼠空闲多久后停止累计前台应用
            "memory_min_segment_seconds": 20,        # 过滤 Alt-Tab 等短暂活动
            "memory_project_roots": [],              # 可选项目根目录；只在这些目录读取 README
            "memory_workday_end": "18:00",          # 当日可见“记下啦”提示时间
            "memory_sync_enabled": False,            # 默认只落本地；明确开启后才向服务端同步
            "memory_sync_url": "http://127.0.0.1:47821",
            "memory_sync_token": "local-dev-token", # 本地模拟服务配对口令；正式远端必须 HTTPS
            "memory_sync_user_id": "local-user",    # MVP 单用户标识
            "memory_sync_device_id": "",            # 首次同步自动生成并持久化
            "memory_sync_interval_seconds": 30,
            "proactive_screen": _default_proactive_screen_data(),  # 主动识屏（上游移植）
            "agent_link": _default_agent_link_data(),             # 多 Agent 联动（上游移植）
        }
        self._load()
        self._normalize_pet_settings()

    def _migrate_legacy_config(self, base) -> None:
        """旧版各变体共用 %APPDATA%/dsh-pet-standalone；升级后首次运行时
        把该目录的 config.json 与 sessions/ 一次性复制到变体独立目录，
        避免用户设置与聊天会话“消失”。仅在新目录尚不存在时执行。"""
        if APP_DIR_NAME == "dsh-pet-standalone" or self.path.exists():
            return
        legacy = base / "dsh-pet-standalone"
        if not (legacy / "config.json").is_file():
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy / "config.json", self.path)
            src_sessions = legacy / "sessions"
            if src_sessions.is_dir():
                shutil.copytree(src_sessions, self.dir / "sessions", dirs_exist_ok=True)
        except OSError:
            pass

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        try:
            old_version = int(raw.get("version", 1) or 1)
        except (TypeError, ValueError):
            old_version = 1  # 脏数据（手改/损坏）不得导致启动崩溃
        if old_version < 2:
            raw.pop("scale", None)
        chat = raw.get("chat") if isinstance(raw.get("chat"), dict) else {}
        legacy = {}
        if "chat_enabled" in raw:
            legacy["enabled"] = raw["chat_enabled"]
        if "chat_system_prompt" in raw:
            legacy["default_system_prompt"] = raw["chat_system_prompt"]
        legacy_provider = {}
        if raw.get("chat_api_url"):
            legacy_provider["base_url"] = raw["chat_api_url"]
        if raw.get("chat_model"):
            legacy_provider["model"] = raw["chat_model"]
        if raw.get("chat_api_key"):
            legacy_provider["api_key"] = raw["chat_api_key"]
        if legacy_provider:
            legacy["providers"] = {"openai-main": legacy_provider}
        merged = dict(legacy)
        merged.update(chat)
        self.data["chat"] = _merge_chat_data(merged)
        for key in (
            "rx", "ry", "screen_name", "facing", "scale", "on_top", "show_dock_icon", "no_move", "character",
            "playback_speed", "animation_gap_seconds", "self_talk_enabled",
            "self_talk_min_interval", "self_talk_max_interval", "self_talk_texts",
            "self_talk_duration_seconds", "self_talk_image_dir",
            "self_talk_bubble_style",
            "mouse_through", "drag_physics", "context_menu_template",
            "context_menu_appearance", "quick_launch_apps",
            "menu_easter_egg", "auto_hide_fullscreen",
            "click_sound_enabled", "click_sound_path",
            "click_show_balance", "click_show_self_talk",
            "balance_refresh_minutes", "autostart_wanted", "stream_capture_mode",
            "chat_background", "modern_chat_background",
            "chat_background_opacity", "chat_background_fill",
            "modern_chat_background_opacity", "modern_chat_background_fill",
            "modern_chat_card_opacity",
            "chat_bg_crops",
            "chat_ui_style",
            "token_display_fields", "token_display_scopes", "token_display_format",
            "token_pricing", "token_peak_hours", "token_period",
            "proactive_care_enabled", "proactive_care_thresholds",
            "context_aware_enabled", "context_focus_enabled", "context_rules",
            "meeting_care_enabled", "meeting_care_thresholds",
            "memory_collection_enabled", "memory_collect_window_titles",
            "memory_idle_seconds", "memory_min_segment_seconds",
            "memory_project_roots", "memory_workday_end",
            "memory_sync_enabled", "memory_sync_url", "memory_sync_token",
            "memory_sync_user_id", "memory_sync_device_id",
            "memory_sync_interval_seconds",
        ):
            if key in raw and raw[key] is not None:
                self.data[key] = raw[key]
        # 嵌套配置组：走清洗合并，防脏数据（上游 proactive/agent_link 移植）
        if "proactive_screen" in raw:
            self.data["proactive_screen"] = _merge_proactive_screen_data(raw["proactive_screen"])
        if "agent_link" in raw:
            self.data["agent_link"] = _clean_agent_link_data(raw["agent_link"])
        self.data["version"] = 4

    def _normalize_pet_settings(self):
        self.data["playback_speed"] = _float_or_default(self.data.get("playback_speed"), 1.0, 0.1, 8.0)
        self.data["animation_gap_seconds"] = _float_or_default(
            self.data.get("animation_gap_seconds"), DEFAULT_ANIMATION_GAP_SECONDS, 0.0, 3600.0
        )
        minimum = _float_or_default(
            self.data.get("self_talk_min_interval"), DEFAULT_SELF_TALK_MIN_INTERVAL, 5.0, 3600.0
        )
        maximum = _float_or_default(
            self.data.get("self_talk_max_interval"), DEFAULT_SELF_TALK_MAX_INTERVAL, 5.0, 3600.0
        )
        self.data["self_talk_min_interval"] = min(minimum, maximum)
        self.data["self_talk_max_interval"] = max(minimum, maximum)
        self.data["self_talk_duration_seconds"] = _float_or_default(
            self.data.get("self_talk_duration_seconds"),
            DEFAULT_SELF_TALK_DURATION_SECONDS,
            1.0,
            300.0,
        )
        self.data["self_talk_image_dir"] = str(
            self.data.get("self_talk_image_dir") or ""
        ).strip()[:500]
        self.data["self_talk_enabled"] = bool(self.data.get("self_talk_enabled", False))
        self.data["show_dock_icon"] = bool(self.data.get("show_dock_icon", True))
        self.data["self_talk_texts"] = _clean_self_talk_texts(self.data.get("self_talk_texts"))
        bubble_style = str(self.data.get("self_talk_bubble_style") or "")
        self.data["self_talk_bubble_style"] = (
            bubble_style if bubble_style in SELF_TALK_BUBBLE_STYLES
            else DEFAULT_SELF_TALK_BUBBLE_STYLE
        )
        if self.data.get("context_menu_template") not in {"legacy", "modern"}:
            self.data["context_menu_template"] = "modern"
        self.data["context_menu_appearance"] = _clean_menu_appearance(
            self.data.get("context_menu_appearance")
        )
        self.data["menu_easter_egg"] = _clean_menu_easter_egg(
            self.data.get("menu_easter_egg")
        )
        self.data["quick_launch_apps"] = _clean_quick_launch_apps(
            self.data.get("quick_launch_apps")
        )
        if self.data.get("chat_ui_style") not in {"modern", "classic"}:
            self.data["chat_ui_style"] = "modern"
        for prefix in ("chat_background", "modern_chat_background"):
            opacity_key = f"{prefix}_opacity"
            fill_key = f"{prefix}_fill"
            try:
                opacity = int(self.data.get(opacity_key, 100))
            except (TypeError, ValueError):
                opacity = 100
            self.data[opacity_key] = max(10, min(100, opacity))
            fill = str(self.data.get(fill_key, "cover") or "cover")
            self.data[fill_key] = fill if fill in {"cover", "contain", "stretch"} else "cover"
        try:
            card_opacity = int(self.data.get("modern_chat_card_opacity", 84))
        except (TypeError, ValueError):
            card_opacity = 84
        self.data["modern_chat_card_opacity"] = max(10, min(100, card_opacity))

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        if key in {
            "playback_speed", "animation_gap_seconds", "self_talk_enabled",
            "self_talk_min_interval", "self_talk_max_interval", "self_talk_texts",
            "self_talk_duration_seconds", "self_talk_image_dir",
            "self_talk_bubble_style",
            "context_menu_appearance", "quick_launch_apps",
            "menu_easter_egg",
        }:
            self._normalize_pet_settings()

    def chat_settings(self):
        from .chat.models import ChatSettings
        return ChatSettings.from_dict(self.data.get("chat", {}))

    def set_chat_settings(self, settings):
        self.data["chat"] = settings.to_dict(include_secrets=True)

    def resolve_api_key(self, provider):
        from .chat.models import SecretStore
        return SecretStore().get(provider.api_key_ref) or provider.api_key

    def save(self):
        try:
            self._normalize_pet_settings()
            self.dir.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            pass
