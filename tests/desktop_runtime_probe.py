# -*- coding: utf-8 -*-
"""真实桌宠常驻探针。

短测：QT_QPA_PLATFORM=offscreen python tests/desktop_runtime_probe.py --duration 60
八小时：python tests/desktop_runtime_probe.py --duration 28800

探针使用临时配置目录，不改用户数据；结束时输出平均 CPU、峰值内存和记忆数量。
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
os.environ.setdefault("DSH_PET_STATE_PORT", "47991")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet.app import PetApp  # noqa: E402
from pet.config import Config  # noqa: E402


def _peak_memory_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS 返回 bytes；Linux 返回 KiB。
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument(
        "--warmup",
        type=float,
        default=0.0,
        help="正式计时前的启动预热秒数（峰值内存仍覆盖整个进程）",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="停在当前帧，用于区分常驻逻辑与动画绘制开销",
    )
    args = parser.parse_args()
    duration = max(1.0, float(args.duration))
    warmup = max(0.0, float(args.warmup))

    with tempfile.TemporaryDirectory(prefix="dsh-pet-runtime-probe-") as temp_dir:
        app = QApplication([])
        config = Config(temp_dir)
        controller = PetApp(app, config, enable_chat=False)
        metric_start = {
            "wall": time.monotonic(),
            "cpu": time.process_time(),
        }
        controller.start()
        if args.static and controller.win.movie is not None:
            controller.win.movie.stop()
        if warmup:
            def begin_measurement() -> None:
                metric_start["wall"] = time.monotonic()
                metric_start["cpu"] = time.process_time()
            QTimer.singleShot(int(warmup * 1000), begin_measurement)
        QTimer.singleShot(int((warmup + duration) * 1000), app.quit)
        exit_code = app.exec()
        wall = max(0.001, time.monotonic() - metric_start["wall"])
        cpu = max(0.0, time.process_time() - metric_start["cpu"])
        snapshot = controller.memory_store.snapshot()
        print(json.dumps({
            "requestedSeconds": duration,
            "warmupSeconds": warmup,
            "wallSeconds": round(wall, 3),
            "averageCpuPercent": round(cpu / wall * 100, 2),
            "staticFrame": bool(args.static),
            "peakMemoryMb": round(_peak_memory_mb(), 2),
            "facts": len(snapshot.get("facts", [])),
            "clues": len(snapshot.get("clues", [])),
            "unackedReports": len(controller.memory_store.pending_reports(limit=100000)),
            "exitCode": int(exit_code),
        }, ensure_ascii=False))
        return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
