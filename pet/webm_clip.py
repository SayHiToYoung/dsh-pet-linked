# -*- coding: utf-8 -*-
"""
WebM-backed clip library（webm 主路线）。

使用 imageio-ffmpeg 自带的静态 ffmpeg 解码 640×360 透明 webm：
- read_frames(..., pix_fmt='rgba', bits_per_pixel=32, input_params=['-c:v','libvpx-vp9'])
  可正确保留 VP9 alpha，输出 RGBA 原始帧。
- imageio_ffmpeg 内部在 Windows 上使用 STARTUPINFO 隐藏控制台窗口，
  避免旧 ffmpeg 子进程方案导致的“窗口反复出现/消失”。

线程模型：
- 后台 reader 线程只负责把 RGBA 字节放入有界队列；
- 主线程 QTimer 按视频 fps 从队列取帧，构造 QImage/QPixmap 并发出 frameChanged；
- 所有 Qt GUI 操作只发生在主线程。
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap

from . import catalog

logger = logging.getLogger(__name__)

# 进程内元数据缓存：(源帧率, 时长)，避免切换角色时重复启动 ffmpeg。
_META_CACHE: dict[str, tuple[float, float]] = {}

# 桌宠常驻时，24/30fps 的透明 VP9 解码会持续占用明显 CPU。15fps 对这种
# 小尺寸、缓慢动作仍足够自然，同时能把解码与 GUI 上传像素的次数近乎减半。
MAX_RENDER_FPS = 15.0
FRAME_QUEUE_SIZE = 3

try:
    import imageio_ffmpeg
except Exception as exc:  # pragma: no cover - 依赖缺失时无法使用 webm 路线
    imageio_ffmpeg = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class WebMClip(QObject):
    """与窗口层期望的媒体播放器接口兼容。"""

    available = imageio_ffmpeg is not None

    frameChanged = Signal(int)
    finished = Signal()
    errorOccurred = Signal(str)

    def __init__(self, path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self._w = catalog.CANVAS_W
        self._h = catalog.CANVAS_H
        self._bpp = 4  # RGBA

        # 元数据（惰性填充；由 MovieLibrary 并行 warm 或首次使用时读取）
        self._frame_count = 0
        self._duration = 0.0
        self._source_fps = 24.0
        self._fps = 24.0
        self.playback_speed = 1.0

        # 播放状态
        self._queue: queue.Queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(self._timer_interval())
        self._timer.timeout.connect(self._poll)

        self._current_image: QImage | None = None
        self._current_pixmap: QPixmap | None = None
        self._first_image: QImage | None = None
        self._frame_index = 0
        self._ended_fired = False
        self._running = False

    # ------------------------------------------------------------ metadata
    def _ensure_meta(self) -> None:
        if self._duration > 0 or imageio_ffmpeg is None:
            return
        key = str(self.path)
        cached = _META_CACHE.get(key)
        if cached is not None:
            self._source_fps, self._duration = cached
            self._refresh_render_fps()
            return
        gen = None
        try:
            # count_frames_and_secs 会让 ffmpeg 扫完整段视频；素材多时启动阶段
            # CPU/峰值内存都很高。read_frames 的第一项就是流 metadata，拿到即关。
            gen = imageio_ffmpeg.read_frames(
                key,
                pix_fmt='rgba',
                bits_per_pixel=self._bpp * 8,
                input_params=['-c:v', 'libvpx-vp9'],
            )
            meta = next(gen)
            fps = float(meta.get('fps') or self._fps or 24.0)
            duration = float(meta.get('duration') or 0.0)
            if fps > 0:
                self._source_fps = fps
                self._refresh_render_fps()
            if duration > 0:
                self._duration = duration
                self._frame_count = max(1, int(round(duration * self._fps)))
            _META_CACHE[key] = (self._source_fps, self._duration)
        except Exception as exc:
            logger.warning('webm 元数据读取失败 %s: %s', self.path, exc)
            # 保留默认值，后续 reader 会尝试从 read_frames 的 meta 补充
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass

    def warm_meta(self) -> None:
        """预取元数据（可被线程池并行调用）。"""
        self._ensure_meta()

    def _timer_interval(self) -> int:
        if self._fps > 0:
            return max(1, int(round(1000 / (self._fps * self.playback_speed))))
        return max(1, int(round(catalog.FRAME_MS / self.playback_speed)))

    def _refresh_render_fps(self) -> None:
        # 常速最多 15fps；用户主动加速时逐步放宽到源帧率，避免 2x 播放
        # 仅靠减少等待而把整段动画播得过快。
        cap = MAX_RENDER_FPS * max(1.0, self.playback_speed)
        self._fps = max(1.0, min(float(self._source_fps or 24.0), cap))
        if self._duration > 0:
            self._frame_count = max(1, int(round(self._duration * self._fps)))

    def frameCount(self) -> int:
        if self._frame_count <= 0:
            self._ensure_meta()
        return max(1, self._frame_count)

    def duration(self) -> float:
        if self._duration <= 0:
            self._ensure_meta()
        return self._duration / self.playback_speed if self._duration > 0 else 0.0

    def currentFrameNumber(self) -> int:
        return self._frame_index

    def currentTimeSeconds(self) -> float:
        if self._fps <= 0:
            return 0.0
        return self._frame_index / (self._fps * self.playback_speed)

    def currentPixmap(self):
        return self._current_pixmap

    # ------------------------------------------------------------ lifecycle
    def set_playback_speed(self, speed: float) -> None:
        was_running = self._running
        if was_running:
            self.stop()
        self.playback_speed = max(0.1, float(speed))
        self._refresh_render_fps()
        # _switch() 在 movie.start() 之前设置速率，不能只在 QTimer 已启动时更新。
        # 否则每个新 WebM 动画都会继续使用默认的 1x interval。
        self._timer.setInterval(self._timer_interval())
        if was_running:
            self.start()

    def start(self) -> None:
        if self._running or self._closed:
            return
        if imageio_ffmpeg is None:
            self.errorOccurred.emit(str(_IMPORT_ERROR or 'imageio_ffmpeg 不可用'))
            return

        # 在 GUI 线程读取真实 fps 后再启动 QTimer，保证新动画的实际帧率
        # 与播放速率计算一致；reader 线程只负责解码和入队。
        self._ensure_meta()
        self._timer.setInterval(self._timer_interval())
        self._stop_evt = threading.Event()
        self._queue = queue.Queue(maxsize=FRAME_QUEUE_SIZE)
        self._frame_index = 0
        self._ended_fired = False
        self._running = True

        self._thread = threading.Thread(target=self._reader, args=(self._stop_evt,), daemon=True)
        with self._threads_lock:
            self._threads.add(self._thread)
        self._thread.start()
        self._timer.start()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        if self._stop_evt is not None:
            self._stop_evt.set()
        # 每帧 RGBA 约 0.9MB；旧实现停止后仍让每个
        # 播放过的 clip 保留整队列，随机动画跑久后会按素材数累积到数百 MB。
        q = self._queue
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
        self._current_image = None
        self._current_pixmap = None
        # 不 join：reader 是 daemon 线程，避免切换动画时阻塞 UI 造成卡顿
        self._thread = None

    def close(self, timeout: float = 1.5) -> None:
        """永久关闭播放器，并在有限时间内回收仍在解码的线程。

        普通动画切换继续使用 ``stop``，不阻塞 UI；只有角色销毁或应用退出
        才调用本方法。这样既保留切换手感，也不会把 ffmpeg reader 留到
        Python 解释器退出阶段。
        """
        self._closed = True
        self.stop()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._threads_lock:
                threads = [t for t in self._threads if t.is_alive()]
            if not threads:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            threads[0].join(min(0.1, remaining))

    def jumpToFrame(self, frame_index: int) -> bool:
        # 本项目只需要回到首帧；完整 seek 通过重启 reader + 丢弃帧实现。
        if frame_index <= 0:
            self.stop()
            self._frame_index = 0
            if self._first_image is not None:
                # 首帧已缓存（后台 warm_first_frame 或上次同步解码）：
                # 主线程直接转 QPixmap，零阻塞、无旧帧残留窗口。
                self._current_image = self._first_image
                self._current_pixmap = QPixmap.fromImage(self._first_image)
            else:
                self._current_image = None
                self._current_pixmap = None
                self._decode_first_frame_sync()
            return True
        return False

    def _decode_first_qimage(self):
        """解码首帧为 QImage（线程安全：不触碰 QPixmap/QTimer）。

        返回 None 表示失败或依赖缺失；调用方负责填入 _first_image 等缓存。
        """
        if imageio_ffmpeg is None:
            return None
        gen = None
        try:
            gen = imageio_ffmpeg.read_frames(
                str(self.path),
                pix_fmt='rgba',
                bits_per_pixel=self._bpp * 8,
                input_params=['-c:v', 'libvpx-vp9'],
            )
            meta = next(gen)
            frame = next(gen)
            if meta.get('fps'):
                self._source_fps = float(meta['fps'])
                self._refresh_render_fps()
            if meta.get('duration'):
                self._duration = float(meta['duration'])
            if self._frame_count <= 0 and self._fps > 0 and self._duration > 0:
                self._frame_count = int(round(self._fps * self._duration))
            expect = self._w * self._h * self._bpp
            if len(frame) == expect:
                img = QImage(frame, self._w, self._h, self._w * self._bpp,
                             QImage.Format.Format_RGBA8888)
                if not img.isNull():
                    return img.copy()
            return None
        except Exception as exc:
            logger.warning('webm 首帧解码失败 %s: %s', self.path, exc)
            return None
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass

    def _decode_first_frame_sync(self) -> None:
        """同步解码首帧（主线程），保证 jumpToFrame(0)/currentPixmap 在 start() 前有画面。"""
        img = self._decode_first_qimage()
        if img is not None:
            self._current_image = img
            self._current_pixmap = QPixmap.fromImage(img)
            self._first_image = img

    def warm_first_frame(self) -> None:
        """后台线程预解码首帧缓存（仅 QImage，线程安全）。

        首次播放某动画时 jumpToFrame(0) 需要首帧：有缓存则主线程零阻塞，
        避免点击瞬间同步 ffmpeg 解码造成卡顿，以及 Q 弹期间残留旧动画帧。
        """
        if self._closed or self._first_image is not None or imageio_ffmpeg is None:
            return
        img = self._decode_first_qimage()
        if img is not None:
            self._first_image = img

    # ------------------------------------------------------------ reader
    def _reader(self, stop_evt: threading.Event) -> None:
        gen = None
        try:
            q = self._queue
            gen = imageio_ffmpeg.read_frames(
                str(self.path),
                pix_fmt='rgba',
                bits_per_pixel=self._bpp * 8,
                input_params=['-c:v', 'libvpx-vp9'],
                output_params=['-vf', f'fps={self._fps:g}'],
            )
            meta = next(gen)
            # 用实际流信息修正元数据
            if meta.get('fps'):
                self._source_fps = float(meta['fps'])
                self._refresh_render_fps()
            if meta.get('duration'):
                self._duration = float(meta['duration'])
            if self._frame_count <= 0 and self._fps > 0 and self._duration > 0:
                self._frame_count = int(round(self._fps * self._duration))

            for frame in gen:
                if stop_evt.is_set():
                    break
                try:
                    q.put(frame, timeout=0.2)
                except queue.Full:
                    # 队列满说明 UI 消费不过来；丢弃这一帧，保持实时性
                    pass
            # 正常播完时放入结束标记。主线程可能正忙（队列满、帧被丢弃），
            # 必须循环重试直到放入或收到停止信号；否则“最后一帧被丢弃且
            # 结束标记也丢失”会让上层永远等不到播完，动画链卡死在最后一帧。
            while not stop_evt.is_set():
                try:
                    q.put(None, timeout=0.5)
                    break
                except queue.Full:
                    continue
        except Exception as exc:
            logger.exception('webm 解码失败: %s', self.path)
            self.errorOccurred.emit(str(exc))
            # 异常中断也要放入结束标记，避免动画链卡在最后一帧
            while not stop_evt.is_set():
                try:
                    q.put(None, timeout=0.5)
                    break
                except queue.Full:
                    continue
        finally:
            if gen is not None:
                try:
                    gen.close()
                except Exception:
                    pass
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def _poll(self) -> None:
        """主线程按视频帧率逐帧取帧，不跳帧、不积压追帧。

        注意：不能一次清空队列只处理最新帧，否则会把中间帧丢弃，
        导致动画视觉上“快进”。这里每次只取最早的一帧。
        """
        try:
            item = self._queue.get_nowait()
        except queue.Empty:
            return

        if item is None:
            # 正常播完；若在处理最后一帧时已经由窗口层启动了下一个动画，
            # self._queue 已被替换，不会走到这里。
            if not self._ended_fired:
                self._ended_fired = True
                self._running = False
                self._timer.stop()
                self.finished.emit()
            return

        self._process_frame(item)

    def _process_frame(self, data: bytes) -> None:
        expect = self._w * self._h * self._bpp
        if len(data) != expect:
            logger.warning('webm 帧长度异常: got=%d expect=%d', len(data), expect)
            return
        img = QImage(data, self._w, self._h, self._w * self._bpp,
                     QImage.Format.Format_RGBA8888)
        if img.isNull():
            return
        # QPixmap.fromImage 会在 GUI 线程取得自己的像素存储；无需再先 img.copy()
        # 保留一份同尺寸 QImage。旧路径每帧至少多复制约 0.9MB。
        self._current_image = None
        self._current_pixmap = QPixmap.fromImage(img)
        self._frame_index += 1
        self.frameChanged.emit(self._frame_index)
