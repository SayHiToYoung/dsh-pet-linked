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
import re
import zstandard
from pathlib import Path

from . import token_cost as token_cost_mod

DEFAULT_SESSIONS_ROOT = Path.home() / ".dsh" / "sessions"

# 一个回合的 usage 形状（同 DSH mapUsage）
_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "reasoningTokens")


def sessions_root() -> Path:
    """DSH 会话根目录（可被环境变量覆盖，便于测试）。"""
    env = os.environ.get("DSH_SESSIONS_ROOT")
    return Path(env) if env else DEFAULT_SESSIONS_ROOT


def _encode_workspace(path_str: str) -> str:
    """把工作区绝对路径编码为 DSH 会话目录名：/Users/x/DeepSeek -> --Users-x-DeepSeek--"""
    p = str(path_str).strip("/")
    return "--" + p.replace("/", "-") + "--"


def workspace_dir() -> Path:
    """桌宠所在的 DSH 工作区（= 仓库根目录的上一级，可用 DSH_WORKSPACE 覆盖）。

    情绪响应只扫这个工作区的会话，避免别的项目/工作区的会话互相干扰。
    """
    env = os.environ.get("DSH_WORKSPACE")
    if env:
        base = Path(env)
        if base.is_dir():
            return base
    repo_parent = Path(__file__).resolve().parents[2]  # /Users/yuyangwei/DeepSeek
    return sessions_root() / _encode_workspace(str(repo_parent))


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


def read_session_usage(path: Path) -> tuple[str, dict, str, dict, dict]:
    """解析一个会话日志，返回 (session_id, totals, model, peak_totals, off_totals)。

    totals 为全部 token；peak_totals / off_totals 按每个事件的时间戳
    （事件顶层 `time`，毫秒）划分高峰/低谷时段分别累计，供"真实花费"
    按各时段价估算后求和（token_cost.estimate_cost_cny_mixed）。
    按 (turn, step) 去重求和。
    """
    sid = _session_id_of(path)
    totals = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    peak_totals = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    off_totals = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    model = ""
    try:
        raw = path.read_bytes()
    except OSError:
        return sid, totals, model, peak_totals, off_totals
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
        bucket = peak_totals if token_cost_mod.is_peak_ts(ev.get("time")) else off_totals
        for k, usage_key in (("input", "inputTokens"), ("output", "outputTokens"),
                             ("cacheRead", "cacheReadTokens"), ("reasoning", "reasoningTokens")):
            n = int(usage.get(usage_key) or 0)
            totals[k] += n
            bucket[k] += n
        msg = data.get("message")
        if isinstance(msg, dict) and msg.get("source") and msg["source"].get("model"):
            model = str(msg["source"]["model"])[:120]
    return sid, totals, model, peak_totals, off_totals


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


# 密钥/长串 token 特征（避免把 API key 等敏感内容当情绪文本处理/记录）
_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _looks_like_secret(text: str) -> bool:
    return bool(_SECRET_RE.search(text)) or bool(_LONG_TOKEN_RE.search(text))


def _workspace_dirs(base: Path) -> list[Path]:
    """把 base 归一化为工作区目录列表（兼容 sessions 根 与 单个工作区目录）。"""
    if not base.is_dir():
        return []
    # 直接包含 session-* 子目录 → 就是单个工作区目录
    if any(p.is_dir() and p.name.startswith("session-") for p in base.iterdir()):
        return [base]
    # 否则是 sessions 根 → 下面全是工作区目录
    return [p for p in base.iterdir() if p.is_dir()]


def latest_user_message_global(root: Path | None = None) -> tuple[str, str, str]:
    """扫描所有工作区的所有会话，返回最新一条用户文本消息 (session_id, fingerprint, text)。

    默认扫 sessions_root()（全部工作区）——"一宠跟人走"：用户切到哪个工作区/会话，
    桌宠就跟着那个会话反应；用消息 time 字段比较（跨会话 seq 不可比）；
    过滤系统注入与密钥。找不到返回 ("", "", "")。
    """
    base = root or sessions_root()
    best = None  # (time, session_id, fingerprint, text)
    for ws in _workspace_dirs(base):
        try:
            for sess in ws.iterdir():
                if not sess.is_dir():
                    continue
                f = sess / "session.jsonl.zstd"
                if not f.is_file():
                    continue
                sid = _session_id_of(f)
                try:
                    raw = f.read_bytes()
                except OSError:
                    continue
                for line in _decompress_all(raw).splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") != "user/message":
                        continue
                    data = ev.get("data")
                    if not isinstance(data, dict):
                        continue
                    text = _data_text(data)
                    if not text or _looks_like_secret(text):
                        continue
                    seq = ev.get("seq")
                    if seq is None:
                        seq = ev.get("seq0")
                    ts = ev.get("time", 0)
                    fingerprint = f"{sid}:{seq}"
                    if best is None or ts > best[0]:
                        best = (ts, sid, fingerprint, text)
        except OSError:
            continue
    if best is None:
        return "", "", ""
    return best[1], best[2], best[3]


def aggregate_all_sessions(root: Path | None = None) -> tuple[dict, dict, dict]:
    """聚合所有工作区、所有会话的 token 总账（跨重启幂等，每次现算）。

    返回 (grand, grand_peak, grand_off)：总账 + 高峰分桶 + 低谷分桶。
    """
    base = root or sessions_root()
    grand = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    grand_peak = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    grand_off = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
    if not base.is_dir():
        return grand, grand_peak, grand_off
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
                    tot, peak, off = cached[1]
                else:
                    _, tot, _, peak, off = read_session_usage(f)
                    _AGG_CACHE[key] = (stamp, (tot, peak, off))
                for k in grand:
                    grand[k] += tot[k]
                    grand_peak[k] += peak[k]
                    grand_off[k] += off[k]
    except OSError:
        pass
    return grand, grand_peak, grand_off


def latest_user_message_time(root: Path | None = None) -> float:
    """全局最新一条用户文本消息的时间戳（毫秒）；无则 0。

    用于主动关怀的「欢迎回来」检测：过滤系统注入与密钥，
    只认真实对话的用户消息。
    """
    base = root or sessions_root()
    best = 0.0
    for ws in _workspace_dirs(base):
        try:
            for sess in ws.iterdir():
                if not sess.is_dir():
                    continue
                f = sess / "session.jsonl.zstd"
                if not f.is_file():
                    continue
                try:
                    raw = f.read_bytes()
                except OSError:
                    continue
                for line in _decompress_all(raw).splitlines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") != "user/message":
                        continue
                    data = ev.get("data")
                    if not isinstance(data, dict):
                        continue
                    text = _data_text(data)
                    if not text or _looks_like_secret(text):
                        continue
                    ts = ev.get("time", 0)
                    if ts > best:
                        best = ts
        except OSError:
            continue
    return best


# 模型名收集缓存：path -> ((mtime,size), [(model, first_time), ...])
_MODEL_CACHE: dict = {}


def collect_model_names(root: Path | None = None, limit: int = 50) -> list[str]:
    """收集所有会话日志中出现过的真实模型名（去重，按首次出现时间升序）。

    供设置界面的「添加模型」参考：用户照着真实名的**开头**填前缀，
    计费时按前缀 startswith 匹配才能命中。带 (mtime,size) 缓存，
    只有新写入的会话会被重新扫描。
    """
    base = root or sessions_root()
    first_seen: dict[str, float] = {}
    if base.is_dir():
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
                    cached = _MODEL_CACHE.get(key)
                    if cached is not None and cached[0] == stamp:
                        names = cached[1]
                    else:
                        names = _scan_model_names(f)
                        _MODEL_CACHE[key] = (stamp, names)
                    for name, ts in names:
                        if name not in first_seen or ts < first_seen[name]:
                            first_seen[name] = ts
        except OSError:
            pass
    ordered = sorted(first_seen, key=first_seen.get)
    return ordered[:limit]


def _scan_model_names(f: Path) -> list[tuple[str, float]]:
    """扫描单个会话日志，返回 [(model, first_time_ms), ...]（按出现顺序）。"""
    out: list[tuple[str, float]] = []
    try:
        raw = f.read_bytes()
    except OSError:
        return out
    text = _decompress_all(raw)
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
        msg = data.get("message")
        if isinstance(msg, dict) and msg.get("source") and msg["source"].get("model"):
            name = str(msg["source"]["model"])[:120]
            out.append((name, ev.get("time") or 0))
    return out
