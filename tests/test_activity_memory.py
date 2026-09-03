from __future__ import annotations

import json
from pathlib import Path

from pet.activity_memory import (
    ActivityCollector,
    ProjectEnricher,
    SharedMemoryStore,
    explicit_emotion_label,
)


def test_activity_segments_become_l1_memory_and_incremental_outbox(tmp_path: Path):
    project = tmp_path / "sample"
    project.mkdir()
    (project / "README.md").write_text(
        "# Sample Project\n\nA small desktop companion project.\n",
        encoding="utf-8",
    )
    store = SharedMemoryStore(tmp_path / "memory.json")
    collector = ActivityCollector(
        store,
        project_enricher=ProjectEnricher([tmp_path]),
        min_segment_seconds=20,
        checkpoint_seconds=30,
    )
    code = {
        "name": "Visual Studio Code",
        "bundle": "com.microsoft.VSCode",
        "title": "main.py — sample — Visual Studio Code",
    }
    collector.observe(code, "work", wall_now=100, mono_now=0)
    collector.observe(code, "work", wall_now=160, mono_now=60)
    committed = collector.observe({"name": "Zoom"}, "meeting", wall_now=220, mono_now=120)

    assert len(committed) == 1
    fact = committed[0]
    assert fact["layer"] == "L1"
    assert fact["sourceType"] == "observed"
    assert fact["durationSeconds"] == 120.0
    assert fact["project"]["name"] == "sample"
    assert "desktop companion" in fact["project"]["summary"]
    pending = store.pending_reports()
    assert len(pending) >= 1
    assert pending[0]["memoryIds"] == [fact["id"]]
    summary = next(iter(store.snapshot()["dailySummaries"].values()))
    assert summary["totalActiveSeconds"] == 120.0
    assert summary["byApp"][0] == {"name": "Visual Studio Code", "durationSeconds": 120.0}


def test_idle_time_is_not_counted_as_foreground_usage(tmp_path: Path):
    store = SharedMemoryStore(tmp_path / "memory.json")
    collector = ActivityCollector(store, min_segment_seconds=10, idle_seconds=180)
    collector.observe({"name": "Steam", "title": "Game"}, "gaming", wall_now=100, mono_now=0)
    facts = collector.observe(
        {"name": "Steam", "title": "Game"},
        "gaming",
        idle_for_seconds=200,
        wall_now=400,
        mono_now=300,
    )
    assert facts[0]["durationSeconds"] == 100.0


def test_long_activity_creates_neutral_l2_clue_not_emotion(tmp_path: Path):
    store = SharedMemoryStore(tmp_path / "memory.json")
    collector = ActivityCollector(store, min_segment_seconds=10)
    collector.observe({"name": "Cursor"}, "work", wall_now=100, mono_now=0)
    collector.observe({"name": "Finder"}, "idle", wall_now=2000, mono_now=1900)
    snap = store.snapshot()
    assert snap["clues"][0]["layer"] == "L2"
    assert snap["clues"][0]["kind"] == "long_continuous_activity"
    assert snap["emotions"] == []


def test_explicit_emotion_is_l3_and_idempotent_by_message(tmp_path: Path):
    assert explicit_emotion_label("今天开了三个小时的会，好烦啊") == "frustrated"
    assert explicit_emotion_label("今天开了三个小时的会") == ""
    assert explicit_emotion_label("客户今天很生气") == ""

    store = SharedMemoryStore(tmp_path / "memory.json")
    first, added = store.append_emotion("我今天好烦", "frustrated", "message-1")
    duplicate, added_again = store.append_emotion("我今天好烦", "frustrated", "message-1")
    assert added is True
    assert added_again is False
    assert duplicate["id"] == first["id"]
    assert first["layer"] == "L3"
    assert first["sourceType"] == "user_stated"


def test_report_ack_is_idempotent(tmp_path: Path):
    store = SharedMemoryStore(tmp_path / "memory.json")
    fact, _ = store.append_fact({
        "kind": "activity",
        "startedAt": "2026-09-02T01:00:00+00:00",
        "endedAt": "2026-09-02T02:00:00+00:00",
        "durationSeconds": 3600,
        "app": "Cursor",
        "title": "",
        "context": "work",
    })
    batch = store.pending_reports()[0]
    assert batch["memoryIds"] == [fact["id"]]
    assert store.acknowledge_report(batch["batchId"]) is True
    assert store.acknowledge_report(batch["batchId"]) is False
    assert store.pending_reports() == []


def test_open_segment_recovers_after_restart_without_duplicates(tmp_path: Path):
    path = tmp_path / "memory.json"
    store = SharedMemoryStore(path)
    store.set_open_segment({
        "key": ["cursor", "", "work", ""],
        "startedTs": 100,
        "lastSeenTs": 180,
        "app": "Cursor",
        "bundle": "",
        "title": "",
        "context": "work",
        "project": {},
    })
    ActivityCollector(store, min_segment_seconds=10)
    assert len(store.snapshot()["facts"]) == 1

    # 模拟旧 checkpoint 又出现：deterministic fact id 会拦住重复写入和重复报信。
    store.set_open_segment({
        "key": ["cursor", "", "work", ""],
        "startedTs": 100,
        "lastSeenTs": 180,
        "app": "Cursor",
        "bundle": "",
        "title": "",
        "context": "work",
        "project": {},
    })
    ActivityCollector(store, min_segment_seconds=10)
    snap = json.loads(path.read_text(encoding="utf-8"))
    assert len(snap["facts"]) == 1


def test_eight_hour_sampling_simulation_stays_bounded(tmp_path: Path):
    """按真实 2 秒采样频率推进 8 小时，验证分段数量不会随 tick 爆炸。"""
    store = SharedMemoryStore(tmp_path / "memory.json")
    collector = ActivityCollector(
        store,
        min_segment_seconds=20,
        checkpoint_seconds=10**9,
    )
    apps = [
        ({"name": "Cursor", "title": "main.py — whale — Cursor"}, "work"),
        ({"name": "Zoom", "title": "Daily Meeting"}, "meeting"),
        ({"name": "Safari", "title": "Documentation"}, "idle"),
    ]
    start = 1_800_000_000.0
    ticks = int(8 * 60 * 60 / 2)
    for index in range(ticks):
        # 每 30 分钟切换一次场景；采样本身不应产生一条条碎片事实。
        app, context = apps[(index // 900) % len(apps)]
        collector.observe(app, context, wall_now=start + index * 2, mono_now=index * 2)
    collector.flush(start + ticks * 2)
    snap = store.snapshot()
    assert len(snap["facts"]) <= 16
    assert len(snap["outbox"]) == len(snap["facts"]) + len(snap["clues"])
    assert sum(row["totalActiveSeconds"] for row in snap["dailySummaries"].values()) == 8 * 60 * 60
