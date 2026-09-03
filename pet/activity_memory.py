# -*- coding: utf-8 -*-
"""桌面活动记忆：把前台应用采样整理成可验证、可去重的共享记忆。

本模块不调用模型，也不猜用户情绪：

- L1 只保存系统实际观察到的应用、标题、时长和会议等事实；
- L2 只保存可从时长直接推出的中性线索，例如“连续专注”；
- L3 只接受用户消息中明确出现的第一人称情绪表达；
- 每条新增记忆同时生成幂等 outbox 项，服务器接入后可直接增量上报。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
DEFAULT_IDLE_SECONDS = 180.0
DEFAULT_MIN_SEGMENT_SECONDS = 20.0
DEFAULT_CHECKPOINT_SECONDS = 60.0
MAX_FACTS = 5000
MAX_CLUES = 1000
MAX_EMOTIONS = 1000
MAX_OUTBOX = 6000


def _utc_iso(ts: float | None = None) -> str:
    value = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _clean_text(value: Any, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _stable_id(prefix: str, payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _default_document() -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": 0,
        "updatedAt": "",
        "facts": [],
        "clues": [],
        "emotions": [],
        "outbox": [],
        "dailySummaries": {},
        "openSegment": None,
        "uiState": {},
    }


class SharedMemoryStore:
    """单文件本地记忆库，原子写入并对 memory/outbox 同时加锁。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return _default_document()
        if not isinstance(raw, dict):
            return _default_document()
        doc = _default_document()
        doc.update(raw)
        for key in ("facts", "clues", "emotions", "outbox"):
            if not isinstance(doc.get(key), list):
                doc[key] = []
        if not isinstance(doc.get("uiState"), dict):
            doc["uiState"] = {}
        if not isinstance(doc.get("dailySummaries"), dict):
            doc["dailySummaries"] = {}
        return doc

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["schemaVersion"] = SCHEMA_VERSION
        self._data["updatedAt"] = _utc_iso()
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def _append_memory_locked(self, bucket: str, item: dict, limit: int) -> tuple[dict, bool]:
        existing = next((row for row in self._data[bucket] if row.get("id") == item["id"]), None)
        if existing is not None:
            return existing, False
        self._data["revision"] = int(self._data.get("revision") or 0) + 1
        item["revision"] = self._data["revision"]
        self._data[bucket].append(item)
        self._data[bucket] = self._data[bucket][-limit:]
        if bucket == "facts":
            self._rebuild_daily_summary_locked(item)
        batch_payload = {"memoryId": item["id"], "revision": item["revision"]}
        batch_id = _stable_id("report", batch_payload)
        if not any(row.get("batchId") == batch_id for row in self._data["outbox"]):
            self._data["outbox"].append({
                "batchId": batch_id,
                "memoryIds": [item["id"]],
                "latestRevision": item["revision"],
                "createdAt": _utc_iso(),
                "status": "pending",
                "attempts": 0,
                "ackedAt": "",
            })
            self._data["outbox"] = self._data["outbox"][-MAX_OUTBOX:]
        self._save_locked()
        return item, True

    @staticmethod
    def _local_date_from_iso(value: str) -> str:
        try:
            return datetime.fromisoformat(value).astimezone().date().isoformat()
        except (TypeError, ValueError):
            return datetime.now().date().isoformat()

    def _rebuild_daily_summary_locked(self, newest_fact: dict) -> None:
        local_date = self._local_date_from_iso(str(newest_fact.get("startedAt") or ""))
        rows = [
            row for row in self._data["facts"]
            if self._local_date_from_iso(str(row.get("startedAt") or "")) == local_date
        ]
        by_app: dict[str, float] = {}
        by_context: dict[str, float] = {}
        by_project: dict[str, float] = {}
        meeting_seconds = 0.0
        meeting_count = 0
        total = 0.0
        latest = None
        for row in rows:
            duration = max(0.0, float(row.get("durationSeconds") or 0.0))
            total += duration
            app = _clean_text(row.get("app"), 120) or "未知应用"
            context = _clean_text(row.get("context"), 40) or "idle"
            by_app[app] = by_app.get(app, 0.0) + duration
            by_context[context] = by_context.get(context, 0.0) + duration
            project = row.get("project") if isinstance(row.get("project"), dict) else {}
            project_name = _clean_text(project.get("name"), 100)
            if project_name:
                by_project[project_name] = by_project.get(project_name, 0.0) + duration
            if row.get("kind") == "meeting" or context == "meeting":
                meeting_seconds += duration
                meeting_count += 1
            if latest is None or str(row.get("endedAt") or "") > str(latest.get("endedAt") or ""):
                latest = row
        rank = lambda mapping: [
            {"name": name, "durationSeconds": round(seconds, 1)}
            for name, seconds in sorted(mapping.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
        ]
        self._data["dailySummaries"][local_date] = {
            "date": local_date,
            "totalActiveSeconds": round(total, 1),
            "byApp": rank(by_app),
            "byContext": rank(by_context),
            "byProject": rank(by_project),
            "meetingSeconds": round(meeting_seconds, 1),
            "meetingCount": meeting_count,
            "latestFactId": str((latest or {}).get("id") or ""),
            "latestAt": str((latest or {}).get("endedAt") or ""),
        }

    def daily_summary(self, local_date: str | None = None) -> dict:
        key = local_date or datetime.now().date().isoformat()
        with self._lock:
            return dict(self._data.get("dailySummaries", {}).get(key) or {})

    def append_fact(self, payload: dict) -> tuple[dict, bool]:
        clean = dict(payload)
        clean.update({
            "layer": "L1",
            "sourceType": "observed",
            "createdAt": clean.get("createdAt") or _utc_iso(),
        })
        clean["id"] = clean.get("id") or _stable_id("fact", {
            key: clean.get(key) for key in (
                "kind", "startedAt", "endedAt", "durationSeconds", "app", "title", "context"
            )
        })
        with self._lock:
            return self._append_memory_locked("facts", clean, MAX_FACTS)

    def append_clue(self, payload: dict) -> tuple[dict, bool]:
        clean = dict(payload)
        clean.update({
            "layer": "L2",
            "sourceType": "derived",
            "createdAt": clean.get("createdAt") or _utc_iso(),
        })
        clean["id"] = clean.get("id") or _stable_id("clue", {
            key: clean.get(key) for key in (
                "kind", "factIds", "durationSeconds", "app", "project", "statement"
            )
        })
        with self._lock:
            return self._append_memory_locked("clues", clean, MAX_CLUES)

    def append_emotion(self, text: str, label: str, source_message_id: str = "") -> tuple[dict, bool]:
        payload = {
            "layer": "L3",
            "kind": "stated_emotion",
            "sourceType": "user_stated",
            "label": _clean_text(label, 40),
            "quote": _clean_text(text, 500),
            "sourceMessageId": _clean_text(source_message_id, 80),
            "occurredAt": _utc_iso(),
        }
        payload["id"] = _stable_id("emotion", {
            "sourceMessageId": payload["sourceMessageId"],
            "quote": payload["quote"],
            "label": payload["label"],
        })
        with self._lock:
            return self._append_memory_locked("emotions", payload, MAX_EMOTIONS)

    def set_open_segment(self, payload: dict | None) -> None:
        with self._lock:
            self._data["openSegment"] = dict(payload) if isinstance(payload, dict) else None
            self._save_locked()

    def take_open_segment(self) -> dict | None:
        with self._lock:
            value = self._data.get("openSegment")
            self._data["openSegment"] = None
            if value:
                self._save_locked()
            return dict(value) if isinstance(value, dict) else None

    def pending_reports(self, limit: int = 100) -> list[dict]:
        """返回所有尚未 ACK 的批次；sent 也必须允许重试，避免断网丢报。"""
        with self._lock:
            rows = [
                dict(row) for row in self._data["outbox"]
                if row.get("status") in ("pending", "sent")
            ]
            return rows[:max(0, int(limit))]

    def memories_for_ids(self, memory_ids: list[str]) -> list[dict]:
        """按请求顺序返回记忆；outbox 只存 ID，上传时再取权威内容。"""
        wanted = [str(value) for value in memory_ids if value]
        with self._lock:
            by_id = {
                str(row.get("id")): row
                for bucket in ("facts", "clues", "emotions")
                for row in self._data[bucket]
                if row.get("id")
            }
            return [
                json.loads(json.dumps(by_id[memory_id], ensure_ascii=False))
                for memory_id in wanted
                if memory_id in by_id
            ]

    def mark_report_sent(self, batch_id: str) -> bool:
        with self._lock:
            for row in self._data["outbox"]:
                if row.get("batchId") == batch_id and row.get("status") in ("pending", "sent"):
                    row["status"] = "sent"
                    row["attempts"] = int(row.get("attempts") or 0) + 1
                    row["sentAt"] = _utc_iso()
                    self._save_locked()
                    return True
        return False

    def acknowledge_report(self, batch_id: str) -> bool:
        """服务器确认后调用；重复确认同一批次不会重复改变状态。"""
        with self._lock:
            for row in self._data["outbox"]:
                if row.get("batchId") != batch_id:
                    continue
                if row.get("status") == "acked":
                    return False
                row["status"] = "acked"
                row["ackedAt"] = _utc_iso()
                self._save_locked()
                return True
        return False

    def mark_notice_shown(self, local_date: str) -> bool:
        with self._lock:
            if self._data["uiState"].get("lastMemoryNoticeDate") == local_date:
                return False
            self._data["uiState"]["lastMemoryNoticeDate"] = local_date
            self._save_locked()
            return True


_EMOTION_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("frustrated", ("好烦", "很烦", "烦死", "烦躁", "不爽", "闹心")),
    ("angry", ("生气", "气死", "恼火", "火大")),
    ("sad", ("难过", "伤心", "想哭", "低落", "沮丧")),
    ("anxious", ("焦虑", "紧张", "担心", "慌")),
    ("tired", ("好累", "很累", "累死", "疲惫", "没力气")),
    ("happy", ("开心", "高兴", "快乐", "爽到了")),
    ("excited", ("兴奋", "激动", "期待")),
    ("wronged", ("委屈", "憋屈")),
)


def explicit_emotion_label(text: str) -> str:
    """只识别用户明确说出的情绪词；没有明确表达就返回空串。"""
    value = _clean_text(text, 1000)
    if not value:
        return ""
    # 必须有第一人称或强烈的直接感受句式，避免把“客户很生气”记成用户情绪。
    first_person = any(token in value for token in ("我", "本人", "自己"))
    direct_feeling = bool(re.search(r"(?:^|[，。！？\s])(?:好|很|太|真|有点|特别)(?:烦|累|开心|难过|焦虑|生气|委屈)", value))
    if not first_person and not direct_feeling:
        return ""
    for label, words in _EMOTION_RULES:
        if any(word in value for word in words):
            return label
    return ""


class ProjectEnricher:
    """从开发工具标题与已知工作区读取项目名和 README 简介。"""

    DEV_TOKENS = ("visual studio code", "code", "cursor", "dsh", "deepseek", "harness", "pycharm", "idea")
    APP_SUFFIXES = ("visual studio code", "cursor", "pycharm", "intellij idea", "dsh", "deepseek")

    def __init__(self, roots: list[str | Path] | None = None) -> None:
        candidates: list[Path] = []
        env_root = os.environ.get("DSH_WORKSPACE", "").strip()
        if env_root:
            candidates.append(Path(env_root))
        candidates.extend(Path(value) for value in (roots or []))
        try:
            cwd = Path.cwd()
            if cwd != Path(cwd.anchor):
                candidates.append(cwd)
        except OSError:
            pass
        self.roots: list[Path] = []
        for path in candidates:
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                continue
            if resolved.is_dir() and resolved not in self.roots:
                self.roots.append(resolved)
        self._readme_cache: dict[str, tuple[int, dict]] = {}

    @classmethod
    def project_name_from_title(cls, app_name: str, title: str) -> str:
        app = _clean_text(app_name).lower()
        value = _clean_text(title)
        if not value or not any(token in app for token in cls.DEV_TOKENS):
            return ""
        parts = [_clean_text(part, 100) for part in re.split(r"\s+[—–-]\s+", value)]
        parts = [part for part in parts if part]
        while parts and parts[-1].lower() in cls.APP_SUFFIXES:
            parts.pop()
        if len(parts) >= 2:
            return parts[-1]
        if parts and not re.search(r"\.[a-z0-9]{1,8}$", parts[0], re.I):
            return parts[0]
        return ""

    def _match_root(self, project_name: str, app_name: str) -> Path | None:
        target = project_name.casefold()
        for root in self.roots:
            if target and root.name.casefold() == target:
                return root
            if target:
                child = root / project_name
                if child.is_dir():
                    return child
        app = app_name.casefold()
        if any(token in app for token in ("dsh", "deepseek", "harness")):
            return self.roots[0] if self.roots else None
        return None

    def _readme_info(self, root: Path) -> dict:
        readme = next((root / name for name in ("README.md", "README.MD", "readme.md", "README.txt") if (root / name).is_file()), None)
        if readme is None:
            return {}
        try:
            stat = readme.stat()
            key = str(readme)
            cached = self._readme_cache.get(key)
            if cached and cached[0] == stat.st_mtime_ns:
                return dict(cached[1])
            text = readme.read_text(encoding="utf-8", errors="replace")[:12000]
        except OSError:
            return {}
        title = ""
        paragraphs: list[str] = []
        block: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not title and line.startswith("#"):
                title = line.lstrip("#").strip()[:100]
                continue
            if not line:
                if block:
                    paragraphs.append(" ".join(block))
                    block = []
                continue
            if line.startswith(("#", "!", "[", "```", "<", "- ", "* ", ">")):
                continue
            block.append(line)
            if len(" ".join(block)) >= 300:
                break
        if block:
            paragraphs.append(" ".join(block))
        summary = _clean_text(paragraphs[0] if paragraphs else "", 320)
        info = {
            "name": title or root.name,
            "summary": summary,
            "readmeHash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        }
        self._readme_cache[str(readme)] = (stat.st_mtime_ns, info)
        return dict(info)

    def enrich(self, app: dict) -> dict:
        name = _clean_text(app.get("name") or app.get("bundle"), 120)
        title = _clean_text(app.get("title"), 240)
        project_name = self.project_name_from_title(name, title)
        root = self._match_root(project_name, name)
        if root is None:
            return {"name": project_name} if project_name else {}
        info = self._readme_info(root)
        if not info:
            info = {"name": project_name or root.name}
        elif project_name:
            info["name"] = project_name
        return info


class ActivityCollector:
    """把周期采样合并为连续活动段，并写入 SharedMemoryStore。"""

    def __init__(
        self,
        store: SharedMemoryStore,
        *,
        project_enricher: ProjectEnricher | None = None,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        min_segment_seconds: float = DEFAULT_MIN_SEGMENT_SECONDS,
        checkpoint_seconds: float = DEFAULT_CHECKPOINT_SECONDS,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.project_enricher = project_enricher or ProjectEnricher()
        self.idle_seconds = max(30.0, float(idle_seconds))
        self.min_segment_seconds = max(1.0, float(min_segment_seconds))
        self.checkpoint_seconds = max(5.0, float(checkpoint_seconds))
        self.wall_clock = wall_clock
        self.mono_clock = mono_clock
        self._current: dict | None = None
        self._last_checkpoint_mono = 0.0
        self._lock = threading.RLock()
        self._recover_previous_open_segment()

    @staticmethod
    def _key(app: dict, context: str, project: dict) -> tuple:
        return (
            _clean_text(app.get("name") or app.get("bundle"), 120).casefold(),
            _clean_text(app.get("title"), 240).casefold(),
            _clean_text(context, 40),
            _clean_text(project.get("name"), 100).casefold(),
        )

    def _recover_previous_open_segment(self) -> None:
        previous = self.store.take_open_segment()
        if not previous:
            return
        try:
            duration = max(0.0, float(previous["lastSeenTs"]) - float(previous["startedTs"]))
        except (KeyError, TypeError, ValueError):
            return
        if duration >= self.min_segment_seconds:
            self._commit(previous, float(previous["lastSeenTs"]))

    def _checkpoint(self, mono_now: float) -> None:
        if self._current is None:
            return
        if mono_now - self._last_checkpoint_mono < self.checkpoint_seconds:
            return
        self._last_checkpoint_mono = mono_now
        self.store.set_open_segment(self._current)

    def observe(
        self,
        app: dict | None,
        context: str,
        *,
        idle_for_seconds: float = 0.0,
        wall_now: float | None = None,
        mono_now: float | None = None,
    ) -> list[dict]:
        wall_now = self.wall_clock() if wall_now is None else float(wall_now)
        mono_now = self.mono_clock() if mono_now is None else float(mono_now)
        observed = dict(app) if isinstance(app, dict) else {}
        inactive = float(idle_for_seconds or 0.0) >= self.idle_seconds
        with self._lock:
            committed: list[dict] = []
            if inactive or not (observed.get("name") or observed.get("bundle")):
                if self._current is not None:
                    # HIDIdleTime 可精确指出最后输入时间，避免把离开后的时间算进应用。
                    end_ts = max(float(self._current["startedTs"]), wall_now - float(idle_for_seconds or 0.0))
                    fact = self._close(end_ts)
                    if fact:
                        committed.append(fact)
                return committed

            project = self.project_enricher.enrich(observed)
            key = self._key(observed, context, project)
            if self._current is not None and tuple(self._current.get("key") or ()) == key:
                self._current["lastSeenTs"] = wall_now
                self._checkpoint(mono_now)
                return committed

            if self._current is not None:
                fact = self._close(wall_now)
                if fact:
                    committed.append(fact)
            self._current = {
                "key": list(key),
                "startedTs": wall_now,
                "lastSeenTs": wall_now,
                "app": _clean_text(observed.get("name") or observed.get("bundle"), 120),
                "bundle": _clean_text(observed.get("bundle"), 160),
                "title": _clean_text(observed.get("title"), 240),
                "context": _clean_text(context, 40) or "idle",
                "project": project,
            }
            self._checkpoint(mono_now)
            return committed

    def _close(self, end_ts: float) -> dict | None:
        segment = self._current
        self._current = None
        if segment is None:
            return None
        fact = self._commit(segment, end_ts)
        # 先落最终事实，再清 checkpoint。若两步之间崩溃，恢复时 deterministic id
        # 会把重复事实挡掉；反过来先清 checkpoint 则可能永久丢失最后一段。
        self.store.set_open_segment(None)
        return fact

    def _commit(self, segment: dict, end_ts: float) -> dict | None:
        start_ts = float(segment.get("startedTs") or end_ts)
        end_ts = max(start_ts, float(end_ts))
        duration = round(end_ts - start_ts, 1)
        if duration < self.min_segment_seconds:
            return None
        context = _clean_text(segment.get("context"), 40) or "idle"
        kind = "meeting" if context == "meeting" else "activity"
        payload = {
            "kind": kind,
            "startedAt": _utc_iso(start_ts),
            "endedAt": _utc_iso(end_ts),
            "durationSeconds": duration,
            "app": _clean_text(segment.get("app"), 120),
            "bundle": _clean_text(segment.get("bundle"), 160),
            "title": _clean_text(segment.get("title"), 240),
            "context": context,
            "project": dict(segment.get("project") or {}),
            "confidence": "observed",
        }
        fact, added = self.store.append_fact(payload)
        if added and duration >= 30 * 60 and context in ("work", "gaming", "idle"):
            self.store.append_clue({
                "kind": "long_continuous_activity",
                "factIds": [fact["id"]],
                "durationSeconds": duration,
                "app": fact["app"],
                "project": fact.get("project") or {},
                "statement": f"连续在{fact['app']}停留了约{round(duration / 60)}分钟",
                "confidence": "derived_from_duration",
            })
        return fact if added else None

    def flush(self, wall_now: float | None = None) -> dict | None:
        with self._lock:
            return self._close(self.wall_clock() if wall_now is None else float(wall_now))


_idle_cache_lock = threading.Lock()
_idle_cache_at = 0.0
_idle_cache_value = 0.0


def detect_system_idle_seconds(cache_seconds: float = 5.0) -> float:
    """无内容读取的系统空闲时长；失败时返回 0，宁可少记空闲也不误删事实。"""
    global _idle_cache_at, _idle_cache_value
    now = time.monotonic()
    with _idle_cache_lock:
        if now - _idle_cache_at < max(0.0, cache_seconds):
            return _idle_cache_value
    value = 0.0
    if sys.platform == "win32":
        try:
            from .vision import get_system_idle_seconds
            value = float(get_system_idle_seconds())
        except Exception:
            value = 0.0
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/ioreg", "-c", "IOHIDSystem"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout or "")
            if match:
                value = int(match.group(1)) / 1_000_000_000.0
        except Exception:
            value = 0.0
    with _idle_cache_lock:
        _idle_cache_at = now
        _idle_cache_value = max(0.0, value)
        return _idle_cache_value
