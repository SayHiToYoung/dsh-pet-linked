# -*- coding: utf-8 -*-
"""“同一个她”的最小状态核心。

本模块只负责鲸鱼娘的视觉归属，不负责动画、聊天或 Agent 状态：

- 桌面与连接器场景同一时刻只有一个拥有渲染权；
- 交接请求有 handoff_id，可安全重试；
- 连接器必须持续续租，失联后自动把渲染权还给桌面；
- 运行时归属不跨进程恢复，桌宠每次启动都从桌面安全态开始。

纯 Python、线程安全、时钟可注入，便于不启动 Qt 的单元测试。
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Callable


PROTOCOL_VERSION = 1
DEFAULT_PET_ID = "shenshen"
DESKTOP_SURFACE = "desktop"
STABLE = "stable"
TO_DESKTOP = "to_desktop"
TO_CONNECTOR = "to_connector"


def connector_surface(connector_id: str) -> str:
    connector_id = str(connector_id or "").strip()
    return f"connector:{connector_id}" if connector_id else DESKTOP_SURFACE


class CompanionState:
    """鲸鱼娘在不同渲染表面之间的唯一归属状态。"""

    def __init__(
        self,
        *,
        pet_id: str = DEFAULT_PET_ID,
        lease_timeout: float = 45.0,
        handoff_timeout: float = 5.0,
        clock: Callable[[], float] | None = None,
        instance_id: str | None = None,
    ) -> None:
        self.pet_id = str(pet_id or DEFAULT_PET_ID)
        self.instance_id = str(instance_id or uuid.uuid4().hex)
        # 租约超时 = 「多久没听到连接器的心跳就把她收回桌面」。
        # 事故(2026-09-03):原值 8s。Office 的心跳挂在渲染进程的 setTimeout 链上,
        # Electron 窗口进后台会把它压到分钟级 → 8 秒必然过期 → 桌宠单方面收回,
        # 而 Office 那边她还画着 → 桌面和办公区各一只。
        # 两件事一起改才治得住:
        #   · 这里把窗口拉长到 45s,让「一次后台/一次卡顿」不再等于「连接器没了」;
        #   · Office 侧在面板不可见时【主动】把她交给桌宠(见 office.js syncPanelVisibility),
        #     所以正常情况下根本走不到这条超时。这条只剩「Office 面板被关掉」的兜底。
        self.lease_timeout = max(1.0, float(lease_timeout))
        # 现有最远距离交接动画可接近 3 秒，给动画和一次事件循环回调留足余量。
        self.handoff_timeout = max(3.0, float(handoff_timeout))
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()

        # 运行时归属永远从桌面开始，不能恢复上次崩溃前的连接器归属。
        self._active_surface = DESKTOP_SURFACE
        self._revision = 0

        self._transition = STABLE
        self._handoff_id = ""
        self._handoff_source = DESKTOP_SURFACE
        self._handoff_target = DESKTOP_SURFACE
        self._handoff_deadline = 0.0
        self._recent_handoff_ids: deque[str] = deque(maxlen=128)
        self._recent_handoff_set: set[str] = set()

        # 为以后多个连接器留出空间；当前实际只接 dsh-agent-office。
        self._leases: dict[str, tuple[str, float]] = {}
        self._last_reason = "startup"

    @staticmethod
    def _clean_connector_id(value: str) -> str:
        return str(value or "").strip()[:80]

    def _now(self, now: float | None = None) -> float:
        return self._clock() if now is None else float(now)

    def _bump(self, reason: str) -> None:
        self._revision += 1
        self._last_reason = str(reason or "state_changed")[:120]

    def _lease_valid(self, connector_id: str, now: float) -> bool:
        lease = self._leases.get(connector_id)
        return bool(lease and lease[1] > now)

    def _remember_handoff(self, handoff_id: str) -> None:
        if not handoff_id or handoff_id in self._recent_handoff_set:
            return
        if len(self._recent_handoff_ids) == self._recent_handoff_ids.maxlen:
            oldest = self._recent_handoff_ids.popleft()
            self._recent_handoff_set.discard(oldest)
        self._recent_handoff_ids.append(handoff_id)
        self._recent_handoff_set.add(handoff_id)

    def _clear_transition(self, *, remember: bool = True) -> None:
        if remember and self._handoff_id:
            self._remember_handoff(self._handoff_id)
        self._transition = STABLE
        self._handoff_id = ""
        self._handoff_source = self._active_surface
        self._handoff_target = self._active_surface
        self._handoff_deadline = 0.0

    def _snapshot_locked(self, now: float) -> dict:
        active_connector = ""
        if self._active_surface.startswith("connector:"):
            active_connector = self._active_surface.split(":", 1)[1]
        lease_remaining = 0.0
        if active_connector:
            lease = self._leases.get(active_connector)
            if lease:
                lease_remaining = max(0.0, lease[1] - now)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "petId": self.pet_id,
            "instanceId": self.instance_id,
            "activeSurface": self._active_surface,
            "connectorId": active_connector,
            "revision": self._revision,
            "transition": self._transition,
            "handoffId": self._handoff_id,
            "handoffTarget": self._handoff_target if self._transition != STABLE else "",
            "leaseExpiresInMs": int(round(lease_remaining * 1000)),
            "leaseTimeoutMs": int(round(self.lease_timeout * 1000)),
            "reason": self._last_reason,
        }

    def snapshot(self, now: float | None = None) -> dict:
        with self._lock:
            return self._snapshot_locked(self._now(now))

    def is_desktop(self) -> bool:
        with self._lock:
            return self._active_surface == DESKTOP_SURFACE

    def heartbeat(
        self,
        connector_id: str,
        lease_id: str = "",
        *,
        now: float | None = None,
    ) -> dict:
        """续租连接器。心跳不增加 revision，避免每次轮询都制造状态变化。"""
        connector_id = self._clean_connector_id(connector_id)
        if not connector_id:
            return self.snapshot(now)
        current = self._now(now)
        cleaned_lease = str(lease_id or "legacy")[:120]
        with self._lock:
            self._leases[connector_id] = (cleaned_lease, current + self.lease_timeout)
            return self._snapshot_locked(current)

    def request_handoff(
        self,
        handoff_id: str,
        target_surface: str,
        *,
        connector_id: str = "",
        now: float | None = None,
    ) -> tuple[dict, bool]:
        """开始一次交接；返回 ``(快照, 是否为新的有效请求)``。"""
        current = self._now(now)
        connector_id = self._clean_connector_id(connector_id)
        target = str(target_surface or DESKTOP_SURFACE).strip()
        if target != DESKTOP_SURFACE:
            target = connector_surface(connector_id or target.removeprefix("connector:"))
        clean_id = str(handoff_id or uuid.uuid4().hex)[:160]

        with self._lock:
            # 网络重试或动画重复回调：同一交接只处理一次。
            if clean_id == self._handoff_id or clean_id in self._recent_handoff_set:
                return self._snapshot_locked(current), False
            # 不允许另一笔交接抢占正在进行的动画。
            if self._transition != STABLE:
                return self._snapshot_locked(current), False
            if target == self._active_surface:
                self._remember_handoff(clean_id)
                return self._snapshot_locked(current), False
            if target.startswith("connector:"):
                target_connector = target.split(":", 1)[1]
                if not self._lease_valid(target_connector, current):
                    return self._snapshot_locked(current), False

            self._transition = TO_DESKTOP if target == DESKTOP_SURFACE else TO_CONNECTOR
            self._handoff_id = clean_id
            self._handoff_source = self._active_surface
            self._handoff_target = target
            self._handoff_deadline = current + self.handoff_timeout
            self._bump("handoff_requested")
            return self._snapshot_locked(current), True

    def commit_handoff(self, handoff_id: str, *, now: float | None = None) -> tuple[dict, bool]:
        """提交当前交接。旧 id、重复 id 或租约失效都不会改变归属。"""
        current = self._now(now)
        clean_id = str(handoff_id or "")[:160]
        with self._lock:
            if self._transition == STABLE or not clean_id or clean_id != self._handoff_id:
                return self._snapshot_locked(current), False
            if self._handoff_target.startswith("connector:"):
                connector_id = self._handoff_target.split(":", 1)[1]
                if not self._lease_valid(connector_id, current):
                    self._active_surface = DESKTOP_SURFACE
                    self._clear_transition()
                    self._bump("handoff_lease_expired")
                    return self._snapshot_locked(current), False
            self._active_surface = self._handoff_target
            self._clear_transition()
            self._bump("handoff_committed")
            return self._snapshot_locked(current), True

    def force_surface(
        self,
        target_surface: str,
        *,
        connector_id: str = "",
        reason: str = "legacy_surface_change",
        now: float | None = None,
    ) -> dict:
        """兼容旧接口的直接切换；新交接应优先走 request/commit。"""
        current = self._now(now)
        connector_id = self._clean_connector_id(connector_id)
        if target_surface == DESKTOP_SURFACE:
            target = DESKTOP_SURFACE
        else:
            connector_id = connector_id or self._clean_connector_id(
                str(target_surface or "").removeprefix("connector:")
            )
            target = connector_surface(connector_id)
        with self._lock:
            if target.startswith("connector:") and not self._lease_valid(connector_id, current):
                # 给旧 Office 一次完整租约窗口，随后仍会被超时机制收回桌面。
                self._leases[connector_id] = ("legacy", current + self.lease_timeout)
            changed = target != self._active_surface or self._transition != STABLE
            self._active_surface = target
            self._clear_transition()
            if changed:
                self._bump(reason)
            return self._snapshot_locked(current)

    def expire(self, *, now: float | None = None) -> dict | None:
        """处理交接超时和连接器失联；无变化返回 ``None``。"""
        current = self._now(now)
        with self._lock:
            if self._transition != STABLE and current >= self._handoff_deadline:
                self._active_surface = self._handoff_source
                recovered = self._active_surface == DESKTOP_SURFACE
                self._clear_transition()
                self._bump("handoff_timeout")
                return {
                    "reason": "handoff_timeout",
                    "recoveredToDesktop": recovered,
                    "state": self._snapshot_locked(current),
                }

            if self._active_surface.startswith("connector:"):
                connector_id = self._active_surface.split(":", 1)[1]
                if not self._lease_valid(connector_id, current):
                    self._active_surface = DESKTOP_SURFACE
                    self._clear_transition()
                    self._bump("connector_lease_expired")
                    return {
                        "reason": "connector_lease_expired",
                        "recoveredToDesktop": True,
                        "state": self._snapshot_locked(current),
                    }
        return None
