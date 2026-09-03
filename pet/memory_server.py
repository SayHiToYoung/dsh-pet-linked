# -*- coding: utf-8 -*-
"""单用户共享记忆服务：SQLite 持久化、幂等批次与原子开场领取。"""
from __future__ import annotations

import hmac
import json
import mimetypes
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .activity_memory import explicit_emotion_label
from .companion_llm import CompanionResponder
from .memory_protocol import (
    ProtocolError,
    build_big_whale_opening,
    build_grounded_companion_reply,
    canonical_json,
    payload_hash,
    validate_batch,
)


MAX_REQUEST_BYTES = 512 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryConflictError(RuntimeError):
    pass


class MemoryRepository:
    def __init__(self, path: Path | str, responder: CompanionResponder | None = None) -> None:
        self.path = Path(path)
        self.responder = responder
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._reply_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._init_lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS memory_events (
                    server_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    client_revision INTEGER NOT NULL,
                    layer TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    memory_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    UNIQUE(user_id, memory_id)
                );
                CREATE INDEX IF NOT EXISTS idx_memory_stream
                    ON memory_events(user_id, server_seq);
                CREATE TABLE IF NOT EXISTS memory_batches (
                    user_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    latest_revision INTEGER NOT NULL,
                    payload_hash TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, batch_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(user_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_latest
                    ON conversation_messages(user_id, message_seq DESC);
                CREATE TABLE IF NOT EXISTS companion_cursors (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    last_memory_seq INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, device_id)
                );
                CREATE TABLE IF NOT EXISTS opening_claims (
                    user_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, device_id, claim_id)
                );
            """)

    @staticmethod
    def _memory_time(memory: dict) -> str:
        for key in ("occurredAt", "endedAt", "startedAt", "createdAt"):
            if memory.get(key):
                return str(memory[key])[:80]
        return ""

    def ingest_batch(self, payload: dict) -> dict:
        batch = validate_batch(payload)
        body_hash = payload_hash({
            "latestRevision": batch["latestRevision"],
            "memories": batch["memories"],
        })
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing_batch = db.execute(
                "SELECT payload_hash FROM memory_batches WHERE user_id=? AND batch_id=?",
                (batch["userId"], batch["batchId"]),
            ).fetchone()
            if existing_batch is not None:
                if existing_batch["payload_hash"] != body_hash:
                    raise MemoryConflictError("batchId already exists with different payload")
                latest_seq = db.execute(
                    "SELECT COALESCE(MAX(server_seq), 0) FROM memory_events WHERE user_id=?",
                    (batch["userId"],),
                ).fetchone()[0]
                return {
                    "accepted": True,
                    "duplicate": True,
                    "batchId": batch["batchId"],
                    "acceptedCount": 0,
                    "latestServerSeq": int(latest_seq),
                    "receivedAt": now,
                }
            accepted = 0
            for memory in batch["memories"]:
                encoded = canonical_json(memory)
                digest = payload_hash(memory)
                existing = db.execute(
                    "SELECT payload_hash FROM memory_events WHERE user_id=? AND memory_id=?",
                    (batch["userId"], memory["id"]),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != digest:
                        raise MemoryConflictError("memoryId already exists with different payload")
                    continue
                db.execute(
                    """INSERT INTO memory_events
                       (user_id, device_id, memory_id, client_revision, layer, kind,
                        memory_time, payload_json, payload_hash, received_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch["userId"], batch["deviceId"], memory["id"], memory["revision"],
                        memory["layer"], str(memory.get("kind") or "")[:80],
                        self._memory_time(memory), encoded, digest, now,
                    ),
                )
                accepted += 1
            db.execute(
                """INSERT INTO memory_batches
                   (user_id, batch_id, device_id, latest_revision, payload_hash, accepted_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    batch["userId"], batch["batchId"], batch["deviceId"],
                    batch["latestRevision"], body_hash, now,
                ),
            )
            latest_seq = db.execute(
                "SELECT COALESCE(MAX(server_seq), 0) FROM memory_events WHERE user_id=?",
                (batch["userId"],),
            ).fetchone()[0]
            return {
                "accepted": True,
                "duplicate": accepted == 0,
                "batchId": batch["batchId"],
                "acceptedCount": accepted,
                "latestServerSeq": int(latest_seq),
                "receivedAt": now,
            }

    def stream(self, user_id: str, after_server_seq: int = 0, limit: int = 200) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT server_seq, device_id, payload_json, received_at
                   FROM memory_events WHERE user_id=? AND server_seq>?
                   ORDER BY server_seq ASC LIMIT ?""",
                (user_id, max(0, int(after_server_seq)), max(1, min(500, int(limit)))),
            ).fetchall()
        memories = []
        for row in rows:
            memory = json.loads(row["payload_json"])
            memory["serverSeq"] = int(row["server_seq"])
            memory["sourceDeviceId"] = row["device_id"]
            memory["receivedAt"] = row["received_at"]
            memories.append(memory)
        return {
            "memories": memories,
            "nextServerSeq": int(rows[-1]["server_seq"]) if rows else max(0, int(after_server_seq)),
            "hasMore": len(rows) >= max(1, min(500, int(limit))),
        }

    def append_message(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        device_id = str(payload.get("deviceId") or "").strip()
        message_id = str(payload.get("messageId") or "").strip()
        role = str(payload.get("role") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not user_id or not device_id or not message_id or role not in {"user", "assistant"}:
            raise ProtocolError("invalid conversation message")
        if not text or len(text) > 4000:
            raise ProtocolError("invalid conversation text")
        now = _now()
        emotion_label = explicit_emotion_label(text) if role == "user" else ""
        emotion_id = (
            "emotion_" + payload_hash({
                "userId": user_id,
                "messageId": message_id,
                "label": emotion_label,
            })[:24]
            if emotion_label else ""
        )
        reply_id = (
            "reply_" + payload_hash({"userId": user_id, "messageId": message_id})[:24]
            if role == "user" else ""
        )
        duplicate = False
        emotion_recorded = False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """SELECT role, text FROM conversation_messages
                   WHERE user_id=? AND message_id=?""",
                (user_id, message_id),
            ).fetchone()
            if existing is not None:
                if existing["role"] != role or existing["text"] != text:
                    raise MemoryConflictError("messageId already exists with different payload")
                duplicate = True
                emotion_recorded = bool(emotion_id and db.execute(
                    "SELECT 1 FROM memory_events WHERE user_id=? AND memory_id=?",
                    (user_id, emotion_id),
                ).fetchone())
            else:
                cursor = db.execute(
                    """INSERT INTO conversation_messages
                       (user_id, device_id, message_id, role, text, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, device_id, message_id, role, text, now),
                )
                if emotion_label:
                    memory = {
                        "id": emotion_id,
                        "revision": int(cursor.lastrowid),
                        "layer": "L3",
                        "sourceType": "user_stated",
                        "kind": "stated_emotion",
                        "label": emotion_label,
                        "quote": text,
                        "sourceMessageId": message_id,
                        "occurredAt": now,
                    }
                    encoded = canonical_json(memory)
                    db.execute(
                        """INSERT INTO memory_events
                           (user_id, device_id, memory_id, client_revision, layer, kind,
                            memory_time, payload_json, payload_hash, received_at)
                           VALUES (?, ?, ?, ?, 'L3', 'stated_emotion', ?, ?, ?, ?)""",
                        (
                            user_id, device_id, emotion_id, memory["revision"], now,
                            encoded, payload_hash(memory), now,
                        ),
                    )
                    emotion_recorded = True

        assistant_message = None
        reply_source = ""
        if role == "user":
            assistant_message, reply_source = self._ensure_assistant_reply(
                user_id=user_id,
                device_id=device_id,
                reply_id=reply_id,
                user_text=text,
                emotion_label=emotion_label,
            )
        return {
            "accepted": True,
            "duplicate": duplicate,
            "messageId": message_id,
            "emotionRecorded": emotion_recorded,
            "emotionMemoryId": emotion_id if emotion_recorded else "",
            "assistantMessage": assistant_message,
            "replySource": reply_source,
        }

    def _ensure_assistant_reply(
        self,
        *,
        user_id: str,
        device_id: str,
        reply_id: str,
        user_text: str,
        emotion_label: str,
    ) -> tuple[dict, str]:
        with self._reply_lock:
            with self._connect() as db:
                existing = db.execute(
                    """SELECT message_id, role, text, created_at FROM conversation_messages
                       WHERE user_id=? AND message_id=?""",
                    (user_id, reply_id),
                ).fetchone()
                if existing is not None:
                    return self._message_payload(existing), "stored"
                memory_rows = db.execute(
                    """SELECT server_seq, payload_json FROM memory_events
                       WHERE user_id=? ORDER BY server_seq DESC LIMIT 200""",
                    (user_id,),
                ).fetchall()
                message_rows = db.execute(
                    """SELECT role, text FROM conversation_messages
                       WHERE user_id=? ORDER BY message_seq DESC LIMIT 24""",
                    (user_id,),
                ).fetchall()

            memories = [json.loads(row["payload_json"]) for row in reversed(memory_rows)]
            conversation = [
                {"role": row["role"], "text": row["text"]}
                for row in reversed(message_rows)
            ]
            reply_text = build_grounded_companion_reply(
                user_text,
                memories,
                emotion_label=emotion_label,
            )
            reply_source = "fallback"
            responder = self.responder
            if responder is not None and responder.available:
                try:
                    candidate = str(responder.reply(memories, conversation) or "").strip()
                    if candidate:
                        reply_text = candidate[:4000]
                        reply_source = "model"
                except Exception:
                    reply_source = "fallback"

            reply_time = _now()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT OR IGNORE INTO conversation_messages
                       (user_id, device_id, message_id, role, text, created_at)
                       VALUES (?, ?, ?, 'assistant', ?, ?)""",
                    (user_id, device_id, reply_id, reply_text, reply_time),
                )
                stored = db.execute(
                    """SELECT message_id, role, text, created_at FROM conversation_messages
                       WHERE user_id=? AND message_id=?""",
                    (user_id, reply_id),
                ).fetchone()
                if memory_rows:
                    through = int(memory_rows[0]["server_seq"])
                    db.execute(
                        """INSERT INTO companion_cursors
                           (user_id, device_id, last_memory_seq, updated_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(user_id, device_id) DO UPDATE SET
                             last_memory_seq=MAX(last_memory_seq, excluded.last_memory_seq),
                             updated_at=excluded.updated_at""",
                        (user_id, device_id, through, reply_time),
                    )
            return self._message_payload(stored), reply_source

    def companion_status(self) -> dict:
        responder = self.responder
        enabled = bool(responder is not None and responder.available)
        return {
            "modelEnabled": enabled,
            "model": str(responder.name) if enabled else "",
            "fallback": "grounded_rules",
        }

    @staticmethod
    def _message_payload(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "messageId": row["message_id"],
            "role": row["role"],
            "text": row["text"],
            "createdAt": row["created_at"],
        }

    def messages(self, user_id: str, after_message_seq: int = 0, limit: int = 200) -> dict:
        with self._connect() as db:
            rows = db.execute(
                """SELECT message_seq, device_id, message_id, role, text, created_at
                   FROM conversation_messages
                   WHERE user_id=? AND message_seq>?
                   ORDER BY message_seq ASC LIMIT ?""",
                (
                    user_id,
                    max(0, int(after_message_seq)),
                    max(1, min(500, int(limit))),
                ),
            ).fetchall()
        messages = [{
            "messageSeq": int(row["message_seq"]),
            "deviceId": row["device_id"],
            "messageId": row["message_id"],
            "role": row["role"],
            "text": row["text"],
            "createdAt": row["created_at"],
        } for row in rows]
        return {
            "messages": messages,
            "nextMessageSeq": int(rows[-1]["message_seq"]) if rows else max(0, int(after_message_seq)),
            "hasMore": len(rows) >= max(1, min(500, int(limit))),
        }

    def claim_opening(self, payload: dict) -> dict:
        user_id = str(payload.get("userId") or "").strip()
        device_id = str(payload.get("deviceId") or "").strip()
        claim_id = str(payload.get("claimId") or "").strip()
        if not user_id or not device_id or not claim_id or len(claim_id) > 128:
            raise ProtocolError("invalid opening claim")
        now = _now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous_claim = db.execute(
                """SELECT response_json FROM opening_claims
                   WHERE user_id=? AND device_id=? AND claim_id=?""",
                (user_id, device_id, claim_id),
            ).fetchone()
            if previous_claim is not None:
                response = json.loads(previous_claim["response_json"])
                response["duplicateClaim"] = True
                return response
            latest_message = db.execute(
                """SELECT message_id, role, text, created_at FROM conversation_messages
                   WHERE user_id=? ORDER BY message_seq DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if latest_message is not None and latest_message["role"] == "assistant":
                response = {
                    "shouldSend": False,
                    "reason": "awaiting_user_reply",
                    "pendingAssistant": {
                        "messageId": latest_message["message_id"],
                        "text": latest_message["text"],
                        "createdAt": latest_message["created_at"],
                    },
                }
            else:
                cursor_row = db.execute(
                    """SELECT last_memory_seq FROM companion_cursors
                       WHERE user_id=? AND device_id=?""",
                    (user_id, device_id),
                ).fetchone()
                cursor = int(cursor_row["last_memory_seq"]) if cursor_row else 0
                rows = db.execute(
                    """SELECT server_seq, payload_json FROM memory_events
                       WHERE user_id=? AND server_seq>? ORDER BY server_seq ASC LIMIT 500""",
                    (user_id, cursor),
                ).fetchall()
                if not rows:
                    response = {"shouldSend": False, "reason": "no_new_memory"}
                else:
                    memories = [json.loads(row["payload_json"]) for row in rows]
                    text, focus_id = build_big_whale_opening(memories)
                    through = int(rows[-1]["server_seq"])
                    if not text:
                        response = {"shouldSend": False, "reason": "no_speakable_memory"}
                    else:
                        digest = payload_hash({
                            "userId": user_id, "deviceId": device_id,
                            "from": cursor, "through": through,
                        })[:24]
                        message_id = f"opening_{digest}"
                        db.execute(
                            """INSERT OR IGNORE INTO conversation_messages
                               (user_id, device_id, message_id, role, text, created_at)
                               VALUES (?, ?, ?, 'assistant', ?, ?)""",
                            (user_id, device_id, message_id, text, now),
                        )
                        db.execute(
                            """INSERT INTO companion_cursors
                               (user_id, device_id, last_memory_seq, updated_at)
                               VALUES (?, ?, ?, ?)
                               ON CONFLICT(user_id, device_id) DO UPDATE SET
                                 last_memory_seq=excluded.last_memory_seq,
                                 updated_at=excluded.updated_at""",
                            (user_id, device_id, through, now),
                        )
                        latest_memory = max(
                            memories,
                            key=lambda item: (
                                str(item.get("occurredAt") or item.get("endedAt") or item.get("createdAt") or ""),
                                int(item.get("revision") or 0),
                            ),
                        )
                        response = {
                            "shouldSend": True,
                            "reason": "new_memory",
                            "messageId": message_id,
                            "text": text,
                            "focusMemoryId": focus_id,
                            "consumedThroughServerSeq": through,
                            "latestMemory": latest_memory,
                        }
            db.execute(
                """INSERT INTO opening_claims
                   (user_id, device_id, claim_id, response_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, device_id, claim_id, canonical_json(response), now),
            )
            return response


class MemoryApiServer:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        token: str,
        host: str = "127.0.0.1",
        port: int = 47821,
        static_dir: Path | str | None = None,
    ) -> None:
        self.repository = repository
        self.token = str(token)
        self.host = host
        self.port = int(port)
        self.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parents[1] / "mobile"
        self.asset_dir = Path(__file__).resolve().parents[1] / "assets"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self):
        repository = self.repository
        expected_token = self.token
        static_dir = self.static_dir
        asset_dir = self.asset_dir

        class Handler(BaseHTTPRequestHandler):
            server_version = "WhaleMemory/1"

            def log_message(self, fmt, *args):
                return

            def _json(self, status: int, payload: dict) -> None:
                body = canonical_json(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: Path, *, cache: bool = True) -> None:
                try:
                    body = path.read_bytes()
                except OSError:
                    self._json(404, {"error": "not_found"})
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                    "script-src 'self'; connect-src 'self'; manifest-src 'self'",
                )
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                value = self.headers.get("Authorization", "")
                supplied = value[7:] if value.startswith("Bearer ") else ""
                return bool(expected_token) and hmac.compare_digest(supplied, expected_token)

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    raise ProtocolError("invalid Content-Length") from None
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ProtocolError("invalid request size")
                try:
                    value = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise ProtocolError("invalid JSON body") from None
                if not isinstance(value, dict):
                    raise ProtocolError("body must be an object")
                return value

            def _dispatch(self) -> None:
                parsed = urlparse(self.path)
                static_routes = {
                    "/": "index.html",
                    "/index.html": "index.html",
                    "/styles.css": "styles.css",
                    "/app.js": "app.js",
                    "/manifest.webmanifest": "manifest.webmanifest",
                    "/sw.js": "sw.js",
                }
                media_routes = {
                    "/media/whale-v2.jpg": asset_dir / "chat" / "whale-v2.jpg",
                    "/media/ojingjing.jpg": asset_dir / "big_blue_fat_fish" / "ojingjing.jpg",
                }
                if self.command == "GET" and parsed.path in static_routes:
                    self._file(static_dir / static_routes[parsed.path], cache=parsed.path != "/sw.js")
                    return
                if self.command == "GET" and parsed.path in media_routes:
                    self._file(media_routes[parsed.path])
                    return
                if parsed.path == "/health" and self.command == "GET":
                    self._json(200, {
                        "ok": True,
                        "protocolVersion": 1,
                        "companion": repository.companion_status(),
                    })
                    return
                if not self._authorized():
                    self._json(401, {"error": "unauthorized"})
                    return
                if self.command == "POST" and parsed.path == "/v1/memory/batches":
                    self._json(200, repository.ingest_batch(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/memory/stream":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    after = int((query.get("afterServerSeq") or ["0"])[0])
                    limit = int((query.get("limit") or ["200"])[0])
                    self._json(200, repository.stream(user_id, after, limit))
                    return
                if self.command == "POST" and parsed.path == "/v1/companion/openings/claim":
                    self._json(200, repository.claim_opening(self._body()))
                    return
                if self.command == "POST" and parsed.path == "/v1/conversation/messages":
                    self._json(200, repository.append_message(self._body()))
                    return
                if self.command == "GET" and parsed.path == "/v1/conversation/messages":
                    query = parse_qs(parsed.query)
                    user_id = str((query.get("userId") or [""])[0]).strip()
                    if not user_id:
                        raise ProtocolError("userId required")
                    after = int((query.get("afterMessageSeq") or ["0"])[0])
                    limit = int((query.get("limit") or ["200"])[0])
                    self._json(200, repository.messages(user_id, after, limit))
                    return
                self._json(404, {"error": "not_found"})

            def do_GET(self):  # noqa: N802
                try:
                    self._dispatch()
                except ProtocolError as exc:
                    self._json(400, {"error": str(exc)})
                except (TypeError, ValueError):
                    self._json(400, {"error": "invalid query"})
                except MemoryConflictError as exc:
                    self._json(409, {"error": str(exc)})
                except Exception:
                    self._json(500, {"error": "internal_error"})

            do_POST = do_GET

        return Handler

    def start(self) -> int:
        if self._server is not None:
            return self.port
        self._server = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="whale-memory-api",
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(2.0)
