# -*- coding: utf-8 -*-
"""生成 GUI 预览截图：offscreen 实例化主窗口 → 导入示例 PDF → 预演 → 截屏。

    python tests/grab_preview.py        # 产物：docs/gui_preview.png
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.app import STYLE_SHEET  # noqa: E402
from gui.core.config_store import ConfigStore  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402


def wait_until(predicate, timeout_ms=20000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    def poll():
        if predicate():
            loop.quit()
        else:
            QTimer.singleShot(100, poll)

    timer.start(timeout_ms)
    QTimer.singleShot(100, poll)
    loop.exec()
    return predicate()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE_SHEET)
    store = ConfigStore(os.path.join(tempfile.mkdtemp(prefix="grab_"), "cfg.json"))
    win = MainWindow(store)
    win.resize(1280, 800)
    win.show()

    test_pdf = os.path.join(_ROOT, "test1.pdf")
    if os.path.isfile(test_pdf):
        win.file_panel.add_paths([test_pdf])
        # 展示第三条「OpenAI 兼容（DeepSeek）」配置的预览面板
        if win.model_panel.combo.count() >= 3:
            win.model_panel.combo.setCurrentIndex(2)
        win._start_batch(dry_run=True)
        wait_until(lambda: win._worker is None, 20000)

    # 再等一拍，让日志定时器把剩余行排空进面板
    app.processEvents()
    QTimer.singleShot(300, app.quit)
    app.exec()

    out = os.path.join(_ROOT, "docs", "gui_preview.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    win.grab().save(out)
    print("已保存预览图：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
