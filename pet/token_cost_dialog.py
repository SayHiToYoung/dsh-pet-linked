# -*- coding: utf-8 -*-
"""
Token 花费显示设置（二次开发新增）—— 让用户自己勾选要看什么数据。

可配置项（存 config.json，键名 token_display_*）：
  token_display_fields : ["input","output","cacheRead","price"]  显示哪些字段
  token_display_scopes : ["session","lifetime"]                  显示哪个口径
  token_display_format : "auto" | "km" | "full"                  数字格式
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

FIELD_OPTIONS = [
    ("input", "输入"),
    ("output", "输出"),
    ("cacheRead", "命中"),
    ("reasoning", "推理"),
    ("price", "价格"),
]
SCOPE_OPTIONS = [
    ("session", "本会话（当前工作区）"),
    ("lifetime", "累计（所有工作区）"),
]
FORMAT_OPTIONS = [
    ("auto", "自动（万/亿）"),
    ("km", "K / M"),
    ("full", "完整数字"),
]

DEFAULT_FIELDS = ["input", "output", "cacheRead", "price"]
DEFAULT_SCOPES = ["session", "lifetime"]
DEFAULT_FORMAT = "auto"


class TokenCostDialog(QDialog):
    """勾选要显示的数据 + 数字格式。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Token 花费 · 显示设置")
        self.setMinimumWidth(320)
        self._build()
        self._load()

    # ---------------------------------------------------------------- UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        hint = QLabel("选择要在「Token 花费统计」气泡里显示哪些数据：")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 显示字段
        fields_box = QGroupBox("显示数据")
        fields_layout = QVBoxLayout(fields_box)
        self._field_checks: dict[str, QCheckBox] = {}
        for key, label in FIELD_OPTIONS:
            cb = QCheckBox(label)
            fields_layout.addWidget(cb)
            self._field_checks[key] = cb
        root.addWidget(fields_box)

        # 口径
        scope_box = QGroupBox("统计范围")
        scope_layout = QHBoxLayout(scope_box)
        self._scope_checks: dict[str, QCheckBox] = {}
        for key, label in SCOPE_OPTIONS:
            cb = QCheckBox(label)
            scope_layout.addWidget(cb)
            self._scope_checks[key] = cb
        root.addWidget(scope_box)

        # 数字格式
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("数字格式："))
        self._format_combo = QComboBox()
        self._format_combo.addItems([label for _, label in FORMAT_OPTIONS])
        self._format_combo.setMinimumWidth(150)
        fmt_row.addWidget(self._format_combo)
        fmt_row.addStretch(1)
        root.addLayout(fmt_row)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        root.addLayout(btn_row)

    # ---------------------------------------------------------------- 读写
    def _load(self) -> None:
        fields = self.cfg.get("token_display_fields", DEFAULT_FIELDS)
        scopes = self.cfg.get("token_display_scopes", DEFAULT_SCOPES)
        fmt = self.cfg.get("token_display_format", DEFAULT_FORMAT)
        for key, cb in self._field_checks.items():
            cb.setChecked(key in fields)
        for key, cb in self._scope_checks.items():
            cb.setChecked(key in scopes)
        for i, (fkey, _label) in enumerate(FORMAT_OPTIONS):
            if fkey == fmt:
                self._format_combo.setCurrentIndex(i)
                break

    def _on_save(self) -> None:
        fields = [key for key, cb in self._field_checks.items() if cb.isChecked()]
        scopes = [key for key, cb in self._scope_checks.items() if cb.isChecked()]
        fmt = FORMAT_OPTIONS[self._format_combo.currentIndex()][0]
        self.cfg.set("token_display_fields", fields)
        self.cfg.set("token_display_scopes", scopes)
        self.cfg.set("token_display_format", fmt)
        self.cfg.save()
        self.accept()
