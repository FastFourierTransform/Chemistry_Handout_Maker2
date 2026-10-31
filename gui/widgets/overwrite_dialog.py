# -*- coding: utf-8 -*-
"""覆盖确认弹窗：开跑前发现同名产物已存在时，先让用户决定。

为什么不用 QMessageBox：产物路径往往又长又多，QMessageBox 的信息文本不能滚动、
自定义按钮也套不上主按钮样式；这里用普通 QDialog，清单只列文件名（全路径进
tooltip），默认动作是「跳过已有文件」。

选中结果放在 ``choice``：``"skip"`` / ``"overwrite"`` / ``"cancel"``。
"""

from __future__ import annotations

import html
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout


class OverwriteDialog(QDialog):
    """列出会被覆盖的产物，默认动作是「跳过已有文件」。"""

    MAX_VISIBLE = 6

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self.choice = "cancel"
        self.setWindowTitle("会覆盖已有讲义")
        self.setMinimumWidth(540)

        title = QLabel(f"有 {len(paths)} 份输出已经存在")
        title.setObjectName("sectionTitle")

        # 手动断行：中文自动断行会从「跳过已有文件」中间劈开，难看且易读错
        body = QLabel(
            "重新转换会把它们整份覆盖，包括你手改过的内容。\n"
            "已经定稿的讲义，请选「跳过已有文件」。")
        body.setWordWrap(True)

        names = [os.path.basename(path) for path in paths]
        shown = names[:self.MAX_VISIBLE]
        if len(names) > len(shown):
            shown = shown + [f"…以及另外 {len(names) - len(shown)} 份"]
        listing = QLabel('<div style="line-height:1.65;">'
                         + "<br>".join(html.escape(name) for name in shown)
                         + "</div>")
        listing.setObjectName("pathBox")
        listing.setTextFormat(Qt.TextFormat.RichText)
        listing.setToolTip("\n".join(paths))
        listing.setWordWrap(True)

        buttons = QDialogButtonBox()
        skip_btn = buttons.addButton("跳过已有文件并开始", QDialogButtonBox.ButtonRole.AcceptRole)
        skip_btn.setObjectName("primaryButton")
        over_btn = buttons.addButton("仍然覆盖", QDialogButtonBox.ButtonRole.DestructiveRole)
        over_btn.setObjectName("dangerButton")
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        skip_btn.clicked.connect(lambda: self._choose("skip"))
        over_btn.clicked.connect(lambda: self._choose("overwrite"))
        buttons.rejected.connect(lambda: self._choose("cancel"))
        skip_btn.setDefault(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(body)
        layout.addWidget(listing)
        layout.addWidget(buttons)

    def _choose(self, choice: str):
        self.choice = choice
        self.accept()
