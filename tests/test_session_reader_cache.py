from __future__ import annotations

import json
from pathlib import Path

import zstandard

from pet import session_reader


def _append_frame(path: Path, *events: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    with path.open("ab") as handle:
        handle.write(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))


def _usage_event(seq: int, input_tokens: int) -> dict:
    return {
        "type": "assistant/message",
        "time": 1_700_000_000_000 + seq,
        "seq": seq,
        "data": {
            "turn": seq,
            "step": 0,
            "usage": {
                "inputTokens": input_tokens,
                "outputTokens": 2,
                "cacheReadTokens": 3,
                "reasoningTokens": 4,
            },
            "message": {"source": {"model": "deepseek-v4-flash"}},
        },
    }


def _user_event(seq: int, text: str) -> dict:
    return {
        "type": "user/message",
        "time": 1_700_000_000_000 + seq,
        "seq": seq,
        "data": {"turn": seq, "step": 0, "content": text},
    }


def test_streaming_usage_cache_invalidates_when_file_grows(tmp_path: Path) -> None:
    path = tmp_path / "session-one" / "session.jsonl.zstd"
    _append_frame(path, _usage_event(1, 10))

    _, first, _, _ = session_reader.read_session_usage(path)
    _, cached, _, _ = session_reader.read_session_usage(path)
    assert first == cached
    assert first["input"] == 10

    _append_frame(path, _usage_event(2, 20))
    _, updated, _, _ = session_reader.read_session_usage(path)
    assert updated["input"] == 30


def test_latest_user_cache_tracks_new_appended_frame(tmp_path: Path) -> None:
    path = tmp_path / "workspace" / "session-one" / "session.jsonl.zstd"
    _append_frame(path, _user_event(1, "今天先做桌宠"))

    assert session_reader.latest_user_message_global(tmp_path)[2] == "今天先做桌宠"
    assert session_reader.latest_user_message_global(tmp_path)[2] == "今天先做桌宠"

    _append_frame(path, _user_event(2, "现在继续完善记忆"))
    sid, fingerprint, text = session_reader.latest_user_message_global(tmp_path)
    assert sid == "session-one"
    assert fingerprint.endswith(":2")
    assert text == "现在继续完善记忆"


def test_aggregate_result_cache_preserves_cross_file_deduplication(tmp_path: Path) -> None:
    first = tmp_path / "workspace" / "session-one" / "session.jsonl.zstd"
    second = tmp_path / "workspace" / "session-two" / "session.jsonl.zstd"
    duplicate = _usage_event(1, 10)
    _append_frame(first, duplicate)
    _append_frame(second, duplicate)

    totals, _ = session_reader.aggregate_all_sessions(tmp_path)
    cached_totals, _ = session_reader.aggregate_all_sessions(tmp_path)
    assert totals == cached_totals
    assert totals == {"input": 10, "output": 2, "cacheRead": 3, "reasoning": 4}

    _append_frame(second, _usage_event(2, 7))
    updated, _ = session_reader.aggregate_all_sessions(tmp_path)
    assert updated["input"] == 17
