# -*- coding: utf-8 -*-
"""运行日志面板：可折叠、彩色分级、自动滚动。

日志来源是工作线程的 log_line 信号（底层脚本 stdout 的行事件），
GUI 线程只负责 append —— 面板本身不 import 任何底层脚本。
"""

from __future__ import annotations

import html

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

_LEVEL_COLORS = {
    # 明亮主题配色（日志面板底色为近白）
    "info": "#24292f",
    "ok": "#1a7f37",
    "warn": "#9a6700",
    "error": "#cf222e",
}


class LogPanel(QWidget):
    """底部日志面板。folded 信号用于主窗口保存/恢复折叠状态。"""

    folded = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._folded = False

        self.title = QLabel("运行日志")
        self.title.setObjectName("groupHeading")   # 结构标签 12/700（与分组框标题同档）
        self.auto_scroll = QCheckBox("自动滚动")
        self.auto_scroll.setChecked(True)
        self.clear_btn = QPushButton("清空")
        self.fold_btn = QPushButton("折叠 ▾")
        self.fold_btn.setFixedWidth(72)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.auto_scroll)
        header.addWidget(self.clear_btn)
        header.addWidget(self.fold_btn)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setMaximumBlockCount(8000)
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.setObjectName("logView")
        self.body.setPlaceholderText(
            "日志区：批量转换的底层输出会实时滚动到这里（每页进度、重试、体检结果 …）")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(header)
        layout.addWidget(self.body, 1)

        self.clear_btn.clicked.connect(self.body.clear)
        self.fold_btn.clicked.connect(self.toggle_folded)
        self.auto_scroll.toggled.connect(self._on_auto_scroll)

    # ------------------------------------------------------------------ API
    def append(self, level: str, text: str) -> None:
        color = _LEVEL_COLORS.get(level, _LEVEL_COLORS["info"])
        escaped = html.escape(text).replace("\t", "    ")
        self.body.appendHtml(
            f'<span style="color:{color}; white-space:pre;">{escaped}</span>')
        if self.auto_scroll.isChecked():
            scrollbar = self.body.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _on_auto_scroll(self, checked: bool) -> None:
        if checked:
            self.body.verticalScrollBar().setValue(
                self.body.verticalScrollBar().maximum())

    def toggle_folded(self) -> None:
        self.set_folded(not self._folded)

    def set_folded(self, folded: bool) -> None:
        self._folded = folded
        self.body.setVisible(not folded)
        self.fold_btn.setText("展开 ▴" if folded else "折叠 ▾")
        self.folded.emit(folded)

    @property
    def is_folded(self) -> bool:
        return self._folded
