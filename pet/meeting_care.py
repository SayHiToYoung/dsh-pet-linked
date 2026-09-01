# -*- coding: utf-8 -*-
"""
会议关怀 —— 按开会时长给桌宠"分档反馈"。

设计（与 proactive_care 的「陪着，不是指挥」同一哲学）：
- 开会时桌宠本来就在躲起来（hidden），所以**不边开会边叨叨**；
- 只在**散会（context 从 meeting 变走）的边沿结算**：按本次连续开会时长选一条关怀台词；
- 同一次会议只结算一次；关怀之间至少间隔 min_gap，避免变成噪音。

档位默认（分钟）：30 / 60 / 120。阈值可用 config.json 的
`meeting_care_thresholds`（分钟列表）覆盖；`meeting_care_enabled` 总开关。
纯逻辑、不依赖 Qt，由调用方（后台 context 线程）每 2 秒 tick 一次。
"""
from __future__ import annotations

import random

# 分档台词（可爱为主、心疼不催促）；档位缺失时向上取最近的下一档
MEETING_CARE_LINES: dict[int, list[str]] = {
    30: [
        "哎呀，主人刚开了半小时的会，辛苦啦～喝口水歇歇吧🥤",
        "半小时会议达成！鲸鱼娘给你揉揉肩～",
        "刚结束半小时的会，主人累不累？我陪你放空一会儿🐋",
    ],
    60: [
        "都开一个钟头的会了，主人辛苦了！快活动活动肩膀～",
        "一小时会议解锁！鲸鱼娘申请给你倒杯温水💧",
        "开了一个小时的会，主人真的辛苦啦，我在呢～",
    ],
    120: [
        "两个小时的马拉松会议……主人你太能撑了，鲸鱼娘心疼！",
        "两个小时！这会是真·超长待机，主人快站起来伸个懒腰🫡",
        "连着开了两小时会，主人辛苦啦，我去给你顺顺尾巴…啊不是，顺顺肩～",
    ],
}

# 内置默认档位（分钟）
DEFAULT_MEETING_THRESHOLDS_MIN: list[int] = [30, 60, 120]

DEFAULT_MIN_GAP_SEC = 15 * 60  # 两条会议关怀之间最小间隔


class MeetingCare:
    """会议时长关怀状态机。纯逻辑，不依赖 Qt。

    每次 `tick(context, now)` 返回 `(kind, line)` 表示要播报的关怀，或 `None`。
    context 为 `meeting` 时只计时；离开 meeting 的边沿按累计时长结算一条台词。
    """

    def __init__(self, thresholds_min=None, enabled: bool = True,
                 min_gap_sec: float = DEFAULT_MIN_GAP_SEC):
        raw = list(thresholds_min) if isinstance(thresholds_min, (list, tuple)) else list(DEFAULT_MEETING_THRESHOLDS_MIN)
        self.thresholds: list[float] = sorted({max(1.0, float(t)) for t in raw if float(t) > 0})
        self.enabled = bool(enabled)
        self.min_gap_sec = max(0.0, float(min_gap_sec))
        self.last_at: float = 0.0   # 上次播报（单调时钟秒），冷却用
        self.last_level: int = 0    # 上次触发的档位（分钟）
        self._meeting_since: float | None = None  # 本次连续会议开始（单调时钟秒）

    def reset(self) -> None:
        self.last_at = 0.0
        self.last_level = 0
        self._meeting_since = None

    def tick(self, context: str, now: float):
        """推进状态机。now 为单调时钟秒；context 为当前情境（'meeting' 等）。"""
        if not self.enabled:
            self._meeting_since = None
            return None
        if context == "meeting":
            if self._meeting_since is None:
                self._meeting_since = now
            return None
        # 非会议：结算刚结束的会议
        if self._meeting_since is None:
            return None
        duration_sec = now - self._meeting_since
        self._meeting_since = None
        level = self._pick_level(duration_sec)
        if level <= 0:
            return None
        if now - self.last_at < self.min_gap_sec:
            return None  # 冷却期内不重复打扰
        lines = MEETING_CARE_LINES.get(level) or MEETING_CARE_LINES.get(
            max(MEETING_CARE_LINES) if MEETING_CARE_LINES else 0
        )
        if not lines:
            return None
        self.last_at = now
        self.last_level = level
        return "meeting-care", random.choice(lines)

    def _pick_level(self, duration_sec: float) -> int:
        """按时长取达到的最高档位（分钟）；未达最低档返回 0。"""
        reached = [t for t in self.thresholds if duration_sec >= t * 60.0]
        return int(max(reached)) if reached else 0
