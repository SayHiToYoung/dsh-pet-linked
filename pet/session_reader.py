# -*- coding: utf-8 -*-
"""
DSH 会话日志读取（二次开发新增）—— 直接读 DSH 落盘的会话日志算 token 用量。

原理：DSH 把每次会话持久化到 ~/.dsh/sessions/<workspace>/session-<id>/session.jsonl.zstd，
追加写入、每个 flush 一个 zstd 帧。日志里每个 assistant/message 事件都带 usage
（inputTokens/outputTokens/cacheReadTokens/reasoningTokens）与模型名。

桌宠直接解析这份"权威账本"，不依赖页面信标、不依赖刷新、重启可补账。
"""
from __future__ import annotations

import json
import os
import zstandard
from pathlib import Path

DEFAULT_SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"

# 一个回合的 usage 形状（同 DSH mapUsage）
_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens")


def sessions_root() -> Path:
    """DSH 会话根目录（可被环境变量覆盖，便于测试）。"""
    env = os.environ.get("DSH_SESSIONS_ROOT")
    return Path(env) if env else DEFAULT_SESSIONS_ROOT


def _session_id_of(path: Path) -> str:
    """从 session-<uuid>/session.jsonl.zstd 提取 session id。"""
    return path.parent.name


def find_current_session_file(root: Path | None = None) -> Path | None:
    """在所有 workspace 的 session 里找最新（mtime 最大）的 session.jsonl.zstd。

    返回 None 表示还没找到任何会话（DSH 未启动/无会话）。
    """
    base = root or sessions_root()
    best: Path | None = None
    best_mtime = -1.0
    if not base.is_dir():
        return None
    try:
        for ws in base.iterdir():
            if not ws.is_dir():
                continue
            for sess in ws.iterdir():
                f = sess / "session.jsonl.zstd"
                try:
                    if f.is_file() and f.stat().st_mtime > best_mtime:
                        best, best_mtime = f, f.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        return None
    return best


def _decompress_all(data: bytes) -> str:
    """解压可能含多个拼接 zstd 帧的数据，返回文本。

    用 read_across_frames 一次扫过所有完整帧（约 30ms/10MB）；末尾半帧
    （仍在上写的部分）会自动丢弃，后续轮询读到完整时自然补上。
    """
    try:
        dctx = zstandard.ZstdDecompressor()
        dobj = dctx.decompressobj(read_across_frames=True)
        out = dobj.decompress(data)
        return out.decode("utf-8", "replace")
    except Exception:
        # 回退：逐帧解压
        return _decompress_all_fallback(data)


def _decompress_all_fallback(data: bytes) -> str:
    dctx = zstandard.ZstdDecompressor()
    out_chunks: list[bytes] = []
    offset = 0
    n = len(data)
    while offset < n:
        try:
            dobj = dctx.decompressobj()
            out = dobj.decompress(data[offset:])
            out_chunks.append(out)
            if dobj.eof:
                consumed = n - offset - len(dobj.unused_data)
                offset += consumed
                if consumed <= 0:
                    break
            else:
                break
        except Exception:
            break
    return b"".join(out_chunks).decode("utf-8", "replace")


def read_session_usage(path: Path) -> tuple[str, dict, str]:
    """解析一个会话日志，返回 (session_id, totals, model)。

    totals = {"input","output","cacheRead","reasoning"}（按 (turn,step) 去重求和）。
    model 取日志里最后出现的模型名。
    """
    sid = _session_id_of(path)
    totals = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    model = ""
    try:
        raw = path.read_bytes()
    except OSError:
        return sid, totals, model
    text = _decompress_all(raw)
    seen: set[tuple] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        data = ev.get("data") if isinstance(ev, dict) else None
        if not isinstance(data, dict):
            continue
        usage = data.get("usage")
        if not (isinstance(usage, dict) and isinstance(usage.get("inputTokens"), (int, float))):
            continue
        turn = data.get("turn")
        step = data.get("step")
        key = (turn, step)
        if key in seen:
            continue
        seen.add(key)
        totals["input"] += int(usage.get("inputTokens") or 0)
        totals["output"] += int(usage.get("outputTokens") or 0)
        totals["cacheRead"] += int(usage.get("cacheReadTokens") or 0)
        totals["reasoning"] += int(usage.get("reasoningTokens") or 0)
        msg = data.get("message")
        if isinstance(msg, dict) and msg.get("source") and msg["source"].get("model"):
            model = str(msg["source"]["model"])[:120]
    return sid, totals, model


# 全量聚合缓存：path -> ((mtime,size), totals)；文件没变就不重解析
_AGG_CACHE: dict = {}


def latest_assistant_message(path: Path) -> tuple[str, str]:
    """返回最新一条 assistant 文本消息的 (指纹, 文本)。

    指纹用 (turn, step, seq)；用于检测"出现了新回合"，触发情绪动作。
    找不到返回 ("", "")。
    """
    return _latest_message(path, "assistant/message")


def latest_user_message(path: Path) -> tuple[str, str]:
    """返回最新一条 user 文本消息（排除纯工具结果）的 (指纹, 文本)。"""
    return _latest_message(path, "user/message")


def _extract_blocks_text(content) -> str:
    """从 content（str 或 block 列表）提取纯文本（只取 text 块）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                pieces.append(str(block.get("text") or ""))
        return "".join(pieces).strip()
    return ""


# 系统注入内容特征（开头的 PERSONA_LOAD / 运行时上下文 / 技能提醒等，不是真实对话）
_SYSTEM_HINTS = ("[PERSONA_LOAD]", "Current runtime context", "<system-reminder>",
                 "Approval policy:", "Current DSH file policy")


def _data_text(data) -> str:
    """从事件 data 里提取文本：助手在 data.message.content，用户在 data.content。"""
    if not isinstance(data, dict):
        return ""
    # 助手消息：data.message.content
    msg = data.get("message")
    if isinstance(msg, dict):
        t = _extract_blocks_text(msg.get("content"))
        if t:
            return t
    # 用户消息：data.content
    t = _extract_blocks_text(data.get("content"))
    if t and not any(hint in t for hint in _SYSTEM_HINTS):
        return t
    return ""


def _latest_message(path: Path, event_type: str) -> tuple[str, str]:
    """扫描会话日志，返回指定事件类型的最后一条含文本消息 (指纹, 文本)。"""
    fingerprint = ""
    text = ""
    try:
        raw = path.read_bytes()
    except OSError:
        return fingerprint, text
    for line in _decompress_all(raw).splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != event_type:
            continue
        data = ev.get("data")
        if not isinstance(data, dict):
            continue
        turn, step = data.get("turn"), data.get("step")
        seq = ev.get("seq")
        if seq is None:
            seq = ev.get("seq0")
        joined = _data_text(data)
        if joined:
            fingerprint = f"{turn}:{step}:{seq}"
            text = joined
    return fingerprint, text


def aggregate_all_sessions(root: Path | None = None) -> dict:
    """聚合所有工作区、所有会话的 token 总账（跨重启幂等，每次现算）。"""
    base = root or sessions_root()
    grand = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    if not base.is_dir():
        return grand
    try:
        for ws in base.iterdir():
            if not ws.is_dir():
                continue
            for sess in ws.iterdir():
                f = sess / "session.jsonl.zstd"
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                    stamp = (st.st_mtime, st.st_size)
                except OSError:
                    continue
                key = str(f)
                cached = _AGG_CACHE.get(key)
                if cached is not None and cached[0] == stamp:
                    tot = cached[1]
                else:
                    _, tot, _ = read_session_usage(f)
                    _AGG_CACHE[key] = (stamp, tot)
                for k in grand:
                    grand[k] += tot[k]
    except OSError:
        pass
    return grand
