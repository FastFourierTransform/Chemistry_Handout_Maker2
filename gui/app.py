# -*- coding: utf-8 -*-
"""应用入口：QApplication + Fusion 风格 + 明亮主题 + 主窗口。

主题只动样式表，不改任何控件逻辑；字体族、字阶、字色与行距的令牌在
``gui/theme.py``（唯一事实来源），这里只负责装配。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

from gui import theme
from gui.core.config_store import ConfigStore
from gui.main_window import MainWindow

APP_NAME = "Handout-Maker2 · 化学讲义批量工厂"

# 整份 QSS 由排版令牌生成（见 gui/theme.py 的字阶表）
STYLE_SHEET = theme.stylesheet()




def _make_icon() -> QIcon:
    """现场画一个简单图标（渐变底 + 讲义字样），避免外部资源文件。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#2f6fed"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Microsoft YaHei UI", 20, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "讲义")
    painter.end()
    return QIcon(pixmap)


def main(argv=None) -> int:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Handout-Maker2")
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setFont(theme.ui_font())      # 与 QSS 同一套字阶（原 9.5pt 被截成 9pt 且被 QSS 覆盖）
    app.setStyleSheet(STYLE_SHEET)
    app.setWindowIcon(_make_icon())

    store = ConfigStore()
    window = MainWindow(store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
