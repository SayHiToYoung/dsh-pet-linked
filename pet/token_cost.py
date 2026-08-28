# -*- coding: utf-8 -*-
"""
Token 花费估算（二次开发新增）—— 按 token 数与单价估算 DeepSeek 用量花费。

DeepSeek 官方 API 不返回金额，只返回 token 数，所以这是"估算"而非账单。
定价参考 DeepSeek 官方公开价（USD / 1M tokens），汇率固定按 7.2（参考 dsh-balance）。

默认按 deepseek-chat 定价；reasoner 或自定义模型可在 config 里覆盖。
v4-flash / v4-pro / v4-flash-vision-exp 按官方两档价（高峰/低谷）依当前时间自动切换。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# USD / 1M tokens（官方价，2026-08-16 调价后；来源 api-docs.deepseek.com/quick_start/pricing）
# 官方两档价：高峰（peak）为低谷（off-peak）的两倍。高峰 = UTC 周一~周五 01:00-04:00、06:00-10:00。
#   v4-flash：输入(未命中) 0.44/0.22 · 缓存命中 0.014/0.007 · 输出 1.32/0.66
#   v4-pro：  输入(未命中) 1.32/0.66 · 缓存命中 0.044/0.022 · 输出 3.96/1.98
#   v4-flash-vision-exp：同 v4-flash
PRICING_FLASH_PEAK = {"input": 0.44, "cacheRead": 0.014, "output": 1.32}
PRICING_FLASH_OFF_PEAK = {"input": 0.22, "cacheRead": 0.007, "output": 0.66}
PRICING_PRO_PEAK = {"input": 1.32, "cacheRead": 0.044, "output": 3.96}
PRICING_PRO_OFF_PEAK = {"input": 0.66, "cacheRead": 0.022, "output": 1.98}
# 已无官方两档价的历史模型（deepseek-chat/v3/r1 等）回落单档，仅作估算参考
PRICING_DEFAULT = {
    "input": 0.27,        # 缓存未命中输入
    "cacheRead": 0.07,    # 缓存命中输入
    "output": 1.10,       # 输出
}
PRICING_REASONER = {
    "input": 0.55,
    "cacheRead": 0.14,
    "output": 2.19,
}
# 固定汇率（人民币/美元）
EXCHANGE_RATE = 7.2

# 模型前缀 → 定价档位键（长前缀优先匹配，避免 "deepseek-v4" 抢先吞掉 "deepseek-v4-flash"）
_MODEL_PRICING_MAP = {
    "deepseek-reasoner": "reasoner",
    "deepseek-v4-flash-vision-exp": "flash",
    "deepseek-v4-flash": "flash",
    "deepseek-v4-pro": "pro",
    "deepseek-v4": "flash",
    "deepseek-chat": "default",
    "deepseek-v3": "default",
    "deepseek-r1": "reasoner",
}
_MODEL_PRICING_ORDER = sorted(_MODEL_PRICING_MAP, key=len, reverse=True)


def _is_peak_now(when=None) -> bool:
    """官方高峰时段判断（UTC）：周一~周五 01:00-04:00、06:00-10:00；周末/其余为低谷。

    `when` 可注入 datetime 用于测试；默认取当前 UTC 时间。
    """
    when = when or datetime.now(timezone.utc)
    if when.weekday() >= 5:  # 周六/周日 → 低谷
        return False
    minutes = when.hour * 60 + when.minute
    return (60 <= minutes < 240) or (360 <= minutes < 600)


def is_peak_ts(ms) -> bool:
    """毫秒时间戳（DSH 会话事件顶层 `time`）→ 是否高峰时段。"""
    try:
        when = datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return _is_peak_now(when)


def pricing_for_model(model: str | None, when=None) -> dict:
    """按模型名选定价；官方两档价模型按当前时间自动切高峰/低谷，其余回落默认档。

    `when` 可注入 datetime 用于测试；默认取当前 UTC 时间判断高峰。
    """
    if not model:
        return dict(PRICING_DEFAULT)
    low = str(model).lower()
    key = "default"
    for prefix in _MODEL_PRICING_ORDER:
        if low.startswith(prefix):
            key = _MODEL_PRICING_MAP[prefix]
            break
    if key == "flash":
        return dict(PRICING_FLASH_PEAK if _is_peak_now(when) else PRICING_FLASH_OFF_PEAK)
    if key == "pro":
        return dict(PRICING_PRO_PEAK if _is_peak_now(when) else PRICING_PRO_OFF_PEAK)
    if key == "reasoner":
        return dict(PRICING_REASONER)
    return dict(PRICING_DEFAULT)


def pricing_both_for_model(model: str | None) -> tuple[dict, dict]:
    """返回该模型 (高峰价, 低谷价) 两套定价，供"双账本"同时估算。

    官方有两档价的模型（v4-flash / v4-pro / vision-exp）返回各自峰/谷价；
    无两档价的模型（reasoner/chat/未知）两套相同，显示时归并为单值。
    """
    if not model:
        return dict(PRICING_DEFAULT), dict(PRICING_DEFAULT)
    low = str(model).lower()
    key = "default"
    for prefix in _MODEL_PRICING_ORDER:
        if low.startswith(prefix):
            key = _MODEL_PRICING_MAP[prefix]
            break
    if key == "flash":
        return dict(PRICING_FLASH_PEAK), dict(PRICING_FLASH_OFF_PEAK)
    if key == "pro":
        return dict(PRICING_PRO_PEAK), dict(PRICING_PRO_OFF_PEAK)
    if key == "reasoner":
        return dict(PRICING_REASONER), dict(PRICING_REASONER)
    return dict(PRICING_DEFAULT), dict(PRICING_DEFAULT)


def estimate_cost_cny(input_t: int, output_t: int, cache_read: int = 0,
                      reasoning: int = 0, pricing: dict | None = None) -> float:
    """估算花费（人民币）。input 已含缓存未命中部分，缓存命中单独计便宜档。"""
    p = pricing or PRICING_DEFAULT
    usd = (
        max(0, int(input_t or 0)) * float(p.get("input", 0))
        + max(0, int(cache_read or 0)) * float(p.get("cacheRead", 0))
        + max(0, int(output_t or 0)) * float(p.get("output", 0))
    ) / 1_000_000.0
    return round(usd * EXCHANGE_RATE, 4)


def estimate_cost_cny_mixed(peak_totals: dict, off_totals: dict,
                            peak_pricing: dict | None = None,
                            off_pricing: dict | None = None) -> float:
    """峰谷分桶混合估算：高峰用量×高峰价 + 低谷用量×低谷价 = 真实花费（人民币）。

    peak_totals / off_totals 为 {input, output, cacheRead, reasoning} 分桶；
    无两档价的模型传同一套 pricing 即可，结果退化为单档估算。
    """
    pp = peak_pricing or PRICING_DEFAULT
    op = off_pricing or PRICING_DEFAULT
    return (estimate_cost_cny(
                peak_totals.get("input", 0), peak_totals.get("output", 0),
                peak_totals.get("cacheRead", 0), peak_totals.get("reasoning", 0), pp)
            + estimate_cost_cny(
                off_totals.get("input", 0), off_totals.get("output", 0),
                off_totals.get("cacheRead", 0), off_totals.get("reasoning", 0), op))


def format_number(n, style: str = "auto") -> str:
    """把大数字压成适合气泡显示的短格式。

    style:
      "auto" -> 中文习惯 万/亿（默认，避免撑爆文本框）
      "km"   -> 1.2K / 3.4M
      "full" -> 完整千分位 1,816,883
    """
    try:
        value = int(n or 0)
    except (TypeError, ValueError):
        value = 0
    if style == "full":
        return f"{value:,}"
    if style == "km":
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)
    # auto: 粒度调细 —— 10 万以下完整千分位，万/亿档保留更多小数位，
    # 让增量变化肉眼可见（原版 1 万/100 万才动一位，小增量被格式抹平）。
    if value >= 100_000_000:
        return f"{value / 100_000_000:.3f}亿"
    if value >= 100_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:,}"


def load_lifetime(path: Path) -> dict:
    """读取持久化的累计用量（不存在返回空）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "input": int(data.get("input", 0) or 0),
                "output": int(data.get("output", 0) or 0),
                "cacheRead": int(data.get("cacheRead", 0) or 0),
                "reasoning": int(data.get("reasoning", 0) or 0),
            }
    except Exception:
        pass
    return {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}


def save_lifetime(path: Path, totals: dict) -> None:
    """持久化累计用量。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(totals, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
