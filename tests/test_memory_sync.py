from __future__ import annotations

import urllib.request
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pet.activity_memory import SharedMemoryStore
from pet.chat.models import ProviderConfig
from pet.companion_llm import (
    ModelCompanionResponder,
    build_model_context,
    model_reply_is_grounded,
)
from pet.memory_protocol import (
    MemorySyncClient,
    ProtocolError,
    SyncTransportError,
    build_big_whale_opening,
    build_grounded_companion_reply,
    make_batch,
)
from pet.memory_server import MemoryApiServer, MemoryConflictError, MemoryRepository
from pet.memory_sync import MemorySyncManager


def _fact(memory_id: str = "fact-1", revision: int = 1, **overrides) -> dict:
    value = {
        "id": memory_id,
        "revision": revision,
        "layer": "L1",
        "sourceType": "observed",
        "kind": "activity",
        "app": "Visual Studio Code",
        "context": "work",
        "durationSeconds": 3600,
        "startedAt": "2026-09-03T01:00:00+00:00",
        "endedAt": "2026-09-03T02:00:00+00:00",
        "project": {"name": "小鲸"},
    }
    value.update(overrides)
    return value


def _batch(batch_id: str = "batch-1", memory: dict | None = None) -> dict:
    item = memory or _fact()
    return {
        "protocolVersion": 1,
        "userId": "user-1",
        "deviceId": "desktop-1",
        "batchId": batch_id,
        "latestRevision": item["revision"],
        "memories": [item],
    }


def test_protocol_rejects_layer_source_mismatch_and_remote_plain_http() -> None:
    bad = _fact(sourceType="user_stated")
    with pytest.raises(ProtocolError):
        make_batch(
            user_id="user-1",
            device_id="desktop-1",
            report={"batchId": "batch-1", "latestRevision": 1},
            memories=[bad],
        )
    with pytest.raises(ProtocolError):
        MemorySyncClient("http://example.com", "token")


def test_memory_sync_settings_survive_config_reload(tmp_path: Path) -> None:
    from pet.config import Config

    config = Config(tmp_path)
    config.set("memory_sync_enabled", True)
    config.set("memory_sync_url", "https://memory.example")
    config.set("memory_sync_token", "pairing-token")
    config.save()
    loaded = Config(tmp_path)
    assert loaded.get("memory_sync_enabled") is True
    assert loaded.get("memory_sync_url") == "https://memory.example"
    assert loaded.get("memory_sync_token") == "pairing-token"


def test_repository_batch_is_idempotent_and_conflicts_on_changed_payload(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    first = repository.ingest_batch(_batch())
    duplicate = repository.ingest_batch(_batch())
    assert first["acceptedCount"] == 1
    assert duplicate["duplicate"] is True
    assert repository.stream("user-1")["memories"][0]["id"] == "fact-1"

    changed = _batch()
    changed["memories"][0]["durationSeconds"] = 7200
    with pytest.raises(MemoryConflictError):
        repository.ingest_batch(changed)


def test_opening_claim_is_recoverable_and_never_repeats_while_waiting(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    repository.ingest_batch(_batch())

    first = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-1",
    })
    retry = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-1",
    })
    blocked = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-2",
    })
    assert first["shouldSend"] is True
    assert "小鲸" in first["text"]
    assert retry["messageId"] == first["messageId"]
    assert retry["duplicateClaim"] is True
    assert blocked["shouldSend"] is False
    assert blocked["reason"] == "awaiting_user_reply"
    assert blocked["pendingAssistant"]["messageId"] == first["messageId"]

    repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "reply-1",
        "role": "user", "text": "忙完了",
    })
    waiting = repository.claim_opening({
        "userId": "user-1", "deviceId": "phone-1", "claimId": "claim-3",
    })
    assert waiting["shouldSend"] is False
    assert waiting["reason"] == "awaiting_user_reply"


def test_phone_reply_only_writes_l3_when_user_states_emotion(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    neutral = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "neutral-1",
        "role": "user", "text": "今天开了三个小时的会",
    })
    emotional = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-1",
        "role": "user", "text": "今天开了三个小时的会，我好烦啊",
    })
    duplicate = repository.append_message({
        "userId": "user-1", "deviceId": "phone-1", "messageId": "emotion-1",
        "role": "user", "text": "今天开了三个小时的会，我好烦啊",
    })
    assert neutral["emotionRecorded"] is False
    assert emotional["emotionRecorded"] is True
    assert duplicate["emotionMemoryId"] == emotional["emotionMemoryId"]
    memories = repository.stream("user-1")["memories"]
    assert len(memories) == 1
    assert memories[0]["layer"] == "L3"
    assert memories[0]["label"] == "frustrated"


def test_translation_uses_longest_observed_group_and_does_not_invent_progress() -> None:
    text, focus_id = build_big_whale_opening([
        _fact("short", 1, durationSeconds=600, project={"name": "短项目"}),
        _fact("long", 2, durationSeconds=5400, project={"name": "鲸鱼应用"}),
    ])
    assert focus_id == "long"
    assert "鲸鱼应用" in text
    assert "1 小时 30 分钟" in text
    assert "不知道进展" in text
    assert "完成了" not in text

    emotion_text, _ = build_big_whale_opening([{
        "id": "emotion-1", "revision": 3, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "occurredAt": "2026-09-03T03:00:00+00:00",
    }])
    assert emotion_text.startswith("我还记得你")
    assert "小鲸说" not in emotion_text


def test_grounded_reply_only_uses_stated_emotion_and_asks_when_unclear() -> None:
    neutral = build_grounded_companion_reply("今天开了三个小时的会", [])
    emotional = build_grounded_companion_reply(
        "今天开了三个小时的会，我好烦啊", [], emotion_label="frustrated"
    )
    remembered = build_grounded_companion_reply("你记得我今天做了什么吗", [_fact()])
    assert "会" in neutral
    assert "？" in neutral
    assert "好烦" not in neutral
    assert "烦" in emotional
    assert "会议本身" in emotional
    assert "小鲸" in remembered
    assert "完成了" not in remembered

    generic = build_grounded_companion_reply("我只是想说一件新事情", [{
        "id": "old-emotion", "revision": 4, "layer": "L3",
        "sourceType": "user_stated", "kind": "stated_emotion",
        "label": "frustrated", "occurredAt": "2026-09-02T03:00:00+00:00",
    }])
    assert "有点烦" not in generic

    stopped = build_grounded_companion_reply("算了，不想说了", [])
    assert "？" not in stopped
    assert "不问" in stopped or "不说" in stopped or "先放这儿" in stopped


def test_reply_is_atomic_idempotent_and_visible_in_history(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory.sqlite3")
    repository.ingest_batch(_batch())
    payload = {
        "userId": "user-1", "deviceId": "phone-1", "messageId": "reply-1",
        "role": "user", "text": "今天忙完了",
    }
    first = repository.append_message(payload)
    duplicate = repository.append_message(payload)
    assert first["assistantMessage"]["messageId"] == duplicate["assistantMessage"]["messageId"]
    assert duplicate["duplicate"] is True
    history = repository.messages("user-1")["messages"]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert len({message["messageId"] for message in history}) == 2


class _StubResponder:
    name = "测试模型"
    available = True

    def __init__(self, text: str = "模型记得这件事。") -> None:
        self.text = text
        self.calls = 0
        self.last_memories = []
        self.last_conversation = []

    def reply(self, memories: list[dict], conversation: list[dict]) -> str:
        self.calls += 1
        self.last_memories = memories
        self.last_conversation = conversation
        return self.text


class _FailingResponder(_StubResponder):
    def reply(self, memories: list[dict], conversation: list[dict]) -> str:
        self.calls += 1
        raise RuntimeError("provider unavailable")


def test_repository_uses_model_once_and_falls_back_safely(tmp_path: Path) -> None:
    responder = _StubResponder("我记得，小鲸记录的是一个小时。你想从哪段聊起？")
    repository = MemoryRepository(tmp_path / "model.sqlite3", responder=responder)
    repository.ingest_batch(_batch())
    payload = {
        "userId": "user-1", "deviceId": "phone-1", "messageId": "model-user-1",
        "role": "user", "text": "你记得我今天做了什么吗？",
    }
    first = repository.append_message(payload)
    duplicate = repository.append_message(payload)
    assert first["replySource"] == "model"
    assert first["assistantMessage"]["text"].startswith("我记得")
    assert duplicate["replySource"] == "stored"
    assert responder.calls == 1
    assert responder.last_memories[0]["id"] == "fact-1"
    assert responder.last_conversation[-1] == {"role": "user", "text": payload["text"]}
    assert repository.companion_status()["modelEnabled"] is True

    fallback = _FailingResponder()
    fallback_repository = MemoryRepository(tmp_path / "fallback.sqlite3", responder=fallback)
    fallback_repository.ingest_batch(_batch())
    result = fallback_repository.append_message({**payload, "messageId": "fallback-user-1"})
    assert result["replySource"] == "fallback"
    assert "小鲸" in result["assistantMessage"]["text"]
    assert fallback.calls == 1


def test_model_adapter_sends_grounded_context_to_openai_compatible_api(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({
                "choices": [{"message": {"content": "我知道的只有小鲸记下的这些。"}}]
            }, ensure_ascii=False).encode("utf-8")

    def fake_urlopen(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _Response()

    monkeypatch.setattr("pet.companion_llm.urllib.request.urlopen", fake_urlopen)
    config = ProviderConfig(
        "test", name="Mock", base_url="https://model.example/v1",
        model="mock-chat", api_key="server-secret", timeout=12,
    )
    responder = ModelCompanionResponder(config)
    result = responder.reply([_fact()], [{"role": "user", "text": "我今天做了什么？"}])
    request_body = json.loads(captured["request"].data.decode("utf-8"))
    assert result == "我知道的只有小鲸记下的这些。"
    assert request_body["messages"][-1]["content"] == "我今天做了什么？"
    assert "事实底线" in request_body["messages"][0]["content"]
    assert "最多问一个问题" in request_body["messages"][0]["content"]
    assert "Visual Studio Code" in request_body["messages"][0]["content"]
    assert captured["request"].get_header("Authorization") == "Bearer server-secret"
    assert captured["kwargs"]["timeout"] == 12


def test_model_context_exposes_only_memory_whitelist() -> None:
    context = json.loads(build_model_context([_fact(secretField="must-not-leak")]))
    encoded = json.dumps(context, ensure_ascii=False)
    assert "must-not-leak" not in encoded
    assert "Visual Studio Code" in encoded


def test_deepseek_companion_request_disables_thinking(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"choices":[{"message":{"content":"\u55ef\uff0c\u4f60\u7ee7\u7eed\u3002"}}]}'

    def fake_urlopen(request, **_kwargs):
        captured.update(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr("pet.companion_llm.urllib.request.urlopen", fake_urlopen)
    responder = ModelCompanionResponder(ProviderConfig(
        "deepseek", base_url="https://api.deepseek.com",
        model="deepseek-v4-flash", api_key="secret", max_tokens=2048,
    ))
    assert responder.reply([], [{"role": "user", "text": "今天事情很多"}])
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["max_tokens"] == 320


def test_model_fact_firewall_rejects_unstated_emotion_duration_and_progress() -> None:
    conversation = [{"role": "user", "text": "今天事情很多"}]
    memories = [_fact()]
    assert model_reply_is_grounded("我在听，你想先聊哪一件？", memories, conversation)
    assert not model_reply_is_grounded("听起来你今天很累。", memories, conversation)
    assert not model_reply_is_grounded("你今天忙了 8 小时。", memories, conversation)
    assert not model_reply_is_grounded("你的项目已经完成了。", memories, conversation)
    assert model_reply_is_grounded("项目做完了吗？", memories, conversation)


def test_http_client_auth_batch_stream_and_opening(tmp_path: Path) -> None:
    server = MemoryApiServer(
        MemoryRepository(tmp_path / "memory.sqlite3"), token="secret", port=0
    )
    port = server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{port}", "secret")
        ack = client.post_batch(_batch())
        assert ack["accepted"] is True
        assert client.stream("user-1")["memories"][0]["id"] == "fact-1"
        opening = client.claim_opening(
            user_id="user-1", device_id="phone-1", claim_id="claim-http-1"
        )
        assert opening["shouldSend"] is True
        reply = client.post_message(
            user_id="user-1", device_id="phone-1", message_id="reply-http-1",
            role="user", text="我忙完了",
        )
        assert reply["assistantMessage"]["role"] == "assistant"
        assert [row["role"] for row in client.messages("user-1")["messages"]] == [
            "assistant", "user", "assistant",
        ]

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            page = response.read().decode("utf-8")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/media/ojingjing.jpg", timeout=2
        ) as response:
            icon = response.read()
        assert "她记得白天发生的事" in page
        assert health["companion"]["modelEnabled"] is False
        assert len(icon) > 1000

        bad_client = MemorySyncClient(f"http://127.0.0.1:{port}", "wrong")
        with pytest.raises(SyncTransportError, match="401"):
            bad_client.stream("user-1")
    finally:
        server.stop()


def test_phone_http_request_reaches_openai_compatible_model(tmp_path: Path) -> None:
    captured = {}

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            captured["path"] = self.path
            captured["body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            body = json.dumps({
                "choices": [{"message": {"content": "我在听。你想先从哪件事说起？"}}]
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()
    config = ProviderConfig(
        "local-model", name="Local model",
        base_url=f"http://127.0.0.1:{model_server.server_address[1]}",
        model="test-chat", timeout=5,
    )
    memory_server = MemoryApiServer(
        MemoryRepository(
            tmp_path / "model-e2e.sqlite3",
            responder=ModelCompanionResponder(config),
        ),
        token="secret",
        port=0,
    )
    memory_port = memory_server.start()
    try:
        client = MemorySyncClient(f"http://127.0.0.1:{memory_port}", "secret")
        response = client.post_message(
            user_id="user-1", device_id="phone-1", message_id="model-e2e-user-1",
            role="user", text="今天有不少事情想说",
        )
        assert response["replySource"] == "model"
        assert response["assistantMessage"]["text"] == "我在听。你想先从哪件事说起？"
        assert captured["path"] == "/v1/chat/completions"
        assert captured["body"]["messages"][-1]["content"] == "今天有不少事情想说"
    finally:
        memory_server.stop()
        model_server.shutdown()
        model_server.server_close()
        model_thread.join(2)


class _Config:
    def __init__(self, url: str) -> None:
        self.data = {
            "memory_sync_enabled": True,
            "memory_sync_url": url,
            "memory_sync_token": "secret",
            "memory_sync_user_id": "user-1",
            "memory_sync_device_id": "desktop-1",
        }

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def save(self) -> None:
        return


def test_desktop_sync_manager_marks_ack_only_after_real_server_response(tmp_path: Path) -> None:
    server = MemoryApiServer(
        MemoryRepository(tmp_path / "server.sqlite3"), token="secret", port=0
    )
    port = server.start()
    try:
        store = SharedMemoryStore(tmp_path / "client.json")
        store.append_fact(_fact(memory_id="ignored-by-store", revision=999))
        report = store.pending_reports()[0]
        manager = MemorySyncManager(_Config(f"http://127.0.0.1:{port}"), store)
        manager._run(force=True)
        assert store.pending_reports() == []
        remote = server.repository.stream("user-1")["memories"]
        assert len(remote) == 1
        assert remote[0]["id"] == report["memoryIds"][0]
    finally:
        server.stop()


def test_failed_sync_keeps_batch_for_retry(tmp_path: Path) -> None:
    store = SharedMemoryStore(tmp_path / "client.json")
    store.append_fact(_fact())
    manager = MemorySyncManager(_Config("http://127.0.0.1:1"), store)
    manager._run(force=True)
    pending = store.pending_reports()
    assert len(pending) == 1
    assert pending[0]["status"] == "sent"
    assert pending[0]["attempts"] == 1
