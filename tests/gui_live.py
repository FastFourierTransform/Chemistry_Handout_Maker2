# -*- coding: utf-8 -*-
"""GUI 真机联调（真发一次 API 请求，花额度）：验证「真实转换」在 GUI 工作线程里
端到端跑通。默认不跑 —— 需显式设置环境变量：

    $env:HM_GUI_LIVE="1"; python tests/gui_live.py

只用一页小 PDF（跟 handout_maker.py --check 的探针同一量级），请求量 ≈ 1 次。
"""

from __future__ import annotations

import os
import sys
import tempfile

if os.environ.get("HM_GUI_LIVE") != "1":
    print("跳过：真机联调需要显式设置 HM_GUI_LIVE=1（会真实调用 API）")
    sys.exit(0)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import pymupdf  # noqa: E402
from gui.app import STYLE_SHEET  # noqa: E402
from gui.core.config_store import ConfigStore  # noqa: E402
from gui.core.models import TaskStatus  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

tmp = tempfile.mkdtemp(prefix="gui_live_")
pdf = os.path.join(tmp, "probe.pdf")
doc = pymupdf.open()
page = doc.new_page(width=300, height=140)
page.insert_text((20, 60), "H2O  NaCl  Fe3+  Na2CO3", fontsize=20)
doc.save(pdf)
doc.close()


def wait_until(predicate, timeout_ms=300_000):
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)

    def poll():
        if predicate():
            loop.quit()
        else:
            QTimer.singleShot(200, poll)

    timer.start(timeout_ms)
    QTimer.singleShot(200, poll)
    loop.exec()
    return predicate()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE_SHEET)
    store = ConfigStore(os.path.join(tmp, "cfg.json"))
    win = MainWindow(store)
    win.show()

    win.outdir_edit.setText(os.path.join(tmp, "out"))
    win.file_panel.add_paths([pdf])
    task = next(iter(win.file_panel.tasks()))
    print("开始真实转换（1 页小图，≈1 次请求）...")
    win._start_batch(dry_run=False)

    done = wait_until(
        lambda: (win._worker is not None and win._worker.drain_events(5000),
                 win._worker is None)[1],
        timeout_ms=300_000)
    if win._worker is not None:
        win._worker.drain_events(10_000)

    status = task.status
    out_md = os.path.join(tmp, "out", "probe.md")
    print(f"任务状态：{status.value}")
    print(f"产物存在：{os.path.isfile(out_md)}")
    if os.path.isfile(out_md):
        text = open(out_md, encoding="utf-8").read()
        print("产物片段：", text[:200].replace("\n", " ⏎ "))
    log = win.log_panel.body.toPlainText()
    print("日志尾部：", log[-400:].replace("\n", " ⏎ "))

    ok = (done and status == TaskStatus.SUCCESS and os.path.isfile(out_md))
    print("\n真机联调", "通过 ✅" if ok else "失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
