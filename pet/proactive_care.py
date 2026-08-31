# -*- coding: utf-8 -*-
"""
主动关怀（移植自 dsh-whale-musume v1.8.0）—— 四条触发线：久坐、深夜、卡住、欢迎回来。

铁律是「陪着，不是指挥」：
- 台词只在空闲时播报（工作态绝不插嘴）；忙碌只是累计计时，停下来才结算提醒；
- 关怀之间至少间隔 min_gap 秒，避免变成噪音；
- 深夜劝休息属于关怀保留（不在深夜免打扰之列）。

阈值可用 config.json 的 `proactive_care_thresholds` 覆盖（单位：秒），
留空 {} 则用内置默认。
"""
from __future__ import annotations

import random
import time
from datetime import datetime

# 四组关怀台词（沿用 dsh-whale-musume 的中文文案，可爱为主、提醒不催促）
PROACTIVE_LINES: dict[str, list[str]] = {
    "long-work": [
        "主人已经盯了很久了，眼睛要不要歇一会儿？👀",
        "鲸鱼娘申请中场休息！哪怕只是伸个懒腰也好～",
        "再敲下去尾巴都要打结了，主人起来喝口水吧💧",
        "报告：主人已连续工作很久，鲸鱼娘建议起身活动三十秒",
        "久坐伤身哦，鲸鱼娘先替你伸个懒腰示范一下🐋",
    ],
    "late-night": [
        "很晚了主人，鲸鱼娘有点担心你的黑眼圈🌙",
        "这个点还在写代码，明天的主人会恨今天的主人的……",
        "深夜写的代码容易长 bug，要不要明天再战？",
        "鲸鱼娘困得尾巴都垂下来了，主人也去睡吧😴",
        "夜深了，再撑下去效率会掉的哦，去睡吧～",
    ],
    "stuck": [
        "卡住了吗？要不要先去喝口水，回来可能就想通了💡",
        "鲸鱼娘觉得……换个思路说不定就通了？",
        "在同一个地方转圈圈好久了，主人要不要休息一下再回来🔄",
        "要不要把问题念给鲸鱼娘听听？说出来有时候就想通了🐋",
    ],
    "welcome-back": [
        "主人回来啦！鲸鱼娘等到尾巴都摆酸了～",
        "欢迎回来，主人不在的时候鲸鱼娘有乖乖看家哦🏠",
        "哇主人回来了，快看看我有没有长高一点点🐋",
        "你回来啦，鲸鱼娘的等待终于有回报了🥺",
    ],
}

# 内置默认阈值（秒）——与 dsh-whale-musume 对齐
DEFAULT_THRESHOLDS: dict[str, float] = {
    "long_work_sec": 25 * 60,   # 连续忙 25 分钟 → 久坐提醒
    "night_work_sec": 10 * 60,  # 深夜仍忙 10 分钟 → 劝睡
    "stuck_sec": 8 * 60,        # 同一状态停滞 8 分钟 → 卡住
    "away_sec": 3 * 60,         # 离开 3 分钟 → 欢迎回来
    "min_gap_sec": 15 * 60,     # 关怀间最小间隔 15 分钟
}


def is_late_night(now: float | None = None) -> bool:
    """是否深夜时段（23:00 - 6:00，本地时区）。"""
    h = datetime.now().hour
    return h >= 23 or h < 6


class ProactiveCare:
    """主动关怀状态机。纯逻辑，不依赖 Qt；由调用方（后台线程）每 30 秒 tick 一次。

    每次 `tick` 返回 `(kind, line)` 表示要播报的关怀，或 `None`。
    忙→闲的边沿统一结算"久坐/深夜/卡住"，空闲期间检测"欢迎回来"。
    """

    def __init__(self, thresholds: dict | None = None):
        self.t: dict[str, float] = dict(DEFAULT_THRESHOLDS)
        if isinstance(thresholds, dict):
            for k, v in thresholds.items():
                if isinstance(v, (int, float)) and v > 0:
                    self.t[k] = float(v)
        self.last_at: float = 0.0      # 上次播报（单调时钟秒），冷却用
        self.last_kind: str = ""
        self.busy_since: float | None = None    # 连续忙碌开始
        self.stuck_since: float | None = None   # 当前同签名停滞开始
        self.last_signature: str | None = None  # 最近一次忙碌的"任务签名"
        self.prev_user_ts: float | None = None  # 上次看到的最新用户消息时间戳（毫秒）

    # ------------------------------------------------------------ 外部接口
    def reset(self) -> None:
        self.last_at = 0.0
        self.last_kind = ""
        self.busy_since = None
        self.stuck_since = None
        self.last_signature = None
        self.prev_user_ts = None

    def tick(self, now: float, working: bool, signature: str = "",
             user_ts: float = 0.0):
        """推进状态机。now 为单调时钟秒。

        working —— 信标判定是否在干活（工具运行）；
        signature —— 工作细分（如工具类型），用于"卡住"判断；
        user_ts —— 全局最新一条用户消息的时间戳（毫秒），用于"欢迎回来"。
        """
        if working:
            self._observe_busy(now, signature)
            return None
        # 空闲：结算"刚结束的忙碌"
        result = self._settle_busy(now)
        if result is not None:
            return result
        # 空闲：欢迎回来（消息间隔超过 away_sec）
        return self._check_welcome_back(now, user_ts)

    # ------------------------------------------------------------ 内部
    def _observe_busy(self, now: float, signature: str) -> None:
        if self.busy_since is None:
            self.busy_since = now
            self.stuck_since = now
            self.last_signature = signature
        elif signature != self.last_signature:
            self.last_signature = signature
            self.stuck_since = now

    def _settle_busy(self, now: float):
        if self.busy_since is None:
            return None
        dur = now - self.busy_since
        self.busy_since = None
        # 久坐：连续忙够 25 分钟
        if dur >= self.t["long_work_sec"]:
            return self._say("long-work", now)
        # 深夜：深夜忙够 10 分钟（停下时才提醒，不插嘴）
        if is_late_night(now) and dur >= self.t["night_work_sec"]:
            return self._say("late-night", now)
        # 卡住：最后一段同签名停滞够 8 分钟
        if self.stuck_since is not None and now - self.stuck_since >= self.t["stuck_sec"]:
            return self._say("stuck", now)
        self.stuck_since = None
        return None

    def _check_welcome_back(self, now: float, user_ts: float):
        if user_ts <= 0:
            return None
        if self.prev_user_ts is None:
            self.prev_user_ts = user_ts  # 首次只记录，不误触发
            return None
        if user_ts > self.prev_user_ts:
            gap = user_ts - self.prev_user_ts
            self.prev_user_ts = user_ts
            if gap > self.t["away_sec"] * 1000.0:
                return self._say("welcome-back", now)
        return None

    def _say(self, kind: str, now: float):
        if now - self.last_at < self.t["min_gap_sec"]:
            return None
        lines = PROACTIVE_LINES.get(kind)
        if not lines:
            return None
        self.last_at = now
        self.last_kind = kind
        return kind, random.choice(lines)