# -*- coding: utf-8 -*-
"""小鲸与共享记忆服务之间的稳定协议、客户端与事实型开场翻译。"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


PROTOCOL_VERSION = 1
MAX_BATCH_ITEMS = 20
MAX_MEMORY_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ProtocolError(ValueError):
    pass


class SyncTransportError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identifier(value: Any, field: str, limit: int = 128) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit or any(ord(char) < 32 for char in text):
        raise ProtocolError(f"invalid {field}")
    return text


def validate_memory(memory: dict) -> dict:
    if not isinstance(memory, dict):
        raise ProtocolError("memory must be an object")
    clean = json.loads(canonical_json(memory))
    clean["id"] = _identifier(clean.get("id"), "memory.id")
    layer = str(clean.get("layer") or "")
    source = str(clean.get("sourceType") or "")
    expected = {"L1": "observed", "L2": "derived", "L3": "user_stated"}
    if layer not in expected or source != expected[layer]:
        raise ProtocolError("memory layer/sourceType mismatch")
    try:
        revision = int(clean.get("revision"))
    except (TypeError, ValueError):
        raise ProtocolError("invalid memory.revision") from None
    if revision < 1:
        raise ProtocolError("invalid memory.revision")
    clean["revision"] = revision
    if len(canonical_json(clean).encode("utf-8")) > MAX_MEMORY_BYTES:
        raise ProtocolError("memory item too large")
    return clean


def validate_batch(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ProtocolError("batch must be an object")
    try:
        version = int(payload.get("protocolVersion"))
    except (TypeError, ValueError):
        raise ProtocolError("invalid protocolVersion") from None
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocolVersion")
    memories = payload.get("memories")
    if not isinstance(memories, list) or not 1 <= len(memories) <= MAX_BATCH_ITEMS:
        raise ProtocolError("memories must contain 1..20 items")
    clean_memories = [validate_memory(item) for item in memories]
    ids = [item["id"] for item in clean_memories]
    if len(ids) != len(set(ids)):
        raise ProtocolError("duplicate memory id inside batch")
    latest_revision = max(item["revision"] for item in clean_memories)
    declared_revision = int(payload.get("latestRevision") or 0)
    if declared_revision != latest_revision:
        raise ProtocolError("latestRevision mismatch")
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "userId": _identifier(payload.get("userId"), "userId"),
        "deviceId": _identifier(payload.get("deviceId"), "deviceId"),
        "batchId": _identifier(payload.get("batchId"), "batchId"),
        "latestRevision": latest_revision,
        "memories": clean_memories,
    }


def make_batch(*, user_id: str, device_id: str, report: dict, memories: list[dict]) -> dict:
    return validate_batch({
        "protocolVersion": PROTOCOL_VERSION,
        "userId": user_id,
        "deviceId": device_id,
        "batchId": report.get("batchId"),
        "latestRevision": report.get("latestRevision"),
        "memories": memories,
    })


def _duration_text(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours} 小时 {rest} 分钟" if rest else f"{hours} 小时"


def _stable_choice(seed: str, options: tuple[str, ...]) -> str:
    digest = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
    return options[int(digest[:8], 16) % len(options)]


_EMOTION_TEXT = {
    "frustrated": "有点烦",
    "angry": "很生气",
    "sad": "有些难过",
    "anxious": "有些焦虑",
    "tired": "很累",
    "happy": "挺开心",
    "excited": "很期待",
    "wronged": "有些委屈",
}


def memory_time(memory: dict) -> str:
    for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
        value = str(memory.get(key) or "")
        if value:
            return value
    return ""


def build_big_whale_opening(memories: list[dict]) -> tuple[str, str]:
    """只用已存事实生成开场；返回 (文本, 聚焦记忆 ID)。"""
    rows = [row for row in memories if isinstance(row, dict)]
    if not rows:
        return "", ""
    emotions = [row for row in rows if row.get("layer") == "L3" and row.get("label")]
    if emotions:
        latest = max(emotions, key=lambda row: (memory_time(row), int(row.get("revision") or 0)))
        feeling = _EMOTION_TEXT.get(str(latest.get("label")), "有些不舒服")
        return f"我还记得你之前说自己{feeling}。今天不用从头讲，那件事好一点了吗？", str(latest.get("id") or "")

    facts = [row for row in rows if row.get("layer") == "L1"]
    if not facts:
        clue = max(rows, key=lambda row: int(row.get("revision") or 0))
        statement = str(clue.get("statement") or "").strip()
        return (f"小鲸把这件事记下来了：{statement}" if statement else "", str(clue.get("id") or ""))

    groups: dict[tuple, dict] = {}
    for fact in facts:
        project = fact.get("project") if isinstance(fact.get("project"), dict) else {}
        key = (
            str(fact.get("context") or "idle"),
            str(fact.get("app") or "未知应用"),
            str(project.get("name") or ""),
            str(fact.get("title") or ""),
        )
        bucket = groups.setdefault(key, {"seconds": 0.0, "latest": fact, "ids": []})
        bucket["seconds"] += max(0.0, float(fact.get("durationSeconds") or 0.0))
        bucket["ids"].append(str(fact.get("id") or ""))
        if (memory_time(fact), int(fact.get("revision") or 0)) > (
            memory_time(bucket["latest"]), int(bucket["latest"].get("revision") or 0)
        ):
            bucket["latest"] = fact
    focus = max(
        groups.values(),
        key=lambda item: (item["seconds"], memory_time(item["latest"]), int(item["latest"].get("revision") or 0)),
    )
    fact = focus["latest"]
    seconds = focus["seconds"]
    duration = _duration_text(seconds)
    context = str(fact.get("context") or "")
    app = str(fact.get("app") or "这个应用").strip()
    title = str(fact.get("title") or "").strip()
    project = fact.get("project") if isinstance(fact.get("project"), dict) else {}
    project_name = str(project.get("name") or "").strip()
    if context == "meeting" or fact.get("kind") == "meeting":
        text = f"小鲸来报信了：你今天开会累计 {duration}。这会挺有存在感的，你想吐槽两句，还是今晚先不聊它？"
    elif context == "gaming":
        subject = title or app
        text = f"你今天玩了 {subject} 大约 {duration}，小鲸记下了。至于战况，我可不乱猜。今天有哪一段值得讲？"
    elif context in {"media", "video", "entertainment"}:
        subject = title or app
        text = f"你今天看了 {subject} 大约 {duration}。小鲸只记到这里，剩下的我想听你说。哪一段最有意思？"
    elif project_name:
        text = f"你今天在 {project_name} 上忙了大约 {duration}，小鲸都记下了。我还不知道进展顺不顺。想说说，还是今晚先不聊工作？"
    else:
        text = f"小鲸说，你今天在 {app} 上花了大约 {duration}。忙完了吗，还是脑子还挂在那里？"
    return text, str(fact.get("id") or "")


def build_grounded_companion_reply(
    user_text: str,
    memories: list[dict],
    *,
    emotion_label: str = "",
) -> str:
    """生成不越过已知事实的最小陪伴回复。

    这不是通用聊天模型。它只复述用户明确表达的情绪，或引用共享记忆里
    已经存在的事实；信息不足时用好奇的追问承接，不补写项目进度。
    """
    text = str(user_text or "").strip()
    emotion = str(emotion_label or "").strip()
    meeting_words = ("开会", "会议", "例会", "周会", "评审会")
    mentions_meeting = any(word in text for word in meeting_words) or bool(
        re.search(r"开(?:了|过|完)?[^。！？\n]{0,16}会", text)
    )
    if emotion:
        feeling = _EMOTION_TEXT.get(emotion, "有些不舒服")
        if emotion == "frustrated":
            options = (
                "嗯，这种烦先不用讲道理。你想吐槽，我陪你；不想复盘也行。",
                "好，今天先站你这边。最烦的那一段，你想从哪儿说？",
            )
        elif emotion == "tired":
            options = (
                "那就先别撑着讲完整。想说一点就说一点，不想说也行。",
                "累了就先靠一会儿。今晚不必把每件事都整理明白。",
            )
        elif emotion in {"sad", "wronged"}:
            options = (
                "先过来待一会儿。你不用马上把话说清楚。",
                "嗯，我不催你往好处想。想说多少就说多少。",
            )
        elif emotion in {"happy", "excited"}:
            options = (
                "这个得好好听。来，最让你开心的那一秒是什么？",
                "哦，这个语气我喜欢。快讲讲，发生什么了？",
            )
        else:
            options = (
                f"嗯，你刚才说自己{feeling}。先不用急着把它处理好。",
                f"知道了，是{feeling}。你想说，我就跟着听。",
            )
        reply = _stable_choice(text + emotion, options)
        if mentions_meeting and emotion in {"frustrated", "angry", "anxious"}:
            return reply + "是会议本身，还是中间某件事特别磨人？"
        return reply

    if any(phrase in text for phrase in ("不想说", "别问了", "算了", "没什么", "没事")):
        return _stable_choice(text, (
            "好，那就不说。你不用为了让我有话接，硬找点情绪出来。",
            "行，不问了。过来待一会儿就好。",
            "那就先放这儿。什么时候想说了，再从这里继续。",
        ))

    if mentions_meeting:
        duration_stated = bool(re.search(
            r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十两]+)\s*(?:个?小时|分钟)",
            text,
        ))
        if duration_stated or any(word in text for word in ("很久", "一下午", "一上午", "一整天")):
            return _stable_choice(text, (
                "这场会开得够有存在感的。你想吐槽两句，还是今晚先不聊它？",
                "会终于开完了。里面有值得讲的事，还是只想来我这儿晃一下？",
            ))
        return "嗯，会议这件事我记下了。你是想聊聊它，还是只是顺手告诉我？"

    rows = [row for row in memories if isinstance(row, dict)]
    opening, _focus_id = build_big_whale_opening(rows)
    fact_opening, _fact_focus_id = build_big_whale_opening([
        row for row in rows if row.get("layer") != "L3"
    ])
    asks_about_memory = any(
        phrase in text
        for phrase in ("记得吗", "记得我", "我做了什么", "今天做了", "忙了多久", "用了多久", "玩了多久", "看了多久")
    )
    if asks_about_memory and opening:
        return opening

    if any(phrase in text for phrase in ("下班了", "忙完了", "做完了", "结束工作", "收工了")):
        if fact_opening:
            return f"收工。{fact_opening}"
        return _stable_choice(text, (
            "收工。今天先到这里，别急着给自己复盘。",
            "好，工作到此为止。脑子要是还没停下来，就先来我这儿坐会儿。",
        ))

    return _stable_choice(text, (
        "嗯，你继续。我跟得上。",
        "知道了。然后呢？",
        "好，我在这儿。你慢慢说。",
        "这句我收到了。你还想往下说吗？",
    ))


class MemorySyncClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 8.0) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        self.timeout = max(1.0, float(timeout))
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProtocolError("invalid memory service URL")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProtocolError("remote memory service must use HTTPS")

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else canonical_json(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(64 * 1024)
            try:
                detail = json.loads(raw.decode("utf-8")).get("error")
            except Exception:
                detail = str(exc)
            raise SyncTransportError(f"HTTP {exc.code}: {detail}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise SyncTransportError(str(exc)) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SyncTransportError("memory service response too large")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise SyncTransportError("invalid JSON response") from exc
        if not isinstance(result, dict):
            raise SyncTransportError("invalid response object")
        return result

    def post_batch(self, batch: dict) -> dict:
        clean = validate_batch(batch)
        result = self._request("POST", "/v1/memory/batches", clean)
        if result.get("batchId") != clean["batchId"] or not result.get("accepted"):
            raise SyncTransportError("server ACK did not match batch")
        return result

    def stream(self, user_id: str, after_server_seq: int = 0, limit: int = 200) -> dict:
        query = urllib.parse.urlencode({
            "userId": user_id,
            "afterServerSeq": max(0, int(after_server_seq)),
            "limit": max(1, min(500, int(limit))),
        })
        return self._request("GET", f"/v1/memory/stream?{query}")

    def claim_opening(self, *, user_id: str, device_id: str, claim_id: str) -> dict:
        return self._request("POST", "/v1/companion/openings/claim", {
            "userId": user_id,
            "deviceId": device_id,
            "claimId": claim_id,
        })

    def post_message(
        self, *, user_id: str, device_id: str, message_id: str, role: str, text: str
    ) -> dict:
        return self._request("POST", "/v1/conversation/messages", {
            "userId": user_id,
            "deviceId": device_id,
            "messageId": message_id,
            "role": role,
            "text": text,
        })

    def messages(self, user_id: str, after_message_seq: int = 0, limit: int = 200) -> dict:
        query = urllib.parse.urlencode({
            "userId": user_id,
            "afterMessageSeq": max(0, int(after_message_seq)),
            "limit": max(1, min(500, int(limit))),
        })
        return self._request("GET", f"/v1/conversation/messages?{query}")
