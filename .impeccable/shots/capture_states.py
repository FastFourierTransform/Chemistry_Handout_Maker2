# -*- coding: utf-8 -*-
"""工作用截图脚本 2：渲染「状态」相关的界面（空态 / 混合结果 / 暂停 / 终止 / 覆盖确认）。

    python .impeccable/shots/capture_states.py before|after

不是交付物；polish 流程用它目检状态与文案。
"""

from __future__ import annotations

import os
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.app import STYLE_SHEET  # noqa: E402
from gui.core.config_store import ConfigStore  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

SHOT_DIR = os.path.join(_ROOT, ".impeccable", "shots")
_STATE_DIR = os.path.join(SHOT_DIR, "_state")
_TAG = sys.argv[1] if len(sys.argv) > 1 else "shot"


def settle(ms: int = 150) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()
    for _ in range(3):
        QApplication.processEvents()


def save(widget, name: str) -> None:
    settle()
    path = os.path.join(SHOT_DIR, f"{name}.png")
    widget.grab().save(path)
    print("saved", path)


def main() -> int:
    os.makedirs(_STATE_DIR, exist_ok=True)
    app = QApplication(sys.argv)
    app.setFont(theme.ui_font())
    app.setStyleSheet(STYLE_SHEET)

    cfg = os.path.join(_STATE_DIR, "cfg.json")
    if os.path.exists(cfg):
        os.remove(cfg)
    store = ConfigStore(cfg)
    win = MainWindow(store)
    win.resize(1280, 800)
    win.show()
    settle()

    # ---- 空态 ----
    save(win, f"{_TAG}-state-empty")

    # ---- 混合状态：成功 / 失败 / 跳过 / 等待 / 准备中 ----
    src = os.path.join(_ROOT, "test1.pdf")
    names = ["3-1 氯及其化合物.pdf", "3-2 钠及其化合物.pdf",
             "3-3 铁及其化合物.pdf", "3-4 铝及其化合物.pdf"]
    paths = []
    for name in names:
        dest = os.path.join(_STATE_DIR, name)
        if not os.path.exists(dest):
            shutil.copyfile(src, dest)
        paths.append(dest)
    win.file_panel.add_paths(paths)
    settle()
    tasks = win.file_panel.tasks()
    win.file_panel.update_task({
        "uid": tasks[0].uid, "status": "已完成", "pages": 15, "seconds": 42.0,
        "output": r"D:\备课\讲义\3-1 氯及其化合物.md",
        "message": "完成（42s）；体检 12 项通过"})
    win.file_panel.update_task({
        "uid": tasks[1].uid, "status": "失败", "pages": 12,
        "message": "ConvertError: 没找到 LibreOffice 或 PowerPoint，PPT 无法转 PDF"})
    win.file_panel.update_task({
        "uid": tasks[2].uid, "status": "已跳过", "pages": 14,
        "output": r"D:\备课\讲义\3-3 铁及其化合物.md",
        "message": "已存在产物，跳过"})
    win.file_panel.update_task({
        "uid": tasks[3].uid, "status": "准备中", "pages": 0,
        "message": "等待转成 PDF …"})
    save(win, f"{_TAG}-state-mixed")

    # ---- 选中行（两行文字的选中态底色） ----
    win.file_panel.table.selectRow(1)
    save(win, f"{_TAG}-state-selected")

    # ---- 运行中：转换中 + 蓝色进度 ----
    win.file_panel.update_task({
        "uid": tasks[3].uid, "status": "转换中", "pages": 15,
        "message": "[2/4] 转换中 ..."})
    win.file_panel.update_task({
        "uid": tasks[0].uid, "status": "等待中", "pages": 15, "output": ""})
    win._n_files = len(tasks)
    win._done_files = 1
    win.progress.setValue(280)
    win._worker = object()
    win._batch_started = time.monotonic() - 30
    win._set_phase("running")
    win._set_running(True)
    save(win, f"{_TAG}-state-running")
    win._set_running(False)
    win._set_phase("idle")
    win._worker = None
    win.progress.setValue(0)

    # ---- 暂停 / 终止（相位 + 文案） ----
    win.file_panel.update_task({"uid": tasks[3].uid, "status": "等待中", "message": ""})
    win._set_running(True)
    win._n_files = len(tasks)
    win._done_files = 2
    win._total_pages = 60
    win._done_pages = 32
    win._page_in_file = 4
    win.progress.setValue(560)
    win._worker = object()      # 只为让 _set_phase 走「会重算文案」的分支
    win._batch_started = time.monotonic() - 96
    win._set_phase("paused")
    win.pause_btn.setText("继续")
    win.pause_btn.setObjectName("primaryButton")
    win.pause_btn.style().unpolish(win.pause_btn)
    win.pause_btn.style().polish(win.pause_btn)
    save(win, f"{_TAG}-state-paused")

    win._set_phase("stopping")
    win.stop_btn.setEnabled(False)
    win.stop_btn.setText("正在终止 …")
    win.pause_btn.setEnabled(False)
    save(win, f"{_TAG}-state-stopping")
    win._set_phase("idle")
    win._worker = None

    # ---- 覆盖确认弹窗 ----
    from gui.widgets.overwrite_dialog import OverwriteDialog

    existing = []
    for name in names[:3]:          # 造 3 份已存在的产物
        path = os.path.join(_STATE_DIR, name[:-4] + ".md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("## [3-1] 氯及其化合物\n")
        existing.append(path)
    dialog = OverwriteDialog(existing, win)
    dialog.show()
    settle()
    dialog.grab().save(os.path.join(SHOT_DIR, f"{_TAG}-state-overwrite.png"))
    dialog.close()
    print("saved overwrite dialog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
