# -*- coding: utf-8 -*-
"""
Token 花费估算（二次开发新增）—— 按 token 数与单价估算 DeepSeek 用量花费。

DeepSeek 官方 API 不返回金额，只返回 token 数，所以这是"估算"而非账单。
定价参考 DeepSeek 官方公开价（USD / 1M tokens），汇率固定按 7.2（参考 dsh-balance）。

默认按 deepseek-chat 定价；reasoner 或自定义模型可在 config 里覆盖。
"""
from __future__ import annotations

import json
from pathlib import Path

# USD / 1M tokens（参考 api-docs.deepseek.com 官方定价）
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
# deepseek-v4-flash 系列（实测价：输入 $0.15 / 输出 $0.29 / 缓存命中 $0.02）
PRICING_V4_FLASH = {
    "input": 0.15,
    "cacheRead": 0.02,
    "output": 0.29,
}
# 固定汇率（人民币/美元）
EXCHANGE_RATE = 7.2

# 可识别的模型前缀 → 定价档位（长前缀优先匹配）
MODEL_PRICING_MAP = {
    "deepseek-reasoner": PRICING_REASONER,
    "deepseek-v4-flash": PRICING_V4_FLASH,
    "deepseek-v4": PRICING_V4_FLASH,
    "deepseek-chat": PRICING_DEFAULT,
    "deepseek-v3": PRICING_DEFAULT,
    "deepseek-r1": PRICING_REASONER,
}
# 按名称长度降序匹配，避免 "deepseek-v4" 抢先吞掉 "deepseek-v4-flash"
_MODEL_PRICING_ORDER = sorted(MODEL_PRICING_MAP, key=len, reverse=True)


def pricing_for_model(model: str | None) -> dict:
    """按模型名选定价；未知模型回落默认。"""
    if not model:
        return dict(PRICING_DEFAULT)
    low = str(model).lower()
    for prefix in _MODEL_PRICING_ORDER:
        if low.startswith(prefix):
            return dict(MODEL_PRICING_MAP[prefix])
    return dict(PRICING_DEFAULT)


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
