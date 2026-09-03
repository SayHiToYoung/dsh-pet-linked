# -*- coding: utf-8 -*-
"""
应用入口 —— QApplication + 桌宠窗口 + 系统托盘。

支持运行时切换角色：
- 右键桌宠 →「切换角色」
- 托盘菜单 →「切换角色」
切换后会热加载对应形象的 webm，并保留位置/朝向等配置。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import autostart as autostart_mod
from . import balance as balance_mod
from . import catalog
from . import session_reader
from . import updater
from .config import APP_DIR_NAME, Config
from .harness_launcher import launch_harness_gui
from .instance_launcher import launch_new_pet
from .proactive_care import ProactiveCare
from .meeting_care import MeetingCare
from .library import MovieLibrary
from .window import PetWindow
from .work_state import WorkStateServer
from .context_aware import ContextAwareMonitor, detect_foreground_app
from .activity_memory import (
    ActivityCollector,
    ProjectEnricher,
    SharedMemoryStore,
    detect_system_idle_seconds,
    explicit_emotion_label,
)
from .memory_sync import MemorySyncManager
from . import emotion_actor
from .fun_image_popup import restore_ojingjing_windows
from .runtime_cleanup import cleanup_stale_runtime_dirs


class _BackgroundResult(QObject):
    done = Signal(bool, object)


class _BalanceBridge(_BackgroundResult):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.done.connect(self._show)

    def _show(self, _ok: bool, message) -> None:
        if self.win is not None:
            self.win.show_bubble(str(message), duration_ms=6000)


class _UpdateBridge(_BackgroundResult):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.done.connect(self._show)

    def _show(self, ok: bool, payload) -> None:
        if not ok:
            if self.parent is not None:
                self.parent.show_bubble(f"检查更新失败：{payload}", duration_ms=7000)
            return
        release = payload
        tag = str(release.get("version", ""))
        if not updater.is_newer(tag):
            if self.parent is not None:
                self.parent.show_bubble(f"已经是最新版本（{updater.APP_VERSION}）啦")
            return
        if self.parent is not None:
            self.parent.show_bubble(
                f"发现新版本 v{tag}（当前 {updater.APP_VERSION}）。"
                "可从“更新与帮助”打开项目页下载。",
                duration_ms=9000,
            )


class _WorkStateBridge(QObject):
    """把后台线程（信标 HTTP / 会话日志轮询）的结果安全投递到 Qt 主线程。"""

    changed = Signal(bool, str)
    # session_id, current_totals, total_totals, model, current_model, total_model
    ledger_changed = Signal(str, object, object, str, object, object)
    emotion_action = Signal(str)
    care_line = Signal(str)  # 主动关怀台词 → 主线程气泡
    office_root = Signal(object)  # 办公区推来的「主控鲸」状态 → 桌宠镜像
    handoff = Signal(object)      # 办公区触发「搬家」→ 走入/走回动画
    companion_recovered = Signal(object)  # Office 失联/交接超时 → 桌面安全恢复
    context = Signal(str, object) # 情境感知：前台应用 → 桌宠行为（会议躲起/游戏安静）
    memory_notice = Signal(str)   # 本地记忆/报信状态 → 低打扰气泡

    def __init__(self, controller) -> None:
        super().__init__()
        self.controller = controller
        self.changed.connect(self._apply)
        self.ledger_changed.connect(self._apply_ledger)
        self.emotion_action.connect(self._apply_emotion_action)
        self.care_line.connect(self._apply_care_line)
        self.office_root.connect(self._apply_office_root)
        self.handoff.connect(self._apply_handoff)
        self.companion_recovered.connect(self._apply_companion_recovered)
        self.context.connect(self._apply_context)
        self.memory_notice.connect(self._apply_memory_notice)

    def _apply(self, working: bool, detail: str) -> None:
        win = self.controller.win
        if win is not None:
            try:
                win.set_work_state(working, detail)
            except Exception:
                logging.exception("work_state 应用失败")

    def _apply_ledger(self, session_id: str, current_totals: dict, total_totals: dict, model: str,
                      current_model: dict, total_model: dict) -> None:
        win = self.controller.win
        if win is not None:
            try:
                win.update_ledger(session_id, current_totals, total_totals, model,
                                  current_model, total_model)
            except Exception:
                logging.exception("token ledger 应用失败")

    def _apply_emotion_action(self, action: str) -> None:
        win = self.controller.win
        if win is not None:
            try:
                win.react_to_emotion(action)
            except Exception:
                logging.exception("emotion action 应用失败")

    def _apply_care_line(self, line: str) -> None:
        """主动关怀台词 → 气泡（主线程）。开会/游戏安静态不插嘴。"""
        win = self.controller.win
        if win is None or not line:
            return
        if getattr(win, "is_quiet", lambda: False)():
            return
        try:
            win.show_bubble(str(line), duration_ms=6000)
        except Exception:
            logging.exception("主动关怀气泡失败")

    def _apply_office_root(self, root) -> None:
        """办公区「主控鲸」实时状态 → 桌宠镜像（主线程）。"""
        win = self.controller.win
        if win is not None and isinstance(root, dict):
            try:
                win.mirror_agent(root)
            except Exception:
                logging.exception("办公区镜像应用失败")

    def _apply_handoff(self, info) -> None:
        """办公区「搬家」→ 走入/走回动画（主线程）。"""
        win = self.controller.win
        if win is None or not isinstance(info, dict):
            return
        try:
            direction = str(info.get("dir") or "to_desktop")
            fs = info.get("fromScreen") if isinstance(info.get("fromScreen"), dict) else {}
            sx = fs.get("x")
            sy = fs.get("y")
            if direction == "to_office":
                agent_id = str(info.get("agentId") or info.get("id") or "root")
                handoff_id = str(info.get("handoffId") or "")

                def _arrived() -> None:
                    srv = getattr(self.controller, "_work_server", None)
                    if srv is not None:
                        owners = srv.set_on_desktop(agent_id, False, handoff_id=handoff_id)
                        if agent_id in owners:
                            # 动画期间连接器失联/交接超时：提交被拒绝，桌面立即重新显示。
                            self.controller._on_companion_recovered({
                                "reason": "handoff_commit_rejected",
                                "recoveredToDesktop": True,
                                "state": srv.companion_snapshot(),
                            })

                win.handoff_leave(sx, sy, on_done=_arrived)
            else:
                if getattr(win, "_context_hidden", False):
                    # 会议/手动勿扰优先于视觉归属；状态回桌面，但继续保持安静隐藏。
                    return
                # handoff_enter 内部会：先定位到大门→透明度0→show→淡入滑入，无需提前 show
                win.handoff_enter(float(sx if sx is not None else 0.0),
                                  float(sy if sy is not None else 0.0),
                                  label=str(info.get("label") or ""))
        except Exception:
            logging.exception("搬家动画应用失败")

    def _apply_companion_recovered(self, event) -> None:
        """Office 失联或交接失败时，让唯一的鲸鱼娘安全回到桌面。"""
        win = self.controller.win
        if win is None or not isinstance(event, dict):
            return
        if getattr(win, "_context_hidden", False):
            return
        try:
            win.setWindowOpacity(1.0)
            win.show()
            win.raise_()
            reason = str(event.get("reason") or "")
            if reason == "connector_lease_expired":
                win.show_bubble("办公室断开了，我先回桌面陪你～", duration_ms=3200)
        except Exception:
            logging.exception("鲸鱼娘恢复桌面显示失败")

    def _apply_context(self, context: str, app) -> None:
        """情境变化 → 桌宠行为（主线程）。"""
        win = self.controller.win
        if win is not None:
            try:
                win.apply_context(context, app)
            except Exception:
                logging.exception("情境感知应用失败")

    def _apply_memory_notice(self, line: str) -> None:
        win = self.controller.win
        if win is None or not line or getattr(win, "is_quiet", lambda: False)():
            return
        try:
            win.show_bubble(str(line), duration_ms=5200)
        except Exception:
            logging.exception("记忆报信气泡失败")


def _setup_logging(config: Config) -> None:
    config.dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(config.dir / 'pet.log'),
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        encoding='utf-8',
    )


def _show_startup_error(title: str, message: str) -> None:
    QMessageBox.critical(None, title, message)


def _cleanup_stale_runtime_dirs() -> None:
    """清理 PyInstaller onefile 遗留的 ``_MEI*`` 临时目录。

    只扫描系统临时目录中超过 24 小时的目录，并始终跳过当前进程的
    ``sys._MEIPASS``。删除失败只记录日志，不接管 ACL，也不影响启动。
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return

    current = Path(meipass).resolve(strict=False)
    result = cleanup_stale_runtime_dirs(current_dir=current)
    for directory in result.removed:
        logging.info("已清理遗留 PyInstaller 缓存目录: %s", directory)
    for directory, error in result.failed.items():
        logging.warning("清理 PyInstaller 缓存目录失败: %s (%s)", directory, error)

class PetApp:
    """管理桌宠窗口、托盘与角色热切换。"""

    def __init__(self, app: QApplication, config: Config, enable_chat: bool = True) -> None:
        self.app = app
        self.config = config
        self.enable_chat = bool(enable_chat)
        self.win: PetWindow | None = None
        self.tray: QSystemTrayIcon | None = None
        self.chat_window = None
        self.legacy_chat_window = None
        self.modern_chat_window = None
        self.chat_settings_dialog = None
        self.modern_settings_dialog = None
        self._spawned_pet_count = 0
        self._pending_dialog_opens: set[str] = set()
        self._balance_busy = False
        self._balance_cache = None
        self._balance_bridge = None
        self._balance_timer = QTimer()
        self._balance_timer.timeout.connect(self.show_balance)
        self._update_bridge = None
        self._work_bridge = _WorkStateBridge(self)
        self._work_server: WorkStateServer | None = None
        self._companion_timer: QTimer | None = None
        self._ledger_timer: QTimer | None = None
        self._ledger_busy = False
        # 情境感知（前台应用监听 → context 事件）
        self._context_timer: QTimer | None = None
        self._context_monitor: ContextAwareMonitor | None = None
        self._context_busy = False
        # 情绪响应状态
        self._last_react_msg: str = ""
        self._last_react_ts: float = 0.0
        self._emotion_react_interval: float = 15.0
        self._seen_react_fps: set = set()
        self._current_sid: str = ""
        # 主动关怀（久坐/深夜/卡住/欢迎回来）
        self._care = ProactiveCare(self.config.get("proactive_care_thresholds") or {})
        self._care_ticks: int = 0
        # 会议关怀（按开会时长分档反馈）
        self._meeting_care = MeetingCare(
            self.config.get("meeting_care_thresholds") or None,
            enabled=bool(self.config.get("meeting_care_enabled", True)),
        )
        memory_path = self.config.dir / "memory" / "shared_memory.json"
        roots = self.config.get("memory_project_roots") or []
        roots = roots if isinstance(roots, list) else []
        self.memory_store = SharedMemoryStore(memory_path)
        self.activity_collector = ActivityCollector(
            self.memory_store,
            project_enricher=ProjectEnricher(roots),
            idle_seconds=float(self.config.get("memory_idle_seconds", 180) or 180),
            min_segment_seconds=float(self.config.get("memory_min_segment_seconds", 20) or 20),
        )
        self.memory_sync = MemorySyncManager(self.config, self.memory_store)
        self.memory_sync.delivered.connect(self._on_memory_delivered)
        self._shutdown_done = False
        about_to_quit = getattr(self.app, "aboutToQuit", None)
        connect = getattr(about_to_quit, "connect", None)
        if callable(connect):
            connect(self._shutdown_runtime)

    # ------------------------------------------------------------ 启动
    def start(self) -> None:
        character_id = str(self.config.get('character', catalog.DEFAULT_CHARACTER))
        logging.info('当前形象: %s', character_id)
        self._create_ui(character_id)
        self._apply_spawn_offset()
        self._apply_balance_timer()
        self._start_work_state()
        self._start_ledger_timer()
        self._start_context_monitor()
        self.memory_sync.start()
        QTimer.singleShot(3500, self._check_autostart_wanted)

    # ------------------------------------------------------------ DSH 工作状态联动
    def _start_work_state(self) -> None:
        """启动信标接收端：页内信标把 DSH 是否正在干活 POST 到本进程。"""
        if self._work_server is not None:
            return
        self.app.aboutToQuit.connect(self._stop_work_state)
        server = WorkStateServer(
            on_change=self._on_work_state_change,
            # Token 记账已改为直读 DSH 会话日志，不再依赖信标上报用量
            on_usage=None,
            on_emote=self._on_emote,
            # 办公区(dsh-agent-office)联动：主控鲸状态镜像 + 搬家
            on_root=self._on_office_root,
            on_handoff=self._on_handoff,
            on_companion_recovered=self._on_companion_recovered,
        )
        if server.start():
            self._work_server = server
            timer = QTimer()
            timer.setInterval(1000)
            timer.timeout.connect(self._poll_companion_state)
            timer.start()
            self._companion_timer = timer
            if self.win is not None:
                # 桌宠被拖动松手 → 判断是否拖进了办公区面板(搬回)
                self.win.on_dropped = self._on_pet_dropped
                # 桌宠拖动中 → 上报位置,办公区 ghost 无感进入预览
                self.win.on_drag_move = self._on_pet_drag_move
                self.win.show_bubble(
                    f"已连接工作状态信标（127.0.0.1:{server.port}）",
                    duration_ms=3200,
                )
        else:
            if self.win is not None:
                self.win.show_bubble("工作状态信标端口被占用，本次不联动", duration_ms=3200)

    def _on_work_state_change(self, working: bool, detail: str) -> None:
        # HTTP 线程 → Qt 主线程（Signal 队列投递）
        self._work_bridge.changed.emit(working, detail)

    def _on_office_root(self, root: dict) -> None:
        # 办公区推来的主控鲸状态（HTTP 线程）→ 主线程镜像
        self._work_bridge.office_root.emit(root)

    def _on_handoff(self, info: dict) -> None:
        # 办公区触发搬家（HTTP 线程）→ 主线程播动画
        self._work_bridge.handoff.emit(info)

    def _on_companion_recovered(self, event: dict) -> None:
        self._work_bridge.companion_recovered.emit(event)

    def _poll_companion_state(self) -> None:
        server = self._work_server
        if server is not None:
            server.expire_companion()

    def _pet_over_office(self, geo):
        """返回 (rect, rid, cx, cy, over)；rect/rid 缺失时 over=False。"""
        srv = self._work_server
        if srv is None:
            return None, None, 0.0, 0.0, False
        rect = srv.office_rect()
        rid = srv.office_root_id()
        cx = geo.x() + geo.width() / 2.0
        cy = geo.y() + geo.height() / 2.0
        over = False
        if rect and rid:
            left, top = rect.get("left"), rect.get("top")
            right, bottom = rect.get("right"), rect.get("bottom")
            if None not in (left, top, right, bottom):
                over = (left <= cx <= right and top <= cy <= bottom)
        return rect, rid, cx, cy, over

    def _on_pet_drag_move(self, geo) -> None:
        """桌宠拖动中（主线程）→ 上报实时位置 + 是否压在办公区上，触发办公区 ghost 预览。

        仅当主控鲸当前「在桌面」（由桌宠持有）时，压进面板才算 over——此时桌宠
        淡出交棒、办公区冒出 ghost 跟随光标，实现无感进入。
        """
        srv = self._work_server
        win = self.win
        if srv is None or win is None:
            return
        try:
            _rect, rid, cx, cy, over = self._pet_over_office(geo)
            over = bool(over and rid and (rid in srv.desktop_list()))
            srv.set_drag_state(True, cx, cy, over)
            try:
                win.setWindowOpacity(0.32 if over else 1.0)  # 压进办公区→淡出交棒
            except Exception:
                pass
        except Exception:
            logging.exception("拖动上报失败")

    def _on_pet_dropped(self, geo) -> None:
        """桌宠拖拽松手（主线程）→ 若落在办公区面板范围内,则「搬回办公室」。

        落点语义：桌宠拖到面板的哪个位置，主控鲸最终就落到那个位置对应的
        场景坐标（屏幕坐标 → 场景坐标换算在办公区完成）。这里把桌宠窗口中心
        作为落点记录，并让桌宠走回该落点后淡出，办公区鲸在同一点接棒。
        """
        srv = self._work_server
        win = self.win
        if srv is None or win is None:
            return
        committed = False
        try:
            rect, rid, cx, cy, over = self._pet_over_office(geo)
            if rect and rid and over:
                handoff_id = srv.begin_to_office(rid)
                if not handoff_id:
                    return
                committed = True
                # 记录落点（一次性下发给办公区，决定主控鲸落到哪个场景位置）
                srv.note_drop_screen(cx, cy)

                def _arrived() -> None:
                    owners = srv.set_on_desktop(rid, False, handoff_id=handoff_id)
                    if rid in owners:
                        self._on_companion_recovered({
                            "reason": "handoff_commit_rejected",
                            "recoveredToDesktop": True,
                            "state": srv.companion_snapshot(),
                        })

                win.handoff_leave(cx, cy, on_done=_arrived)
        except Exception:
            logging.exception("拖回办公区判定失败")
        finally:
            # 结束拖动预览：清 ghost 上报；没搬回则恢复不透明度
            try:
                srv.set_drag_state(False, 0.0, 0.0, False)
            except Exception:
                pass
            if not committed:
                try:
                    win.setWindowOpacity(1.0)
                except Exception:
                    pass

    def _stop_work_state(self) -> None:
        if self._companion_timer is not None:
            self._companion_timer.stop()
            self._companion_timer = None
        if self._work_server is not None:
            self._work_server.stop()
            self._work_server = None

    # ------------------------------------------------------------ Token 账本（DSH 会话日志轮询）
    def _start_ledger_timer(self) -> None:
        """每 5 秒在后台线程解析最新 DSH 会话日志，把用量投递到账本。"""
        if self._ledger_timer is not None:
            return
        timer = QTimer()
        timer.setInterval(2000)  # 2 秒：用户消息即时落盘，缩短情绪反应延迟（原 5 秒）
        timer.timeout.connect(self._poll_ledger)
        self._ledger_timer = timer
        timer.start()
        # 启动后立刻先跑一次
        QTimer.singleShot(300, self._poll_ledger)

    def _poll_ledger(self) -> None:
        if self._ledger_busy:
            return
        self._ledger_busy = True
        threading.Thread(target=self._ledger_worker, daemon=True, name="pet-ledger").start()

    # ------------------------------------------------------------ 情境感知（前台应用监听）
    def _start_context_monitor(self) -> None:
        """每 2 秒探测一次前台应用 → 防抖后产出 context 事件（后台线程）。"""
        if self._context_timer is not None:
            return
        self._context_monitor = ContextAwareMonitor(
            detector=lambda: detect_foreground_app(
                include_title=bool(self.config.get("memory_collect_window_titles", True))
            ),
            on_change=self._on_context_change,
            rules=self._context_rules,
            enabled=self._context_enabled,
            focus_override=self._context_focus_override,
        )
        timer = QTimer()
        timer.setInterval(2000)
        timer.timeout.connect(self._poll_context)
        self._context_timer = timer
        timer.start()
        QTimer.singleShot(300, self._poll_context)  # 启动后立刻先跑一次

    def _context_rules(self) -> dict | None:
        """用户覆盖规则：非空才接管，否则回退内置默认。"""
        raw = self.config.get("context_rules") or {}
        if not isinstance(raw, dict):
            return None
        has_any = any(isinstance(v, list) and v for v in raw.values())
        return raw if has_any else None

    def _context_enabled(self) -> bool:
        return bool(self.config.get("context_aware_enabled", True))

    def _context_focus_override(self) -> bool:
        return bool(self.config.get("context_focus_enabled", False))

    def _poll_context(self) -> None:
        if self._context_busy or self._context_monitor is None:
            return
        self._context_busy = True
        threading.Thread(target=self._context_worker, daemon=True, name="pet-context").start()

    def _context_worker(self) -> None:
        try:
            snap = self._context_monitor.sample()
            idle_seconds = detect_system_idle_seconds()
            if bool(self.config.get("memory_collection_enabled", True)):
                self.activity_collector.observe(
                    snap.get("app") if isinstance(snap.get("app"), dict) else {},
                    str(snap.get("context") or "idle"),
                    idle_for_seconds=idle_seconds,
                )
                self._maybe_show_memory_notice(idle_seconds)
            # 会议关怀：按当前情境 tick（开会计时、散会结算补播台词）
            if self._meeting_care.enabled:
                result = self._meeting_care.tick(
                    str(snap.get("context") or "idle"), time.monotonic()
                )
                if result:
                    _, line = result
                    self._work_bridge.care_line.emit(line)
            # sample 已内部防抖 + 回调；changed 时 on_change 已在工作线程触发
        except Exception:
            logging.exception("情境探测异常")
        finally:
            self._context_busy = False

    def _maybe_show_memory_notice(self, idle_seconds: float = 0.0) -> None:
        """工作日结束或傍晚离开电脑时，每天最多显示一次记忆提示。"""
        now = datetime.now()
        raw_end = str(self.config.get("memory_workday_end", "18:00") or "18:00")
        try:
            hour, minute = (int(part) for part in raw_end.split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 18, 0
        reached_end = (now.hour, now.minute) >= (max(0, min(23, hour)), max(0, min(59, minute)))
        left_near_end = idle_seconds >= 10 * 60 and now.hour >= max(12, hour - 1)
        if not (reached_end or left_near_end):
            return
        if not self.memory_store.pending_reports(limit=1):
            # 全天只停留在一个应用时，当前段尚未自然结束；到报信时刻先做一次
            # 截止结算，下一轮采样会从当前时刻继续，时长不会重复。
            self.activity_collector.flush()
        if not self.memory_store.pending_reports(limit=1):
            return
        if self.memory_store.mark_notice_shown(now.date().isoformat()):
            self._work_bridge.memory_notice.emit("今天的我记下啦，等大鲸接收时我再告诉你 🌊")
            self.memory_sync.trigger()

    def report_memory_now(self) -> None:
        """用户主动触发本地报信准备；服务器未接入前绝不伪造“已收到”。"""
        self.activity_collector.flush()
        pending = self.memory_store.pending_reports(limit=1000)
        if not pending:
            self._work_bridge.memory_notice.emit("今天还没有记到足够长的活动，我再陪你一会儿～")
            return
        if self.memory_sync.trigger(force=True):
            self._work_bridge.memory_notice.emit(
                f"今天的我记下啦，正在把 {len(pending)} 条报给大鲸 🌊"
            )
        else:
            self._work_bridge.memory_notice.emit(
                f"今天的我记下啦，已经整理好 {len(pending)} 条；开启记忆同步后就能报给大鲸 🌊"
            )

    def _on_memory_delivered(self, count: int) -> None:
        """一次同步只提示一次，避免多个小批次连续刷屏。"""
        if int(count) > 0:
            self._work_bridge.memory_notice.emit("大鲸那边已经收到啦～")

    def acknowledge_memory_report(self, batch_id: str) -> bool:
        """供未来服务器同步层调用；只有真实 ACK 才显示“已经收到”。"""
        acknowledged = self.memory_store.acknowledge_report(batch_id)
        if acknowledged:
            self._work_bridge.memory_notice.emit("大鲸那边已经收到啦～")
        return acknowledged

    def record_user_emotion(self, text: str, message_id: str = "") -> bool:
        label = explicit_emotion_label(text)
        if not label:
            return False
        _, added = self.memory_store.append_emotion(text, label, message_id)
        return added

    def _shutdown_runtime(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.activity_collector.flush()
        except Exception:
            logging.exception("退出时结算桌面记忆失败")
        self.memory_sync.stop()
        if self.win is not None:
            self.win.shutdown_media()

    def _on_context_change(self, context: str, app: dict) -> None:
        # 工作线程 → Qt 主线程（Signal 队列投递）
        self._work_bridge.context.emit(context, app)

    def _ledger_worker(self) -> None:
        try:
            # 累计 = 所有工作区全部会话总账；本会话 = 当前工作区最新会话
            period = str(self.config.get("token_period", "all") or "all")
            total_totals, total_model = session_reader.aggregate_all_sessions(period=period)
            session_file = session_reader.find_current_session_file()
            empty = {"input": 0, "output": 0, "cacheRead": 0, "reasoning": 0}
            if session_file is None:
                sid, current_totals, model = "", dict(empty), ""
                current_model = {}
            else:
                sid, current_totals, model, current_model = \
                    session_reader.read_session_usage(session_file, period)
            self._work_bridge.ledger_changed.emit(
                sid, current_totals, total_totals, model,
                current_model, total_model)
            # 情绪响应：新回合 + 空闲 + 节流 → 本地/LLM 决策动作
            if session_file is not None:
                self._maybe_emotion_react(session_file)
            # 主动关怀：每 30 秒节流（2 秒轮询 × 15 次）
            self._care_ticks += 1
            if self._care_ticks >= 15:
                self._care_ticks = 0
                self._tick_proactive_care(session_file)
        except Exception:
            logging.exception("会话日志账本解析失败")
        finally:
            self._ledger_busy = False

    def _tick_proactive_care(self, session_file) -> None:
        """推进主动关怀状态机（后台线程）；命中则投递台词到主线程气泡。"""
        try:
            if not bool(self.config.get("proactive_care_enabled", True)):
                return
            server = self._work_server
            working = bool(server and server.working)
            detail = server.detail if server is not None else ""
            user_ts = session_reader.latest_user_message_time() if session_file is not None else 0.0
            result = self._care.tick(time.monotonic(), working, detail, user_ts)
            if result:
                _, line = result
                self._work_bridge.care_line.emit(line)
        except Exception:
            logging.exception("主动关怀 tick 失败")

    # ------------------------------------------------------------ 情绪响应
    def _maybe_emotion_react(self, session_file) -> None:
        try:
            if not bool(self.config.get("emotion_reactions_enabled", True)):
                return
            # 仅空闲时响应（DSH 没在跑任务）
            server = self._work_server
            if server is not None and server.working:
                return
            # 节流：两次响应至少间隔 N 秒
            now = time.monotonic()
            if self._last_react_ts and (now - self._last_react_ts) < self._emotion_react_interval:
                return
            # 以全局最新一条用户消息为准；锁定"当前会话"，别的会话不干扰
            session_id, fingerprint, text = session_reader.latest_user_message_global()
            if not fingerprint or not text:
                return
            if session_id and session_id != self._current_sid:
                # 用户切到了新会话 → 切换跟踪目标，清空已处理记录
                logging.info("情绪响应：切换到会话 %s", session_id[:16])
                self._current_sid = session_id
                self._seen_react_fps.clear()
            if fingerprint in self._seen_react_fps:
                return
            self._seen_react_fps.add(fingerprint)
            if len(self._seen_react_fps) > 500:  # 防内存无限增长
                self._seen_react_fps = set(list(self._seen_react_fps)[-300:])
            self._last_react_msg = fingerprint
            self._last_react_ts = now
            self._react_to_text(text, f"log:{fingerprint}")
        except Exception:
            logging.exception("情绪响应判断失败")

    # ------------------------------------------------------------ 情绪响应（实时 + 日志兜底）
    def _react_to_text(self, text: str, origin: str) -> None:
        """混合决策并播放动作（本地为主，必要时 LLM 升级）。"""
        try:
            provider = None
            api_key = ""
            try:
                settings = self.config.chat_settings()
                provider = settings.active_config
                api_key = self.config.resolve_api_key(provider)
            except Exception:
                provider = None
            action, source = emotion_actor.decide_action(text, provider, api_key)
            if action:
                logging.info("情绪响应 [%s]: %s <- %s", source, action, origin)
                self._work_bridge.emotion_action.emit(action)
        except Exception:
            logging.exception("情绪响应决策失败")

    def _on_emote(self, text: str) -> None:
        """页内信标实时上报的用户消息（HTTP 线程）→ 后台决策，立即响应。"""
        if not bool(self.config.get("emotion_reactions_enabled", True)):
            return
        # 仅空闲时响应
        server = self._work_server
        if server is not None and server.working:
            return
        # 节流
        now = time.monotonic()
        if self._last_react_ts and (now - self._last_react_ts) < self._emotion_react_interval:
            return
        self._last_react_ts = now
        text = (text or "").strip()[:500]
        if not text:
            return
        threading.Thread(
            target=self._react_to_text, args=(text, "realtime"), daemon=True, name="pet-emote"
        ).start()

    def _set_autostart(self, enabled: bool, win=None) -> bool:
        ok = autostart_mod.set_enabled(bool(enabled))
        self.config.set("autostart_wanted", bool(enabled))
        self.config.save()
        target = win or self.win
        if target is not None and not ok:
            target.show_bubble("开机自启写入失败，请检查系统登录项或安全软件设置。", duration_ms=6000)
        return ok

    def _check_autostart_wanted(self) -> None:
        if self.config.get("autostart_wanted", False) and not autostart_mod.is_enabled() and self.win is not None:
            self.win.show_bubble("检测到开机自启已被系统或安全软件关闭，可在设置中重新启用。", duration_ms=7000)

    def _apply_balance_timer(self) -> None:
        self._balance_timer.stop()
        minutes = max(0, int(self.config.get("balance_refresh_minutes", 0) or 0))
        if minutes:
            self._balance_timer.start(minutes * 60000)

    def show_balance(self, parent=None) -> None:
        win = parent or self.win
        if win is None or self._balance_busy or not win.isVisible():
            return
        now = time.monotonic()
        if self._balance_cache and now - self._balance_cache[0] < 30:
            win.show_bubble(self._balance_cache[1], duration_ms=6000)
            return
        self._balance_busy = True
        win.show_bubble("正在查询余额…", duration_ms=6000)
        provider = self.config.chat_settings().active_config
        provider.api_key = self.config.resolve_api_key(provider)
        bridge = _BalanceBridge(win)
        self._balance_bridge = bridge

        def worker() -> None:
            try:
                result = balance_mod.fetch_balance(provider.base_url, provider.api_key, verify_ssl=provider.verify_ssl)
                message = balance_mod.format_balance(result)
                self._balance_cache = (time.monotonic(), message)
                bridge.done.emit(True, message)
            except Exception as exc:
                bridge.done.emit(False, f"余额查询失败：{exc}")
            finally:
                self._balance_busy = False

        threading.Thread(target=worker, daemon=True, name="pet-balance").start()

    def check_update(self, parent=None) -> None:
        target = parent or self.win
        if target is not None:
            target.show_bubble("正在检查更新…", duration_ms=6000)
        bridge = _UpdateBridge(target)
        self._update_bridge = bridge

        def worker() -> None:
            release = updater.latest_release()
            bridge.done.emit(bool(release), release or "无法连接更新服务，请稍后重试。")

        threading.Thread(target=worker, daemon=True, name="pet-update-check").start()

    def sync_look_to_chat(self, user_text: str, reply: str) -> None:
        if self.chat_window is not None and hasattr(self.chat_window, "append_look_sync"):
            self.chat_window.append_look_sync(user_text, reply)

    def _apply_spawn_offset(self) -> None:
        """让新孵化的桌宠与母桌宠错开，避免两个窗口完全重叠。"""
        if self.win is None:
            return
        try:
            index = max(0, int(os.environ.get('DSH_PET_SPAWN_OFFSET_INDEX', '0')))
        except ValueError:
            index = 0
        if index <= 0:
            return
        scr = self.win._screen_available()
        if scr is None:
            return
        available = scr.availableGeometry()
        horizontal = -1 if self.win.geometry().center().x() > available.center().x() else 1
        vertical = -1 if self.win.geometry().center().y() > available.center().y() else 1
        x = self.win.x() + horizontal * 48 * index
        y = self.win.y() + vertical * 32 * index
        # 小屏（可用区比窗口还窄/矮）时上界 < 下界，min/max 会互相打架把
        # 窗口推出屏幕外；先判边界再钳制。
        max_x = available.right() - self.win.width() + 1
        max_y = available.bottom() - self.win.height() + 1
        x = available.left() if max_x < available.left() else min(max(x, available.left()), max_x)
        y = available.top() if max_y < available.top() else min(max(y, available.top()), max_y)
        self.win.move(x, y)

    def _create_library(self, character_id: str) -> MovieLibrary:
        lib = MovieLibrary(character_id=character_id)
        logging.info('素材加载完成：%s %d 段动画', character_id, len(lib.names()))
        return lib

    def _create_ui(self, character_id: str) -> None:
        lib = self._create_library(character_id)
        win = PetWindow(lib, self.config)
        win.on_switch_character = self.switch_character
        win.on_open_chat = self.open_chat if self.enable_chat else None
        win.on_open_chat_settings = self.open_chat_settings if self.enable_chat else None
        win.on_show_balance = self.show_balance if self.enable_chat else None
        win.on_check_update = self.check_update
        win.on_look_synced = self.sync_look_to_chat if self.enable_chat else None
        win.on_look_screen = win._on_look_screen if self.enable_chat and hasattr(win, "_on_look_screen") else None
        win.on_open_legacy_settings = None
        win.on_open_modern_settings = self.open_modern_settings
        win.on_spawn_pet = self.spawn_pet
        win.on_restore_fun_windows = restore_ojingjing_windows
        win.on_memory_report = self.report_memory_now
        win.on_user_chat_message = self.record_user_emotion
        win.on_hidden = self._notify_pet_hidden
        win.show()

        tray = self._build_tray(win)

        # 清理旧对象（热切换时使用）
        old_win = self.win
        old_tray = self.tray
        self.win = win
        self.tray = tray

        if old_win is not None:
            old_win.hide(notify=False)
            old_win.shutdown_media()
            old_tray.hide() if old_tray is not None else None
            QTimer.singleShot(0, old_win.deleteLater)
            if old_tray is not None:
                QTimer.singleShot(0, old_tray.deleteLater)

        self.app.aboutToQuit.connect(win._save_position)

    # ------------------------------------------------------------ 角色切换
    def switch_character(self, character_id: str) -> None:
        if self.win is None:
            return
        current = str(self.config.get('character', catalog.DEFAULT_CHARACTER))
        if character_id == current:
            return

        # 先保存配置，即使后续加载失败也记住用户选择
        self.config.set('character', character_id)
        self.config.save()

        try:
            # 预创建新库，失败则保留当前角色
            lib = self._create_library(character_id)
        except Exception as exc:
            logging.exception('切换角色失败: %s', character_id)
            _show_startup_error('切换角色失败', str(exc))
            return

        logging.info('切换角色: %s -> %s', current, character_id)

        # 用新库创建新窗口/托盘，旧对象延迟销毁
        win = PetWindow(lib, self.config)
        win.on_switch_character = self.switch_character
        win.on_open_chat = self.open_chat if self.enable_chat else None
        win.on_open_chat_settings = self.open_chat_settings if self.enable_chat else None
        win.on_show_balance = self.show_balance if self.enable_chat else None
        win.on_check_update = self.check_update
        win.on_look_synced = self.sync_look_to_chat if self.enable_chat else None
        win.on_look_screen = win._on_look_screen if self.enable_chat and hasattr(win, "_on_look_screen") else None
        win.on_open_legacy_settings = None
        win.on_open_modern_settings = self.open_modern_settings
        win.on_spawn_pet = self.spawn_pet
        win.on_restore_fun_windows = restore_ojingjing_windows
        win.on_memory_report = self.report_memory_now
        win.on_user_chat_message = self.record_user_emotion
        win.on_hidden = self._notify_pet_hidden
        win.show()

        tray = self._build_tray(win)

        old_win = self.win
        old_tray = self.tray
        self.win = win
        self.tray = tray

        old_win.hide(notify=False)
        old_win.shutdown_media()
        if old_tray is not None:
            old_tray.hide()
        QTimer.singleShot(0, old_win.deleteLater)
        if old_tray is not None:
            QTimer.singleShot(0, old_tray.deleteLater)
        if self.enable_chat:
            for chat_window in (self.legacy_chat_window, self.modern_chat_window):
                if chat_window is not None:
                    chat_window.set_pet_window(self.win)
                    chat_window.switch_character(character_id)

        self.app.aboutToQuit.connect(win._save_position)

    def open_chat(self) -> None:
        """Open the configured chat UI; menus only need this stable dispatcher."""
        if str(self.config.get("chat_ui_style", "modern")) == "classic":
            self.open_legacy_chat()
        else:
            self.open_modern_chat()

    def open_legacy_chat(self) -> None:
        if not self.enable_chat or self.win is None:
            return
        if self._defer_while_popup_active("legacy-chat", self.open_chat):
            return
        from .chat.legacy_widgets import ChatWindow
        if self.legacy_chat_window is None:
            self.legacy_chat_window = ChatWindow(self.config, str(self.config.get('character', catalog.DEFAULT_CHARACTER)), pet_window=self.win)
        else:
            self.legacy_chat_window.set_pet_window(self.win)
        self.chat_window = self.legacy_chat_window
        self._present_dialog(self.legacy_chat_window, lambda: self.legacy_chat_window.position_near_pet(self.win))

    def open_modern_chat(self) -> None:
        if not self.enable_chat or self.win is None:
            return
        if self._defer_while_popup_active("modern-chat", self.open_modern_chat):
            return
        from .chat.widgets import ChatWindow
        if self.modern_chat_window is None:
            self.modern_chat_window = ChatWindow(self.config, str(self.config.get('character', catalog.DEFAULT_CHARACTER)), pet_window=self.win)
        else:
            self.modern_chat_window.set_pet_window(self.win)
        self.chat_window = self.modern_chat_window
        self._present_dialog(self.modern_chat_window, lambda: self.modern_chat_window.position_near_pet(self.win))

    def spawn_pet(self) -> None:
        """启动一个完全独立的新桌宠进程。"""
        try:
            self._spawned_pet_count += 1
            launch_new_pet(self._spawned_pet_count)
        except OSError as exc:
            self._spawned_pet_count = max(0, self._spawned_pet_count - 1)
            logging.exception('生小肥鱼失败')
            _show_startup_error('生小肥鱼失败', str(exc))

    def _defer_while_popup_active(self, key: str, callback) -> bool:
        """Avoid constructing a heavy dialog inside QMenu.exec()."""
        if QApplication.activePopupWidget() is None:
            self._pending_dialog_opens.discard(key)
            return False
        if key in self._pending_dialog_opens:
            return True
        self._pending_dialog_opens.add(key)

        def retry() -> None:
            if QApplication.activePopupWidget() is not None:
                QTimer.singleShot(50, retry)
                return
            self._pending_dialog_opens.discard(key)
            callback()

        QTimer.singleShot(50, retry)
        return True

    def _present_dialog(self, dialog, before_present=None, attempt: int = 0) -> None:
        """延迟呈现非模态窗口，直到任何弹出菜单关闭。

        macOS 的右键/托盘菜单是原生 NSMenu 跟踪会话（menu.exec 阻塞期间），
        菜单项动作触发时会话尚未结束，此时新建窗口的 show/raise/activate
        会被 AppKit 抑制——表现为首次点击「AI 设置 / 桌宠设置」无反应，
        需要再点一次（此时窗口实例已存在，直接 show 成功）。
        延迟到菜单关闭后再呈现即可稳定弹出；Qt 自绘菜单（Windows）同样
        覆盖：弹窗仍显示时重试等待。重试 60 次（约 3.6 秒）后放弃，
        防止弹窗长期不消失时无限空转。
        """
        if attempt > 60:
            return
        if QApplication.activePopupWidget() is not None:
            QTimer.singleShot(60, lambda: self._present_dialog(dialog, before_present, attempt + 1))
            return
        if before_present is not None:
            before_present()
        if dialog.isMinimized():
            dialog.showNormal()
        else:
            dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_chat_settings(self) -> None:
        """Open settings without blocking the desktop pet window.

        QDialog.exec() makes the dialog application-modal, which prevents the
        user from dragging or interacting with the pet while editing settings.
        Keep one modeless dialog alive instead, and refresh the chat window
        after the dialog reports an accepted save.
        """
        if not self.enable_chat:
            return
        from .chat.settings_dialog import ChatSettingsDialog
        if self.chat_settings_dialog is None:
            dialog = ChatSettingsDialog(self.config, self.chat_window)
            dialog.setModal(False)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dialog.finished.connect(self._chat_settings_finished)
            self.chat_settings_dialog = dialog
        self._present_dialog(self.chat_settings_dialog)

    def _chat_settings_finished(self, result: int) -> None:
        dialog = self.chat_settings_dialog
        self.chat_settings_dialog = None
        if result:
            self._refresh_chat_windows()

    def _refresh_chat_windows(self) -> None:
        """Refresh both independently styled chat windows after shared settings change."""
        for chat_window in (self.legacy_chat_window, self.modern_chat_window):
            if chat_window is not None:
                chat_window.refresh_settings()

    # ------------------------------------------------------------ 托盘
    def open_modern_settings(self) -> None:
        from .modern_settings_dialog import ModernSettingsDialog
        if self.modern_settings_dialog is None:
            dialog = ModernSettingsDialog(
                self.config,
                self.win,
                include_ai=self.enable_chat,
            )
            dialog.finished.connect(self._modern_settings_finished)
            self.modern_settings_dialog = dialog
        # 在 show 之前定位，避免 Windows 上窗口先显示默认位置再跳走（闪现小窗）
        self._present_dialog(
            self.modern_settings_dialog,
            before_present=self.modern_settings_dialog.move_away_from_pet,
        )

    def _modern_settings_finished(self, result: int) -> None:
        self.modern_settings_dialog = None
        if not result:
            return
        if self.win is not None:
            self.win.refresh_pet_settings()
            # 主动识屏 / Agent 联动：设置保存后立即按新配置启停（无需重启）
            if getattr(self.win, "proactive_watcher", None) is not None:
                try:
                    self.win.proactive_watcher.apply_config()
                except Exception:
                    logging.exception("主动识屏配置重载失败")
            if getattr(self.win, "agent_link_manager", None) is not None:
                try:
                    self.win.agent_link_manager.apply_config()
                except Exception:
                    logging.exception("Agent 联动配置重载失败")
        self._apply_balance_timer()
        self._reload_proactive_care()
        self._reload_meeting_care()
        self._reload_activity_memory()
        self.memory_sync.apply_config()
        self._refresh_chat_windows()
        _mac_set_dock_icon_visible(bool(self.config.get("show_dock_icon", True)))

    def _reload_proactive_care(self) -> None:
        """设置保存后重建主动关怀状态机（阈值即时生效，无需重启）。"""
        try:
            self._care = ProactiveCare(self.config.get("proactive_care_thresholds") or {})
        except Exception:
            logging.exception("主动关怀配置重载失败")

    def _reload_activity_memory(self) -> None:
        """设置保存后结算旧段并按新阈值重建采集器。"""
        try:
            self.activity_collector.flush()
            roots = self.config.get("memory_project_roots") or []
            roots = roots if isinstance(roots, list) else []
            self.activity_collector = ActivityCollector(
                self.memory_store,
                project_enricher=ProjectEnricher(roots),
                idle_seconds=float(self.config.get("memory_idle_seconds", 180) or 180),
                min_segment_seconds=float(self.config.get("memory_min_segment_seconds", 20) or 20),
            )
        except Exception:
            logging.exception("日常记忆配置重载失败")

    def _reload_meeting_care(self) -> None:
        """设置保存后重建会议关怀状态机（开关/档位即时生效，无需重启）。"""
        try:
            self._meeting_care = MeetingCare(
                self.config.get("meeting_care_thresholds") or None,
                enabled=bool(self.config.get("meeting_care_enabled", True)),
            )
        except Exception:
            logging.exception("会议关怀配置重载失败")

    def _notify_pet_hidden(self) -> None:
        """用户主动隐藏桌宠后弹托盘提示，指明恢复入口。"""
        if self.tray is None:
            return
        self.tray.showMessage(
            "桌宠已隐藏",
            "点击托盘图标或 Dock 图标即可恢复。",
            QSystemTrayIcon.MessageIcon.Information,
            4000,
        )

    def _build_tray(self, win: PetWindow) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(QIcon(win.icon_pixmap()))

        def toggle_visible() -> None:
            if win.isVisible():
                win.hide()
            else:
                win.show()

        menu = QMenu()
        menu.addAction('显示 / 隐藏', toggle_visible)
        if self.enable_chat:
            menu.addAction('AI 对话', self.open_chat)
            menu.addAction('AI 设置', self.open_chat_settings)
        menu.addAction('桌宠设置', self.open_modern_settings)

        m_char = menu.addMenu('切换角色')
        current = str(self.config.get('character', catalog.DEFAULT_CHARACTER))
        for cid in catalog.list_available_characters():
            act = m_char.addAction(cid)
            act.setCheckable(True)
            act.setChecked(cid == current)
            act.triggered.connect(lambda checked=False, cid=cid: self.switch_character(cid))

        mouse_through = menu.addAction('鼠标穿透')
        mouse_through.setCheckable(True)
        mouse_through.setChecked(bool(self.config.get('mouse_through', False)))
        mouse_through.toggled.connect(win.set_mouse_through)

        menu.addSeparator()

        auto = menu.addAction('开机自启')
        auto.setCheckable(True)
        auto.setChecked(autostart_mod.is_enabled())
        auto.toggled.connect(lambda enabled: self._set_autostart(enabled, win))

        menu.addSeparator()
        if self.enable_chat:
            menu.addAction('DeepSeek 余额', lambda: self.show_balance(win))
        menu.addAction('Token 花费统计', lambda: win.show_token_cost())
        menu.addAction('Token 花费设置', lambda: win.open_token_cost_settings())
        menu.addAction('检查更新', lambda: self.check_update(win))
        menu.addAction('启动 DeepSeek Harness', lambda: launch_harness_gui(win))
        menu.addAction('退出', self.app.quit)

        tray.setContextMenu(menu)
        tray.setToolTip('dsh-pet 独立桌宠')
        tray.activated.connect(
            lambda reason: toggle_visible()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
        tray.show()
        return tray


def _mac_set_dock_icon_visible(visible: bool) -> None:
    """Switch the macOS application policy without restarting the pet.

    The speech bubble itself owns the non-activating window flags; application
    activation policy must not be used as a focus workaround because Accessory
    Regular (0) displays a Dock item; Accessory (1) keeps the application out
    of the Dock. Pet tool windows own their independent visibility/focus flags.
    """
    if sys.platform != 'darwin':
        return
    try:
        from PySide6.QtGui import QGuiApplication
        if QGuiApplication.instance() is not None and QGuiApplication.platformName() != "cocoa":
            return
    except Exception:
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc') or '/usr/lib/libobjc.A.dylib')
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.objc_getClass.restype = ctypes.c_void_p
        msg = objc.objc_msgSend
        msg.restype = ctypes.c_void_p
        msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        shared = msg(
            objc.objc_getClass(b'NSApplication'),
            objc.sel_registerName(b'sharedApplication'),
        )
        # NSApplicationActivationPolicyRegular = 0; Accessory = 1
        msg.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        msg(shared, objc.sel_registerName(b'setActivationPolicy:'), 0 if visible else 1)
    except Exception:
        pass


def _set_app_icon(app) -> None:
    """设置应用/Dock 图标为鲸鱼娘图标，避免源码运行时显示成 Python 图标。

    macOS 上 QApplication.setWindowIcon 即 Dock 图标；优先用内置 icon.icns。
    """
    from PySide6.QtGui import QIcon

    try:
        icon_path = Path(__file__).resolve().parent.parent / "assets" / "icon.icns"
        if icon_path.is_file():
            app.setWindowIcon(QIcon(str(icon_path)))
            return
        # 回退：内置图标缺失时至少设置一个占位，避免纯 Python 图标
        app.setWindowIcon(QIcon.fromTheme("applications-games"))
    except Exception:
        pass


def main(argv: list[str] | None = None, enable_chat: bool = True) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_DIR_NAME)
    app.setQuitOnLastWindowClosed(False)

    config = Config()
    _mac_set_dock_icon_visible(bool(config.get("show_dock_icon", True)))
    _set_app_icon(app)  # 在激活策略之后设图标，避免被重置
    _setup_logging(config)
    logging.info('dsh-pet-standalone 启动')
    _cleanup_stale_runtime_dirs()

    controller = PetApp(app, config, enable_chat=enable_chat)
    try:
        controller.start()
    except Exception as exc:
        logging.exception('启动失败')
        _show_startup_error('dsh-pet-standalone', str(exc))
        return 1

    logging.info('进入事件循环')
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
