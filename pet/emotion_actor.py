# -*- coding: utf-8 -*-
"""
情绪 → 动作导演（二次开发新增）—— 混合引擎。

思路：桌宠读完 DSH 会话日志拿到最新一条对话文本后，判断其中情绪，
再选一个贴合场景的动作播放（映射到现有动画）。

混合策略（本地为主 + LLM 升级）：
  1. 本地关键词情感打分 → 情绪 + 置信度；
  2. 置信度够且情绪平稳 → 直接用本地动作（零成本）；
  3. 置信度低 或 情绪强烈（生气/难过/惊讶/喜欢/激动）→ 升级调 LLM，
     让 LLM 从动作候选里挑最贴合的一个（花少量 token）。
"""
from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 情绪 → 可播放动作
# 值都必须是 catalog.ANIM_FILES 中真实存在的动画名（shenshen）。
# 曾有一批"幽灵名"（傲娇生气/开心跃动/害羞惊讶/放烟花/凭空生花/三球抛接 等）
# 在 catalog 里不存在，播放时静默失败 → 表情不变。已全部替换为真实动画名。
EMOTION_ACTIONS: dict[str, list[str]] = {
    "joy":      ["点击回应 - 开心跃动", "可爱宅舞", "轻快摇摆舞", "原地跳跃抓碎头顶物品"],
    "love":     ["点击回应 - 害羞惊讶", "轻快摇摆舞", "吹气球", "鲸鱼吐泡泡特效", "女仆屈膝礼仪"],
    "shy":      ["点击回应 - 害羞惊讶", "待机呼吸休闲", "轻快摇摆舞"],
    "angry":    ["点击回应 - 傲娇生气（侧身展示）", "用鲸鱼尾巴拍打地面", "原地重力下蹲压缩"],
    "sad":      ["被落叶淹没", "原地小憩沉眠", "偷吃零食被抓住"],
    "surprised": ["被吓一跳（炸毛）", "点击回应 - 害羞惊讶", "原地跳跃抓碎头顶物品"],
    "laugh":    ["轻快摇摆舞", "可爱宅舞", "点击回应 - 开心跃动", "原地跳跃抓碎头顶物品"],
    "tired":    ["哈欠连天", "原地小憩沉眠", "超大伸懒腰"],
    "hungry":   ["吃白饭", "大口吃零食", "吃Token", "偷吃零食被抓住"],
    "thinking": ["深度思考碎碎念", "原地专心玩魔方"],
    "celebrate": ["吹气球", "动物环绕", "原地跳跃抓碎头顶物品", "放风筝", "轻快摇摆舞"],
    "playful":  ["偷吃零食被抓住", "原地敲击桌面互动", "原地蹲下玩玩具汽车"],
    "excited":  ["原地跳跃抓碎头顶物品", "可爱宅舞", "小幅度原地 360 度旋转展示"],
    "gaming":   ["玩游戏气急败坏", "玩水枪"],
    "tease":    ["点击回应 - 傲娇生气（侧身展示）", "用鲸鱼尾巴拍打地面", "原地重力下蹲压缩", "偷吃零食被抓住"],
    "neutral":  ["待机呼吸休闲", "悠闲哼歌"],
}

# LLM 可选的"动作标签" → 实际动画（给 LLM 的词汇表，越短越省 token）
ACTION_LABELS: dict[str, list[str]] = {
    "开心跳舞": EMOTION_ACTIONS["joy"],
    "害羞脸红": ["点击回应 - 害羞惊讶"],
    "生气傲娇": ["点击回应 - 傲娇生气（侧身展示）"],
    "惊吓炸毛": ["被吓一跳（炸毛）"],
    "撒娇卖萌": ["女仆屈膝礼仪", "轻快摇摆舞"],
    "困倦打哈欠": ["哈欠连天", "超大伸懒腰"],
    "想睡觉": ["原地小憩沉眠"],
    "吃东西": ["吃白饭", "大口吃零食"],
    "思考发呆": ["深度思考碎碎念", "原地专心玩魔方"],
    "庆祝欢呼": ["吹气球", "动物环绕", "放风筝"],
    "偷懒摸鱼": ["偷吃零食被抓住"],
    "写代码工作": ["写代码"],
    "玩玩具": ["原地蹲下玩玩具汽车", "玩水枪"],
    "哼歌自得": ["悠闲哼歌"],
    "被冷落": ["被落叶淹没"],
    "玩得气急败坏": ["玩游戏气急败坏"],
    "发呆待机": ["待机呼吸休闲"],
}
LABEL_NAMES = list(ACTION_LABELS.keys())

# ---------------------------------------------------------------- 本地关键词情绪
# (关键词组, 情绪, 权重)
KEYWORD_RULES: list[tuple[tuple[str, ...], str, float]] = [
    (("哈哈", "笑死", "好笑", "咯咯", "haha", "lol", "太逗", "笑翻"), "laugh", 1.0),
    (("真棒", "太棒", "很棒", "好棒", "成功", "厉害", "牛", "完美", "搞定", "完成", "🎉", "恭喜", "👍", "太好了"), "celebrate", 1.0),
    (("开心", "高兴", "喜欢", "好耶", "耶", "快乐", "兴奋", "太爽", "🥳", "激动"), "joy", 1.0),
    (("爱你", "想你", "么么", "喜欢", "心动", "害羞", "脸红", "🥰", "😳", "亲亲", "抱抱"), "love", 1.0),
    (("生气", "气死", "无语", "烦", "讨厌", "🔥", "怒", "气炸", "受不了", "😤", "哼"), "angry", 1.0),
    (("难过", "伤心", "哭了", "想哭", "失败", "可惜", "遗憾", "😢", "😭", "完蛋", "糟糕"), "sad", 1.0),
    (("震惊", "惊讶", "真的吗", "哇", "不可能", "卧槽", "天哪", "😲", "😱", "吓人", "惊了"), "surprised", 1.0),
    (("困", "累", "晚安", "睡觉", "熬夜", "😴", "打哈欠", "疲惫", "好累"), "tired", 1.0),
    (("饿", "吃饭", "吃", "零食", "奶茶", "火锅", "🍚", "🍜", "🥘", "好吃"), "hungry", 1.0),
    (("思考", "想想", "考虑", "大概", "也许", "应该", "可能", "琢磨", "纠结"), "thinking", 0.6),
    (("玩", "游戏", "摸鱼", "摆烂", "划水", "发呆", "无聊", "偷懒"), "playful", 0.8),
    (("写代码", "代码", "bug", "重构", "提交", "写代码", "开发"), "writing", 0.7),
    (("肥", "胖", "笨蛋", "傻瓜", "蠢", "呆子", "憨", "蠢货", "大肥鱼"), "tease", 1.0),
]

# 高情绪：置信度足够也会升级 LLM 精调
HIGH_EMOTION = {"angry", "sad", "surprised", "love", "excited"}
LOW_CONF_THRESHOLD = 0.5


def _normalize(text: str) -> str:
    return (text or "").lower()


def detect_emotion_local(text: str) -> tuple[str, float]:
    """返回 (emotion, confidence 0~1)。"""
    low = _normalize(text)
    scores: dict[str, float] = {}
    for keywords, emotion, weight in KEYWORD_RULES:
        hits = sum(1 for kw in keywords if kw.lower() in low)
        if hits:
            scores[emotion] = scores.get(emotion, 0.0) + weight * hits
    if not scores:
        return "neutral", 0.0
    top = max(scores, key=scores.get)
    total = sum(scores.values())
    return top, scores[top] / total


def needs_llm(emotion: str, confidence: float) -> bool:
    """置信度低 或 情绪强烈 → 升级 LLM。"""
    if emotion in HIGH_EMOTION:
        return True
    return confidence < LOW_CONF_THRESHOLD


def pick_action_local(emotion: str) -> str | None:
    acts = EMOTION_ACTIONS.get(emotion)
    return random.choice(acts) if acts else None


def resolve_label(label: str) -> str | None:
    """LLM 输出的动作标签 → 实际动画名（取该标签下随机一个）。"""
    if not label:
        return None
    label = str(label).strip()
    # 直接命中动作名
    for acts in EMOTION_ACTIONS.values():
        if label in acts:
            return label
    # 命中标签
    acts = ACTION_LABELS.get(label)
    if acts:
        return random.choice(acts)
    # 模糊：包含匹配
    for name, acts in ACTION_LABELS.items():
        if name in label or label in name:
            return random.choice(acts)
    return None


def _call_llm(messages: list[dict], config, api_key: str, timeout: float = 20.0) -> str:
    """非流式调 OpenAI 兼容接口，返回正文文本。"""
    from .chat.providers import normalize_chat_endpoint, _make_ssl_context  # 复用
    endpoint = normalize_chat_endpoint(config.base_url, config.chat_path)
    payload = {
        "model": getattr(config, "model", None) or "deepseek-chat",
        "messages": messages,
        "stream": False,
        "temperature": 0.4,
        "max_tokens": 120,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_make_ssl_context(getattr(config, "verify_ssl", True))) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return str(body["choices"][0]["message"]["content"] or "")


def escalate_with_llm(text: str, config, api_key: str) -> str | None:
    """让 LLM 从动作标签里挑最贴合的一个，返回实际动画名（失败回 None）。"""
    system = (
        "你是桌面宠物「鲸鱼娘」的行为导演。根据用户最近一条消息里的情绪，"
        f"从下面动作里选一个最贴合的。只输出 JSON，格式："
        f'{{"emotion":"情绪","action":"动作标签","reason":"一句话理由"}}\n'
        f"可用动作标签：{', '.join(LABEL_NAMES)}"
    )
    user = f"用户消息：{text[:500]}\n请只输出 JSON。"
    try:
        raw = _call_llm([{"role": "system", "content": system}, {"role": "user", "content": user}], config, api_key)
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return None
        data = json.loads(match.group(0))
        return resolve_label(str(data.get("action") or ""))
    except Exception:
        return None


def decide_action(text: str, config=None, api_key: str = "") -> tuple[str, str]:
    """混合决策：返回 (实际动画名 或 None, 来源 "local"/"llm"/"none")。

    中性（无情绪信号）→ 不反应；有情绪才本地或 LLM 出动作。
    """
    emotion, confidence = detect_emotion_local(text)
    if emotion == "neutral":
        return None, "none"
    if not needs_llm(emotion, confidence):
        action = pick_action_local(emotion)
        return action, "local"
    if config is not None:
        action = escalate_with_llm(text, config, api_key)
        if action:
            return action, "llm"
    # LLM 不可用/失败 → 本地兜底
    return pick_action_local(emotion), "local"
