# -*- coding: utf-8 -*-
"""工作用截图脚本：渲染 GUI 的配置面（主窗口 / 配置弹窗）到 .impeccable/shots/。

    python .impeccable/shots/capture.py before

不是交付物的一部分；impeccable 的 typeset 流程用它对比「改前 / 改后」。
"""

from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleFactory  # noqa: E402

from gui.app import STYLE_SHEET  # noqa: E402
from gui.core.config_store import ConfigStore  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from gui.widgets.profile_dialog import ProfileDialog  # noqa: E402

SHOT_DIR = os.path.join(_ROOT, ".impeccable", "shots")


def settle(ms: int = 150) -> None:
    """让布局/分栏/滚动区稳定下来再截图（单次 processEvents 拿到的可能是中间状态）。"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    for _ in range(3):
        QApplication.processEvents()


def save(widget, name: str) -> str:
    settle()
    path = os.path.join(SHOT_DIR, f"{name}.png")
    widget.grab().save(path)
    print("saved", path)
    return path


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "shot"
    os.makedirs(SHOT_DIR, exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setFont(QFont("Microsoft YaHei UI", 9.5))
    app.setStyleSheet(STYLE_SHEET)

    store = ConfigStore(os.path.join(tempfile.mkdtemp(prefix="shot_"), "cfg.json"))
    win = MainWindow(store)
    win.resize(1280, 800)
    win.show()
    QApplication.processEvents()

    save(win, f"{tag}-main-empty")

    test_pdf = os.path.join(_ROOT, "test1.pdf")
    if os.path.isfile(test_pdf):
        win.file_panel.add_paths([test_pdf])
        QApplication.processEvents()
        save(win, f"{tag}-main-loaded")

    # 配置面 1：模型 / API 配置面板（右侧，含预览）
    win.model_panel.setFocus()
    save(win.model_panel, f"{tag}-model-panel")

    # 配置面 2：新建配置弹窗（GLM 默认）——按真实最小宽度 620 渲染，不人为拉宽
    dlg = ProfileDialog(None, win)
    dlg.setWindowFlag(Qt.WindowType.Widget)  # 当作普通 widget 渲染，避免顶层窗口 grab 黑屏
    dlg.show()
    QApplication.processEvents()
    save(dlg, f"{tag}-profile-new-glm")

    # 配置面 3：编辑 OpenAI 兼容配置（必填项 / 提示文案不同）
    dlg2 = ProfileDialog(store.profiles[2], win)
    dlg2.setWindowFlag(Qt.WindowType.Widget)
    dlg2.show()
    QApplication.processEvents()
    save(dlg2, f"{tag}-profile-openai")

    # 最小尺寸下的主窗口（1080 x 640）
    win.resize(1080, 640)
    QApplication.processEvents()
    save(win, f"{tag}-main-min")

    return 0


if __name__ == "__main__":
    sys.exit(main())
