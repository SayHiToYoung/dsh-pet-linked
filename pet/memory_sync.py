# -*- coding: utf-8 -*-
"""桌宠端 outbox 同步器：顺序上传、断线重试、真实 ACK 后落盘。"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal

from .memory_protocol import MemorySyncClient, make_batch


class MemorySyncManager(QObject):
    delivered = Signal(int)
    failed = Signal(str)

    def __init__(self, config, store, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.store = store
        self._busy = False
        self._lock = threading.Lock()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.trigger)

    def enabled(self) -> bool:
        return bool(self.config.get("memory_sync_enabled", False))

    def start(self) -> None:
        interval = max(10, min(3600, int(self.config.get("memory_sync_interval_seconds", 30) or 30)))
        self._timer.setInterval(interval * 1000)
        if self.enabled():
            self._timer.start()
            QTimer.singleShot(1200, self.trigger)
        else:
            self._timer.stop()

    def apply_config(self) -> None:
        self._timer.stop()
        self.start()

    def stop(self) -> None:
        self._timer.stop()

    def _device_id(self) -> str:
        value = str(self.config.get("memory_sync_device_id", "") or "").strip()
        if value:
            return value
        value = f"desktop-{uuid.uuid4().hex}"
        self.config.set("memory_sync_device_id", value)
        self.config.save()
        return value

    @staticmethod
    def _retry_due(report: dict, now: float) -> bool:
        attempts = max(0, int(report.get("attempts") or 0))
        if attempts <= 0:
            return True
        raw = str(report.get("sentAt") or "")
        try:
            sent_at = datetime.fromisoformat(raw).timestamp()
        except (TypeError, ValueError):
            return True
        delay = min(300.0, 5.0 * (2 ** min(6, attempts - 1)))
        return now - sent_at >= delay

    def trigger(self, force: bool = False) -> bool:
        if not self.enabled():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(
            target=self._run,
            args=(bool(force),),
            daemon=True,
            name="pet-memory-sync",
        ).start()
        return True

    def _run(self, force: bool) -> None:
        delivered = 0
        try:
            client = MemorySyncClient(
                str(self.config.get("memory_sync_url", "http://127.0.0.1:47821") or ""),
                str(self.config.get("memory_sync_token", "") or ""),
            )
            user_id = str(self.config.get("memory_sync_user_id", "local-user") or "local-user").strip()
            device_id = self._device_id()
            now = time.time()
            for report in self.store.pending_reports(limit=20):
                if not force and not self._retry_due(report, now):
                    continue
                memories = self.store.memories_for_ids(report.get("memoryIds") or [])
                if len(memories) != len(report.get("memoryIds") or []):
                    logging.error("记忆批次 %s 缺少本地内容，保留待人工检查", report.get("batchId"))
                    continue
                batch = make_batch(
                    user_id=user_id,
                    device_id=device_id,
                    report=report,
                    memories=memories,
                )
                self.store.mark_report_sent(batch["batchId"])
                response = client.post_batch(batch)
                if response.get("batchId") != batch["batchId"]:
                    raise RuntimeError("memory ACK batch mismatch")
                if self.store.acknowledge_report(batch["batchId"]):
                    delivered += 1
            if delivered:
                self.delivered.emit(delivered)
        except Exception as exc:
            logging.warning("记忆同步暂未完成: %s", exc)
            self.failed.emit(str(exc))
        finally:
            with self._lock:
                self._busy = False
