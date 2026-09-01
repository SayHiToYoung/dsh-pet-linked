# -*- coding: utf-8 -*-
"""
情境感知 connector —— 监听「前台是哪个 App」，产出统一的 context 事件。

产品语义（见 docs/情境感知-无处不在-2026-09-01.md）：
- 只读「前台应用」身份，**不读内容、不碰隐私**（与「读微信聊天」截然不同）。
- 产出情境：meeting(开会) > gaming(游戏) > work(工作) > focus(专注) > idle(空闲)。
  focus 由用户手动「别打扰」开关触发，不由 App 匹配产生；本模块只保留常量。

技术：
- macOS：`lsappinfo`（LaunchServices 自带，无需辅助功能/Screen Recording 权限）。
- Windows：`GetForegroundWindow` + `QueryFullProcessImageNameW`（ctypes，零依赖）。
- 其它平台：回退 idle。
- 防抖：同一候选持续 `debounce_seconds` 才真正切换，避免 Alt-Tab 快速切换导致闪跳。

本模块纯逻辑与平台探测分离：`classify_context` / `ContextAwareMonitor` 都接受可注入的
`detector`，便于单测（不依赖真实桌面）。
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from typing import Callable

log = logging.getLogger('dsh-pet-standalone')

# 情境常量（优先级从低到高）
IDLE = "idle"
FOCUS = "focus"
WORK = "work"
GAMING = "gaming"
MEETING = "meeting"

CONTEXTS = (IDLE, FOCUS, WORK, GAMING, MEETING)
# 冲突时定死优先级：会议 > 游戏 > 工作 > 专注 > 空闲
_CONTEXT_ORDER = (MEETING, GAMING, WORK, FOCUS, IDLE)

DEFAULT_DEBOUNCE_SECONDS = 2.5

# 纯会议 App（只会拿来开会，命中即开会）：这些应用一在前台就是开会。
MEETING_APPS = ("wemeet", "腾讯会议", "tencent meeting", "zoom", "google meet")
# 聊天+会议二合一工作 IM（钉钉 / Teams / 飞书 / 企业微信）：开在桌面≠开会，
# 回个消息不该把桌宠藏起来。只有**窗口标题**带会议特征词才算开会；
# 否则按工作 IM 陪伴处理（它们也在 WORK 规则里）。
WORK_IM_TOKENS = ("钉钉", "dingtalk", "teams", "飞书", "feishu", "企业微信", "wecom")
# 标题里的会议特征词：命中即视为「真在开会」。
MEETING_TITLE_KEYWORDS = (
    "会议", "视频会议", "音视频会议", "语音会议", "开会", "meeting", "conference",
)

# 每个情境的匹配关键词（小写后对「名称 + bundle id + 窗口标题」做子串匹配）。
# 只有会「独占前台」的应用才该进表；进表越克制，误判越少。
DEFAULT_RULES: dict[str, list[str]] = {
    MEETING: list(MEETING_APPS),
    GAMING: [
        "steam", "epic games", "battle.net", "wegame",
        "league of legends", "英雄联盟", "dota", "cs:go", "cs2",
        "minecraft", "我的世界", "genshin", "原神", "崩坏", "starrail",
        "overwatch", "永劫无间", "naraka", "pubg", "绝地求生",
    ],
    WORK: [
        # DSH / Harness 是桌宠的主战场，前台即「工作陪伴」
        "dsh", "deepseek", "harness",
        # 编辑器 / IDE
        "visual studio code", "cursor", "pycharm", "webstorm", "intellij",
        "xcode", "sublime", "vim", "neovim",
        # 终端
        "terminal", "iterm", "alacritty", "wezterm", "konsole",
        "powershell", "cmd.exe", "windows terminal", "warp",
        # 聊天+会议二合一工作 IM：开在桌面=工作陪伴（真开会靠标题特征词识别）
        *WORK_IM_TOKENS,
        # 常见工作类
        "notion", "obsidian", "figma", "postman",
    ],
}

_LSAPPINFO_KEY = re.compile(r'"([^"]+)"="([^"]*)"')


def _lower_tokens(rules: dict | None) -> dict[str, list[str]]:
    cleaned: dict[str, list[str]] = {}
    source = rules if isinstance(rules, dict) else DEFAULT_RULES
    for ctx in _CONTEXT_ORDER:
        tokens = source.get(ctx) if isinstance(source.get(ctx), list) else []
        seen: list[str] = []
        for token in tokens:
            text = str(token).strip().lower()
            if text and text not in seen:
                seen.append(text)
        if seen:
            cleaned[ctx] = seen
    return cleaned


def _extract_lsappinfo(text: str, key: str) -> str:
    for k, v in _LSAPPINFO_KEY.findall(text):
        if k.lower() == key.lower():
            return v.strip()
    return ""


def _macos_foreground() -> dict:
    """lsappinfo：前台应用的 ASN → 显示名 + bundle id。无需特殊权限。"""
    try:
        front = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=2.0
        )
        asn = (front.stdout or "").strip()
        if not asn:
            return {}
        info = subprocess.run(
            ["lsappinfo", "info", "-only", "name", "-only", "bundleID", asn],
            capture_output=True, text=True, timeout=2.0,
        )
        out = info.stdout or ""
        name = _extract_lsappinfo(out, "LSDisplayName") or _extract_lsappinfo(out, "name")
        bundle = _extract_lsappinfo(out, "CFBundleIdentifier") or _extract_lsappinfo(out, "bundleID")
        return {"name": name, "bundle": bundle, "title": "", "platform": "darwin"}
    except Exception:
        log.debug("macOS 前台应用探测失败", exc_info=True)
        return {}


def _windows_foreground() -> dict:
    """GetForegroundWindow → 进程名 + 窗口标题（ctypes，零依赖）。"""
    try:
        import ctypes
        from pathlib import Path
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {}
        title = ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = (buf.value or "").strip()
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        if pid.value:
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                try:
                    pbuf = ctypes.create_unicode_buffer(260)
                    size = ctypes.c_ulong(260)
                    if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                        proc = Path(pbuf.value).name
                finally:
                    kernel32.CloseHandle(h)
        return {"name": proc, "bundle": proc, "title": title, "platform": "win32"}
    except Exception:
        log.debug("Windows 前台应用探测失败", exc_info=True)
        return {}


def detect_foreground_app() -> dict:
    """返回前台应用身份：{name, bundle, title, platform}；拿不到返回空 dict。"""
    if sys.platform == "darwin":
        return _macos_foreground()
    if sys.platform == "win32":
        return _windows_foreground()
    return {}  # Linux 等回退 idle


def _app_haystack(app: dict) -> str:
    if not isinstance(app, dict):
        return ""
    parts = [
        str(app.get("name") or ""),
        str(app.get("bundle") or ""),
        str(app.get("title") or ""),
    ]
    return " ".join(parts).lower()


def classify_context(app: dict, rules: dict | None = None) -> str:
    """把前台应用身份归类为情境；命中优先级 meeting > gaming > work，否则 idle。

    关键：开会不只认「App 是谁」，还要认「窗口标题在开什么」——
    钉钉/Teams/飞书 这种聊天+会议二合一的 App，光在前台≠开会。
    回个消息不该把桌宠藏起来：只有**标题带会议特征词**（「会议」「meeting」…）
    才算开会；否则按工作 IM 陪伴处理。纯会议 App（Zoom/腾讯会议）一在前台即开会。
    """
    if not isinstance(app, dict):
        return IDLE
    haystack = _app_haystack(app)
    if not haystack:
        return IDLE
    title = str(app.get("title") or "").lower()

    # 1) 聊天+会议二合一工作 IM：标题带会议特征词 → 真在开会（否则落到工作 IM）
    if any(token in haystack for token in WORK_IM_TOKENS):
        if any(keyword in title for keyword in MEETING_TITLE_KEYWORDS):
            return MEETING

    # 2) 纯会议 App：一在前台即开会
    for token in _lower_tokens(rules).get(MEETING, []):
        if token in haystack:
            return MEETING

    # 3) 其余：gaming > work
    for ctx in (GAMING, WORK):
        for token in _lower_tokens(rules).get(ctx, []):
            if token in haystack:
                return ctx
    return IDLE


def behavior_for(context: str) -> dict:
    """情境 → 桌宠行为决策（纯函数，便于单测与 P2 设置界面复用）。

    - meeting / focus：隐藏（躲起来/隐身）
    - gaming：安静（不移动、不闲聊、不打扰），但保持可见
    - 其它（work / idle）：正常陪伴
    """
    ctx = str(context or IDLE)
    return {
        "hidden": ctx in (MEETING, FOCUS),
        "quiet": ctx == GAMING,
    }


def context_blocks_interrupt(context: str) -> bool:
    """统一裁决：该情境是否禁止一切打扰（气泡 / 动画 / 办公区镜像）。

    meeting/focus（躲起）与 gaming（安静）都压过 office 镜像与 DOM 信标——
    这是「统一事件流」的最高优先级（情境 > office > 信标）。
    """
    return str(context or IDLE) in (MEETING, FOCUS, GAMING)


class ContextAwareMonitor:
    """周期性采样前台应用，防抖后产出稳定的 context 事件。

    - `detector`：可注入的探测函数（默认 detect_foreground_app）。
    - `on_change(context, app)`：仅当情境**真正切换**后回调一次（在调用线程内，非锁内）。
    - `debounce_seconds`：候选须持续这么久才提交。
    - `rules`：dict 或返回 dict 的 callable；dict 表示固定规则，callable 每次采样重新取值
      （用于设置界面改完关键词后无需重启）。
    - `enabled` / `focus_override`：返回 bool 的 callable；关闭时按 idle，focus 开时强制 focus。
    """

    def __init__(self, detector: Callable[[], dict] | None = None,
                 on_change: Callable[[str, dict], None] | None = None,
                 rules: dict | Callable[[], dict] | None = None,
                 enabled: Callable[[], bool] | None = None,
                 focus_override: Callable[[], bool] | None = None,
                 debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS):
        self._detector = detector or detect_foreground_app
        self._on_change = on_change
        self._rules_static: dict | None = rules if isinstance(rules, dict) else None
        self._rules_fn: Callable[[], dict] | None = rules if callable(rules) and not isinstance(rules, dict) else None
        self._enabled = enabled
        self._focus_override = focus_override
        self._debounce = max(0.0, float(debounce_seconds))
        self._lock = threading.Lock()
        self._current = IDLE
        self._current_app: dict = {}
        self._candidate = IDLE
        self._candidate_app: dict = {}
        self._candidate_since: float | None = None

    def _resolve_rules(self) -> dict | None:
        """取当前规则（callable 每次重新取值；异常/空回退内置默认）。"""
        if self._rules_fn is not None:
            try:
                return self._rules_fn()
            except Exception:
                log.debug("情境规则 callable 取值失败，回退默认", exc_info=True)
                return None
        return self._rules_static

    @property
    def context(self) -> str:
        with self._lock:
            return self._current

    def snapshot(self) -> dict:
        with self._lock:
            return {"context": self._current, "app": dict(self._current_app)}

    def sample(self, now: float | None = None) -> dict:
        """采样一次；返回快照 + 本次是否发生切换。"""
        now = time.monotonic() if now is None else now
        app: dict = {}
        try:
            app = self._detector() or {}
        except Exception:
            log.debug("情境探测异常，按 idle 处理", exc_info=True)
            app = {}
        if self._enabled is not None and not self._enabled():
            context = IDLE
        elif self._focus_override is not None and self._focus_override():
            context = FOCUS
        else:
            context = classify_context(app, self._resolve_rules())

        callback = None
        with self._lock:
            if context == self._current:
                # 回到当前情境（或一直没变）：清候选，保持稳定
                self._candidate = IDLE
                self._candidate_app = {}
                self._candidate_since = None
                changed = False
            elif self._debounce <= 0.0:
                # 无防抖：首次观察到新情境即提交
                self._current = context
                self._current_app = app
                self._candidate = IDLE
                self._candidate_app = {}
                self._candidate_since = None
                changed = True
                callback = self._on_change
            elif context != self._candidate:
                # 新候选：开始计时
                self._candidate = context
                self._candidate_app = app
                self._candidate_since = now
                changed = False
            elif self._candidate_since is not None and (now - self._candidate_since) >= self._debounce:
                # 候选坚持够久：提交
                self._current = context
                self._current_app = app
                self._candidate = IDLE
                self._candidate_app = {}
                self._candidate_since = None
                changed = True
                callback = self._on_change
            else:
                changed = False
            snapshot = {"context": self._current, "app": dict(self._current_app)}

        if changed and callback is not None:
            try:
                callback(context, app)
            except Exception:
                log.exception("context 变化回调失败")
        snapshot["changed"] = changed
        return snapshot
