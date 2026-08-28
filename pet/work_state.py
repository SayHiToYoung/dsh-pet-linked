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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

DEFAULT_PORT = int(__import__("os").environ.get("DSH_PET_STATE_PORT", "47890"))


class WorkStateServer:
    """收信标 POST /state，存当前工作状态，并在变化时回调。"""

    def __init__(self, on_change: Callable[[bool, str], None] | None = None,
                 on_usage: Callable[[dict], None] | None = None,
                 port: int = DEFAULT_PORT):
        self._port = port
        self._on_change = on_change
        self._on_usage = on_usage
        self._lock = threading.Lock()
        self.working = False
        self.detail = ""
        # Token 用量（信标按增量上报，此处累计到"本进程会话"；数据源为 DSH 会话）
        self.session_usage = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
        self.model = ""  # 最近一次上报携带的模型名（来自 DSH 事件流）
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # 信标诊断信息（页面加载的信标版本 / WebSocket 截获是否挂上）
        self.beacon_version: str = ""
        self.beacon_ws_tap: bool = False

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
                    changed = server.update(working, detail, beacon=beacon, ws_tap=ws_tap)
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
                self._send_json(404, {"ok": False, "error": "not found"})

            def do_GET(self):
                path = self.path.rstrip("/")
                if path == "/state":
                    self._send_json(200, server.snapshot())
                elif path == "/usage":
                    self._send_json(200, server.usage_snapshot())
                elif path in ("/health", ""):
                    self._send_json(200, {"ok": True, "port": server.port})
                else:
                    self._send_json(404, {"ok": False, "error": "not found"})

        return Handler

    def update(self, working: bool, detail: str, beacon: str = "", ws_tap: bool = False) -> bool:
        with self._lock:
            if beacon:
                self.beacon_version = beacon
            self.beacon_ws_tap = ws_tap
            if working == self.working and detail == self.detail:
                return False
            self.working = working
            self.detail = detail
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

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "working": self.working,
                "detail": self.detail,
                "beacon": self.beacon_version,
                "wsTap": self.beacon_ws_tap,
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
