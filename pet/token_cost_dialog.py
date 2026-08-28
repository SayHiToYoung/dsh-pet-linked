# -*- coding: utf-8 -*-
"""
Token 花费显示设置（二次开发新增）—— 让用户自己勾选要看什么数据。

可配置项（存 config.json，键名 token_*）：
  token_display_fields : ["input","output","cacheRead","price"]  显示哪些字段
  token_display_scopes : ["session","lifetime"]                  显示哪个口径
  token_display_format : "auto" | "km" | "full"                  数字格式
  token_pricing        : {模型前缀: {"peak": {...}, "off": {...}}}  用户覆盖价格表（USD/百万）
  token_peak_hours     : [[起, 止], ...]（UTC 小时）              用户覆盖高峰窗口（空=官方窗口）
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from . import token_cost as token_cost_mod

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

# 价格表可编辑行：模型前缀 → 界面标签（前缀按最长匹配命中实际模型）
PRICE_ROWS = [
    ("deepseek-v4-flash", "v4-flash"),
    ("deepseek-v4-pro", "v4-pro"),
    ("deepseek-v4-flash-vision-exp", "v4-flash-vision-exp"),
    ("deepseek-chat", "deepseek-chat（默认档）"),
    ("deepseek-reasoner", "deepseek-reasoner"),
]
# 列：界面列标题 → 定价键 → 档位
PRICE_COLS = [
    ("输入 · 高峰", "input", "peak"),
    ("输入 · 低谷", "input", "off"),
    ("缓存 · 高峰", "cacheRead", "peak"),
    ("缓存 · 低谷", "cacheRead", "off"),
    ("输出 · 高峰", "output", "peak"),
    ("输出 · 低谷", "output", "off"),
]
DEFAULT_PEAK_HOURS_TEXT = "1-4,6-10"


def _parse_hours(text: str) -> list:
    """'1-4,6-10' → [[1,4],[6,10]]；空 → []（官方窗口）。"""
    out = []
    for seg in str(text or "").replace("，", ",").split(","):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split("-")
        if len(parts) == 2:
            try:
                s, e = int(parts[0]), int(parts[1])
                if 0 <= s < e <= 24:
                    out.append([s, e])
            except ValueError:
                continue
    return out


def _format_hours(windows) -> str:
    """[[1,4],[6,10]] → '1-4,6-10'。"""
    if not windows:
        return ""
    return ",".join(f"{s}-{e}" for s, e in windows)


class TokenCostDialog(QDialog):
    """勾选要显示的数据 + 数字格式 + 编辑价格表与高峰时段。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Token 花费 · 显示设置")
        self.setMinimumWidth(820)
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

        # 价格表与高峰时段（可编辑，保存后立即生效）
        price_box = QGroupBox("价格表（USD / 百万 tokens）· 高峰时段可编辑")
        price_layout = QVBoxLayout(price_box)
        price_hint = QLabel("官方调价后可直接在此修改；保存立即生效，无需重启桌宠。")
        price_hint.setWordWrap(True)
        price_layout.addWidget(price_hint)
        self._price_table = QTableWidget(len(PRICE_ROWS), len(PRICE_COLS))
        self._price_table.setHorizontalHeaderLabels([c[0] for c in PRICE_COLS])
        self._price_table.setVerticalHeaderLabels([r[1] for r in PRICE_ROWS])
        self._price_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        price_layout.addWidget(self._price_table)
        hours_row = QHBoxLayout()
        hours_row.addWidget(QLabel("高峰时段（UTC 小时，格式 1-4,6-10）："))
        self._hours_edit = QLineEdit()
        self._hours_edit.setPlaceholderText(DEFAULT_PEAK_HOURS_TEXT)
        hours_row.addWidget(self._hours_edit, 1)
        reset = QPushButton("恢复内置默认")
        reset.clicked.connect(self._reset_prices)
        hours_row.addWidget(reset)
        price_layout.addLayout(hours_row)
        root.addWidget(price_box)

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

        # 价格表：有覆盖则显示合并后的当前价，否则显示内置价
        overrides = self.cfg.get("token_pricing") or {}
        hours = self.cfg.get("token_peak_hours") or []
        self._hours_edit.setText(_format_hours(hours))
        for row, (prefix, _label) in enumerate(PRICE_ROWS):
            base_peak, base_off = token_cost_mod.pricing_both_builtin(prefix)
            pair = overrides.get(prefix) if isinstance(overrides.get(prefix), dict) else {}
            peak = {**base_peak, **pair.get("peak", {})}
            off = {**base_off, **pair.get("off", {})}
            for col, (_hdr, key, tier) in enumerate(PRICE_COLS):
                val = (peak if tier == "peak" else off).get(key, 0.0)
                self._price_table.setItem(row, col, QTableWidgetItem(f"{val:g}"))

    def _collect_pricing(self) -> dict:
        """从表格读回完整价格表：{前缀: {"peak": {...}, "off": {...}}}。"""
        pricing = {}
        for row, (prefix, _label) in enumerate(PRICE_ROWS):
            peak, off = {}, {}
            for col, (_hdr, key, tier) in enumerate(PRICE_COLS):
                item = self._price_table.item(row, col)
                raw = item.text().strip() if item and item.text() else ""
                try:
                    val = max(0.0, float(raw))
                except ValueError:
                    val = 0.0
                (peak if tier == "peak" else off)[key] = val
            pricing[prefix] = {"peak": peak, "off": off}
        return pricing

    def _reset_prices(self) -> None:
        """把价格表与高峰时段恢复为内置默认（还需点保存才会写入）。"""
        for row, (prefix, _label) in enumerate(PRICE_ROWS):
            peak, off = token_cost_mod.pricing_both_builtin(prefix)
            for col, (_hdr, key, tier) in enumerate(PRICE_COLS):
                val = (peak if tier == "peak" else off).get(key, 0.0)
                self._price_table.setItem(row, col, QTableWidgetItem(f"{val:g}"))
        self._hours_edit.setText(DEFAULT_PEAK_HOURS_TEXT)

    def _on_save(self) -> None:
        fields = [key for key, cb in self._field_checks.items() if cb.isChecked()]
        scopes = [key for key, cb in self._scope_checks.items() if cb.isChecked()]
        fmt = FORMAT_OPTIONS[self._format_combo.currentIndex()][0]
        pricing = self._collect_pricing()
        peak_hours = _parse_hours(self._hours_edit.text())
        self.cfg.set("token_display_fields", fields)
        self.cfg.set("token_display_scopes", scopes)
        self.cfg.set("token_display_format", fmt)
        self.cfg.set("token_pricing", pricing)
        self.cfg.set("token_peak_hours", peak_hours)
        self.cfg.save()
        token_cost_mod.set_overrides(pricing, peak_hours)  # 立即生效，无需重启
        self.accept()