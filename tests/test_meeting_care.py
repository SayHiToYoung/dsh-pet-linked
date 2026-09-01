# -*- coding: utf-8 -*-
"""会议关怀单测：开会计时、散会分档结算、防重复、冷却、开关。"""
import pytest

from pet.meeting_care import MeetingCare, DEFAULT_MEETING_THRESHOLDS_MIN


def test_no_meeting_no_feedback():
    care = MeetingCare()
    assert care.tick("idle", 0.0) is None
    assert care.tick("work", 10.0) is None
    assert care.tick("gaming", 20.0) is None


def test_short_meeting_gives_no_feedback():
    care = MeetingCare()
    care.tick("meeting", 0.0)
    # 只开了 5 分钟就散会：不到最低档，不打扰
    assert care.tick("idle", 5 * 60) is None


def test_thirty_minute_meeting_fires_first_level():
    care = MeetingCare()
    care.tick("meeting", 0.0)
    result = care.tick("idle", 30 * 60 + 1)
    assert result is not None
    kind, line = result
    assert kind == "meeting-care"
    assert line
    assert care.last_level == 30


def test_long_meeting_picks_highest_level():
    care = MeetingCare()
    care.tick("meeting", 0.0)
    result = care.tick("idle", 130 * 60)
    assert result is not None
    assert care.last_level == 120


def test_custom_thresholds():
    care = MeetingCare(thresholds_min=[45, 90])
    care.tick("meeting", 0.0)
    # 50 分钟 ≥ 45 → 触发第一档（自定义）
    result = care.tick("idle", 50 * 60)
    assert result is not None
    assert care.last_level == 45
    care.tick("meeting", 100 * 60)
    result = care.tick("idle", 200 * 60)  # 又开 100 分钟
    assert result is not None
    assert care.last_level == 90


def test_disabled_never_fires():
    care = MeetingCare(enabled=False)
    care.tick("meeting", 0.0)
    assert care.tick("idle", 300 * 60) is None
    assert care.last_level == 0


def test_min_gap_suppresses_second_meeting():
    care = MeetingCare(thresholds_min=[1], min_gap_sec=600)
    # 第一场会（0→700 秒，够档且已过初始冷却）→ 触发
    care.tick("meeting", 0.0)
    assert care.tick("idle", 700) is not None
    # 第二场会（800→1200 秒，也够档）但距上次触发 < 600s 冷却 → 压制
    care.tick("meeting", 800)
    assert care.tick("idle", 1200) is None
    # 第三场会（1400→2200 秒）：已过冷却期 → 恢复触发
    care.tick("meeting", 1400)
    assert care.tick("idle", 2200) is not None


def test_reset_clears_state():
    care = MeetingCare()
    care.tick("meeting", 0.0)
    care.reset()
    assert care.tick("idle", 300 * 60) is None


def test_reentering_meeting_restarts_timer():
    care = MeetingCare()
    care.tick("meeting", 0.0)
    care.tick("idle", 20 * 60)          # 散会，未达档
    care.tick("meeting", 25 * 60)       # 重新开会
    care.tick("idle", 25 * 60 + 40 * 60)  # 又开 40 分钟
    assert care.last_level == 30
