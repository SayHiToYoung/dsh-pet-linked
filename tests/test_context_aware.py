# -*- coding: utf-8 -*-
"""情境感知 connector 单测（纯逻辑 + 防抖状态机，不依赖真实桌面）。"""
from pet.context_aware import (
    ContextAwareMonitor, classify_context, behavior_for, context_blocks_interrupt,
    IDLE, WORK, GAMING, MEETING, FOCUS,
)


def test_classify_meeting_wins_over_gaming_and_work():
    # 同时命中多个关键词时，优先级：会议 > 游戏 > 工作
    assert classify_context({"name": "Zoom Steam DeepSeek"}) == MEETING
    assert classify_context({"name": "Steam", "title": "Visual Studio Code"}) == GAMING
    assert classify_context({"name": "Visual Studio Code"}) == WORK


def test_classify_case_insensitive_and_bundle():
    assert classify_context({"bundle": "AI.DEEPSEEK.DSH.DESKTOP"}) == WORK
    assert classify_context({"name": "腾讯会议"}) == MEETING
    assert classify_context({"title": "League of Legends"}) == GAMING


def test_classify_unknown_is_idle():
    assert classify_context({"name": "访达"}) == IDLE
    assert classify_context({"name": "Finder"}) == IDLE
    assert classify_context({}) == IDLE
    assert classify_context(None) == IDLE


def test_classify_does_not_produce_focus_from_apps():
    # focus 只能由用户手动触发，不来自 App 匹配
    assert classify_context({"name": "随便什么专注App"}) == IDLE
    assert FOCUS not in {classify_context({"name": n}) for n in ("Steam", "Zoom", "Code")}


def test_monitor_commits_only_after_debounce():
    changes = []
    detector = {"app": {"name": "Zoom"}}
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              on_change=lambda ctx, app: changes.append((ctx, app["name"])),
                              debounce_seconds=3.0)
    # t=0 第一次采样：候选开始，但未提交
    snap = mon.sample(now=0.0)
    assert snap["context"] == IDLE and snap["changed"] is False
    # t=2 < debounce：仍未提交
    mon.sample(now=2.0)
    assert mon.context == IDLE
    # t=3 >= debounce：提交
    snap = mon.sample(now=3.0)
    assert snap["context"] == MEETING and snap["changed"] is True
    assert changes == [(MEETING, "Zoom")]


def test_monitor_alt_tab_within_debounce_does_not_flash():
    changes = []
    detector = {"app": {"name": "Zoom"}}
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              on_change=lambda ctx, app: changes.append(ctx),
                              debounce_seconds=2.5)
    mon.sample(now=0.0)               # Zoom 候选开始
    detector["app"] = {"name": "Steam"}  # 2 秒内切到游戏
    mon.sample(now=2.0)               # 候选重置为 gaming
    detector["app"] = {"name": "Zoom"}
    mon.sample(now=2.5)               # 又切回 meeting，重新计时
    assert mon.context == IDLE
    assert changes == []
    mon.sample(now=5.0)               # meeting 坚持够久 → 提交
    assert mon.context == MEETING
    assert changes == [MEETING]


def test_monitor_returns_to_idle_when_app_unknown():
    changes = []
    detector = {"app": {"name": "Steam"}}
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              on_change=lambda ctx, app: changes.append(ctx),
                              debounce_seconds=0.0)
    mon.sample(now=0.0)
    assert mon.context == GAMING
    detector["app"] = {"name": "Finder"}
    mon.sample(now=1.0)
    assert mon.context == IDLE
    assert changes == [GAMING, IDLE]


def test_monitor_detector_exception_falls_back_idle():
    def broken():
        raise RuntimeError("boom")
    changes = []
    mon = ContextAwareMonitor(detector=broken,
                              on_change=lambda ctx, app: changes.append(ctx),
                              debounce_seconds=0.0)
    snap = mon.sample(now=0.0)
    assert snap["context"] == IDLE
    assert changes == []


def test_behavior_for_decision_table():
    # meeting/focus → 躲起来；gaming → 安静；work/idle → 正常
    assert behavior_for(MEETING) == {"hidden": True, "quiet": False}
    assert behavior_for(FOCUS) == {"hidden": True, "quiet": False}
    assert behavior_for(GAMING) == {"hidden": False, "quiet": True}
    assert behavior_for(WORK) == {"hidden": False, "quiet": False}
    assert behavior_for(IDLE) == {"hidden": False, "quiet": False}
    # 非法值按 idle 兜底，不崩溃
    assert behavior_for("") == {"hidden": False, "quiet": False}
    assert behavior_for(None) == {"hidden": False, "quiet": False}


def test_monitor_enabled_callable_gates_to_idle():
    detector = {"app": {"name": "Zoom"}}
    state = {"enabled": False}
    changes = []
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              on_change=lambda ctx, app: changes.append(ctx),
                              enabled=lambda: state["enabled"],
                              debounce_seconds=0.0)
    assert mon.sample(now=0.0)["context"] == IDLE
    # 打开后恢复识别
    state["enabled"] = True
    assert mon.sample(now=1.0)["context"] == MEETING
    assert changes == [MEETING]


def test_monitor_focus_override_forces_focus():
    detector = {"app": {"name": "Steam"}}
    state = {"focus": True}
    changes = []
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              on_change=lambda ctx, app: changes.append(ctx),
                              focus_override=lambda: state["focus"],
                              debounce_seconds=0.0)
    # focus 覆盖一切，包括游戏
    assert mon.sample(now=0.0)["context"] == FOCUS
    state["focus"] = False
    assert mon.sample(now=1.0)["context"] == GAMING
    assert changes == [FOCUS, GAMING]


def test_monitor_callable_rules_take_effect_live():
    detector = {"app": {"name": "MyMysteryApp"}}
    rules = {"value": None}
    mon = ContextAwareMonitor(detector=lambda: detector["app"],
                              rules=lambda: rules["value"],
                              debounce_seconds=0.0)
    # 默认规则不识别这个应用 → idle
    assert mon.sample(now=0.0)["context"] == IDLE
    # 动态加规则后，无需重建 monitor 即生效
    rules["value"] = {"work": ["mystery"]}
    assert mon.sample(now=1.0)["context"] == WORK


def test_context_blocks_interrupt_unified_priority():
    # 会议/专注/游戏 → 禁止一切打扰（压过 office 镜像与信标）
    assert context_blocks_interrupt(MEETING) is True
    assert context_blocks_interrupt(FOCUS) is True
    assert context_blocks_interrupt(GAMING) is True
    # 工作/空闲 → 允许打扰
    assert context_blocks_interrupt(WORK) is False
    assert context_blocks_interrupt(IDLE) is False
    # 非法值兜底不崩
    assert context_blocks_interrupt("") is False
    assert context_blocks_interrupt(None) is False



