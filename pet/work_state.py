# -*- coding: utf-8 -*-
"""
DSH 工作状态信标接收端 —— 二次开发新增模块。

页内信标（注入 DSH 前端的一个小脚本）会在 DSH 页面里实时观察
`data-state="ongoing"` 等"正在工作"信号，并把状态 POST 到本模块
开启的本地端口；桌宠据此切换动画（工作时写代码/吃Token，摸鱼时恢复）。

- 只监听 127.0.0.1，不对外网开放
- 端口默认 47890，可用环境变量 DSH_PET_STATE_PORT 覆盖
- 线程安全；状态变化时在主线程回调（由调用方负责跨线程投递）
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .companion_state import CompanionState, DESKTOP_SURFACE, connector_surface

DEFAULT_PORT = int(__import__("os").environ.get("DSH_PET_STATE_PORT", "47890"))
OFFICE_CONNECTOR_ID = "dsh-agent-office"


class WorkStateServer:
    """收信标 POST /state，存当前工作状态，并在变化时回调。"""

    def __init__(self, on_change: Callable[[bool, str], None] | None = None,
                 on_usage: Callable[[dict], None] | None = None,
                 on_emote: Callable[[str], None] | None = None,
                 on_root: Callable[[dict], None] | None = None,
                 on_handoff: Callable[[dict], None] | None = None,
                 on_companion_recovered: Callable[[dict], None] | None = None,
                 companion: CompanionState | None = None,
                 port: int = DEFAULT_PORT):
        self._port = port
        self._on_change = on_change
        self._on_usage = on_usage
        self._on_emote = on_emote
        # 联动办公区(dsh-agent-office):
        #   on_root    —— 办公区把「主控鲸」实时状态 + 审批数推来 → 桌宠镜像
        #   on_handoff —— 办公区触发「搬家」(主控鲸走出办公室来到桌面 / 回去)
        self._on_root = on_root
        self._on_handoff = on_handoff
        self._on_companion_recovered = on_companion_recovered
        self._lock = threading.Lock()
        # “同一个她”的唯一视觉归属。WorkStateServer 只做 Office 兼容适配，
        # 桌面/连接器归属、交接幂等和失联回退全部由 CompanionState 裁决。
        self._companion = companion or CompanionState()
        # 办公区面板在屏幕上的矩形 + 主控鲸 id(桌宠拖回办公区时判定用)
        self._office_rect: dict = {}
        self._office_root_id: str = ""
        # 桌宠被拖回办公区时的落点（屏幕坐标）——一次性下发给办公区，
        # 让主控鲸「落到桌宠拖放的位置」而不是从大门走进来。
        self._drop_screen: dict | None = None
        # 拖动实时状态（办公区 ghost 无感进入预览用）：桌宠拖动时上报屏幕坐标 +
        # 是否压在办公区面板上（over）；办公区快轮询 /office/dragstate 据此渲染 ghost。
        self._drag: dict = {"active": False, "x": 0.0, "y": 0.0, "over": False}
        # office 定期推送心跳；活跃时旧 DOM 信标只记录、不再抢写桌宠状态。
        self._office_last_seen: float = 0.0
        self._beacon_callback_suppressed: bool = False
        self.working = False
        self.detail = ""
        # Token 用量（信标按增量上报，此处累计到"本进程会话"；数据源为 DSH 会话）
        self.session_usage = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
        self.model = ""  # 最近一次上报携带的模型名（来自 DSH 事件流）
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 信标诊断信息（页面加载的信标版本 / WebSocket 截获是否挂上 / 截获计数）
        self.beacon_version: str = ""
        self.beacon_ws_tap: bool = False
        self.beacon_diag: dict = {}

    @property
    def port(self) -> int:
        return self._port

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            # 静默访问日志，避免刷屏
            def log_message(self, *args):
                pass

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def do_POST(self):
                path = self.path.rstrip("/")
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                if path == "/state":
                    working = bool(payload.get("working", False))
                    detail = str(payload.get("detail", ""))[:80]
                    beacon = str(payload.get("beacon") or "").strip()[:40]
                    ws_tap = bool(payload.get("wsTap", False))
                    diag = payload.get("diag") if isinstance(payload.get("diag"), dict) else {}
                    changed = server.update(working, detail, beacon=beacon, ws_tap=ws_tap, diag=diag)
                    self._send_json(200, {"ok": True, "working": working, "detail": detail, "changed": changed})
                    return
                if path == "/usage":
                    added = server.add_usage(
                        int(payload.get("inputTokens") or 0),
                        int(payload.get("outputTokens") or 0),
                        int(payload.get("cacheReadTokens") or 0),
                        int(payload.get("reasoningTokens") or 0),
                        str(payload.get("model") or ""),
                    )
                    self._send_json(200, {"ok": True, "added": added, "usage": server.usage_snapshot()})
                    return
                if path == "/emote":
                    text = str(payload.get("text") or "")[:500]
                    server.notify_emote(text)
                    self._send_json(200, {"ok": True, "received": bool(text)})
                    return
                if path == "/office/root":
                    # 办公区推来的「主控鲸」实时状态(+ 审批数)→ 桌宠镜像
                    server.notify_root(payload)
                    self._send_json(200, {"ok": True, **server.companion_snapshot()})
                    return
                if path == "/office/handoff":
                    # 搬家:{dir:"to_desktop"|"to_office", agentId, fromScreen:{x,y}, label, model}
                    info = server.handle_handoff(payload)
                    self._send_json(200, {
                        "ok": True,
                        "desktop": info,
                        "agents": info,
                        **server.companion_snapshot(),
                    })
                    return
                if path == "/office/sync":
                    # 办公区每轮同步:上报面板屏幕矩形 + 主控鲸 id;回「谁在桌面」+ 一次性落点
                    resp = server.sync_office(payload)
                    drop = server.consume_drop_screen()
                    if drop:
                        resp["drop"] = drop
                    self._send_json(200, resp)
                    return
                self._send_json(404, {"ok": False, "error": "not found"})

            def do_GET(self):
                path = self.path.rstrip("/")
                if path == "/state":
                    self._send_json(200, server.snapshot())
                elif path == "/usage":
                    self._send_json(200, server.usage_snapshot())
                elif path == "/office/desktop":
                    self._send_json(200, {
                        "agents": server.desktop_list(),
                        **server.companion_snapshot(),
                    })
                elif path == "/companion/state":
                    self._send_json(200, server.companion_snapshot())
                elif path == "/office/dragstate":
                    # 办公区 ghost 无感进入:桌宠拖动实时位置 + 是否压在面板上
                    self._send_json(200, server.drag_state())
                elif path in ("/health", ""):
                    self._send_json(200, {
                        "ok": True,
                        "port": server.port,
                        **server.companion_snapshot(),
                    })
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})

        return Handler

    def update(self, working: bool, detail: str, beacon: str = "", ws_tap: bool = False,
               diag: dict | None = None) -> bool:
        with self._lock:
            if beacon:
                self.beacon_version = beacon
            self.beacon_ws_tap = ws_tap
            if diag:
                self.beacon_diag = {
                    k: int(v) if isinstance(v, (int, float)) else v
                    for k, v in diag.items()
                }
            office_active = time.monotonic() - self._office_last_seen < 7.0
            state_changed = working != self.working
            needs_takeover = self._beacon_callback_suppressed and not office_active
            self.working = working
            self.detail = detail
            if office_active:
                self._beacon_callback_suppressed = True
                cb = None
            elif not state_changed and not needs_takeover:
                # 同一轮里 DSH 可能反复更新 thinking/tool detail；它们只是进度，
                # 不是新的“开工”。仍保存最新 detail，但不重播桌宠入场动画。
                return False
            else:
                self._beacon_callback_suppressed = False
                cb = self._on_change
        if cb is not None:
            try:
                cb(working, detail)
            except Exception:
                logging.exception("work_state 回调失败")
        return True

    def add_usage(self, input_t: int, output_t: int, cache_read: int = 0,
                  reasoning: int = 0, model: str = "") -> dict:
        """累加信标上报的 token 增量（负值/0 忽略）；记录模型名。"""
        added = {
            "input": max(0, int(input_t or 0)),
            "output": max(0, int(output_t or 0)),
            "cacheRead": max(0, int(cache_read or 0)),
            "reasoning": max(0, int(reasoning or 0)),
        }
        with self._lock:
            if isinstance(model, str) and model.strip():
                self.model = model.strip()[:120]
            for key, value in added.items():
                self.session_usage[key] += value
            cb = self._on_usage
        if cb is not None and any(added.values()):
            try:
                cb({**added, "model": self.model})
            except Exception:
                logging.exception("work_state usage 回调失败")
        return added

    def usage_snapshot(self) -> dict:
        with self._lock:
            return {**dict(self.session_usage), "model": self.model}

    def notify_emote(self, text: str) -> None:
        """页内信标实时上报的用户消息 → 触发情绪响应回调。"""
        text = (text or "").strip()
        if not text:
            return
        cb = self._on_emote
        if cb is not None:
            try:
                cb(text)
            except Exception:
                logging.exception("work_state emote 回调失败")

    # ---- 办公区联动(dsh-agent-office → 桌宠)----
    @staticmethod
    def _connector_meta(payload: dict | None) -> tuple[str, str]:
        data = payload if isinstance(payload, dict) else {}
        connector_id = str(data.get("connectorId") or OFFICE_CONNECTOR_ID).strip()[:80]
        lease_id = str(data.get("leaseId") or "legacy-office").strip()[:120]
        return connector_id or OFFICE_CONNECTOR_ID, lease_id or "legacy-office"

    def companion_snapshot(self) -> dict:
        return self._companion.snapshot()

    def sync_office(self, payload: dict | None) -> dict:
        """Office 心跳 + 面板信息同步，返回统一视觉归属快照。"""
        data = dict(payload) if isinstance(payload, dict) else {}
        connector_id, lease_id = self._connector_meta(data)
        self._companion.heartbeat(connector_id, lease_id)
        self.set_office_rect(data.get("panelRect"), str(data.get("rootId") or ""))
        return {
            "agents": self.desktop_list(),
            **self._companion.snapshot(),
        }

    def notify_root(self, payload: dict) -> None:
        """办公区推来的「主控鲸」实时状态 → 触发镜像回调(主线程里应用)。"""
        cb = self._on_root
        if cb is None:
            return
        try:
            connector_id, lease_id = self._connector_meta(payload)
            self._companion.heartbeat(connector_id, lease_id)
            with self._lock:
                self._office_last_seen = time.monotonic()
            cb(dict(payload) if isinstance(payload, dict) else {})
        except Exception:
            logging.exception("work_state root 回调失败")

    def handle_handoff(self, payload: dict) -> list[str]:
        """搬家:更新「在桌面」归属集合并回调播放动画。返回当前桌面集合。

        注意:to_office 时不立即清空归属集——桌宠要走回门口并淡出后,由
        `set_on_desktop(False)`(到位回调里调)再清空,办公区才重新渲染主控鲸。
        若这里抢先清空,办公区下一次 petSync 会立刻重画鲸,与还在走回的桌宠重叠。
        """
        p = dict(payload) if isinstance(payload, dict) else {}
        agent_id = str(p.get("agentId") or p.get("id") or "root")
        direction = str(p.get("dir") or "to_desktop")
        connector_id, lease_id = self._connector_meta(p)
        self._companion.heartbeat(connector_id, lease_id)
        handoff_id = str(p.get("handoffId") or uuid.uuid4().hex)[:160]
        p["handoffId"] = handoff_id
        p["connectorId"] = connector_id

        target = DESKTOP_SURFACE if direction == "to_desktop" else connector_surface(connector_id)
        _state, accepted = self._companion.request_handoff(
            handoff_id,
            target,
            connector_id=connector_id,
        )
        if accepted and direction == "to_desktop":
            # Office 已经走到大门才发请求，可以立即把唯一渲染权交给桌面。
            self._companion.commit_handoff(handoff_id)
        p["accepted"] = accepted
        snap = self.desktop_list()
        cb = self._on_handoff
        # 重复 handoffId 不重播动画，保证网络重试幂等。
        if accepted and cb is not None:
            try:
                cb(p)
            except Exception:
                logging.exception("work_state handoff 回调失败")
        return snap

    def desktop_list(self) -> list[str]:
        with self._lock:
            root_id = self._office_root_id
        return [root_id] if root_id and self._companion.is_desktop() else []

    def set_office_rect(self, rect, root_id: str = "") -> None:
        with self._lock:
            if isinstance(rect, dict):
                self._office_rect = {k: rect.get(k) for k in ("left", "top", "right", "bottom")}
            if root_id and root_id != self._office_root_id:
                self._office_root_id = root_id
            elif root_id:
                self._office_root_id = root_id

    def office_rect(self) -> dict:
        with self._lock:
            return dict(self._office_rect)

    def office_root_id(self) -> str:
        with self._lock:
            return self._office_root_id

    def begin_to_office(self, agent_id: str, handoff_id: str = "") -> str:
        """桌宠拖入 Office 时主动创建一笔交接，返回可用于到位提交的 id。"""
        clean_id = str(handoff_id or uuid.uuid4().hex)[:160]
        self._companion.heartbeat(OFFICE_CONNECTOR_ID, "legacy-office")
        _snap, accepted = self._companion.request_handoff(
            clean_id,
            connector_surface(OFFICE_CONNECTOR_ID),
            connector_id=OFFICE_CONNECTOR_ID,
        )
        return clean_id if accepted else ""

    def set_on_desktop(self, agent_id: str, on: bool, handoff_id: str = "") -> list[str]:
        """桌宠端改「谁在桌面」(拖回办公区 → on=False);返回当前集合。"""
        if on:
            self._companion.force_surface(DESKTOP_SURFACE, reason="desktop_claimed")
        elif handoff_id:
            self._companion.commit_handoff(handoff_id)
        else:
            # 兼容旧调用与既有单测；没有交接 id 时直接归属当前 Office。
            self._companion.force_surface(
                connector_surface(OFFICE_CONNECTOR_ID),
                connector_id=OFFICE_CONNECTOR_ID,
                reason="legacy_office_claimed",
            )
        return self.desktop_list()

    def expire_companion(self) -> dict | None:
        """由主线程定时调用；Office 失联或交接卡住时把鲸鱼娘收回桌面。"""
        event = self._companion.expire()
        if not event:
            return None
        if event.get("recoveredToDesktop"):
            with self._lock:
                # 面板已经不可信，避免桌宠被拖进一个不存在的 Office 矩形。
                self._office_rect = {}
            cb = self._on_companion_recovered
            if cb is not None:
                try:
                    cb(dict(event))
                except Exception:
                    logging.exception("companion recovery 回调失败")
        return event

    def set_drag_state(self, active: bool, x: float = 0.0, y: float = 0.0, over: bool = False) -> None:
        """桌宠拖动中上报:实时屏幕坐标 + 是否压在办公区面板上(over)。"""
        with self._lock:
            self._drag = {"active": bool(active), "x": float(x), "y": float(y), "over": bool(over)}

    def drag_state(self) -> dict:
        with self._lock:
            return dict(self._drag)

    def note_drop_screen(self, x: float, y: float) -> None:
        """桌宠被拖回办公区时，记录其屏幕落点（供办公区换算成场景坐标）。"""
        with self._lock:
            self._drop_screen = {"x": float(x), "y": float(y)}

    def consume_drop_screen(self) -> dict | None:
        """取走一次性落点（办公区每次 /office/sync 消费一次）。"""
        with self._lock:
            drop = self._drop_screen
            self._drop_screen = None
            return drop

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "working": self.working,
                "detail": self.detail,
                "beacon": self.beacon_version,
                "wsTap": self.beacon_ws_tap,
                "diag": dict(self.beacon_diag),
            }

    def start(self) -> bool:
        if self._server is not None:
            return True
        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self._port), self._make_handler())
            self._thread = threading.Thread(
                target=self._server.serve_forever, daemon=True, name="dsh-work-state"
            )
            self._thread.start()
            logging.info("work_state 信标监听 http://127.0.0.1:%d", self._port)
            return True
        except OSError:
            logging.exception("work_state 端口 %d 监听失败（可能被占用）", self._port)
            return False

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            self._thread = None
