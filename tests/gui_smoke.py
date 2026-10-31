# -*- coding: utf-8 -*-
"""GUI 离线冒烟测试（不联网、不花额度）。

    python tests/gui_smoke.py

覆盖：
1. 主窗口能在 offscreen 平台实例化（配置读写、面板、主题）；
2. 导入 PDF → 预演（dry-run）全链路：工作线程 + stdout 捕获 + 日志面板；
3. 导入伪 PPTX → PrepWorker 失败路径（状态变「失败」且带解决提示）；
4. 真转换线程的「终止」链路：在底层 print 点触发协作式取消（用小页 PDF，
   终止落在渲染阶段，不会触网）。

退出码 0 = 全部通过。
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.app import STYLE_SHEET  # noqa: E402
from gui.converters import ppt_to_pdf  # noqa: E402
from gui.core.config_store import ConfigStore  # noqa: E402
from gui.core.models import ApiProfile, ConvertOptions, TaskStatus  # noqa: E402
from gui.core.worker import ConversionWorker  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

_failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [通过] {name}")
    else:
        _failures.append(name)
        print(f"  [失败] {name} {detail}")


def wait_until(app, predicate, timeout_ms=30000):
    """跑 Qt 事件循环直到 predicate 为真或超时。"""
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


def make_tiny_pdf(path, pages=8):
    """现场造一个小 PDF（渲染阶段就有多次 print，让终止测试不触网）。"""
    import pymupdf
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=100)
        page.insert_text((20, 50), "H2O NaCl Na2CO3")
    doc.save(path)
    doc.close()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE_SHEET)

    tmp = tempfile.mkdtemp(prefix="gui_smoke_")
    store = ConfigStore(os.path.join(tmp, "app_config.json"))
    win = MainWindow(store)
    win.show()

    # ---- 1. 窗口实例化 + 空面板 -------------------------------------------------
    print("① 主窗口实例化")
    check("配置店可读可写", store.load()["schema_version"] == 1)
    check("初始任务列表为空", not win.file_panel.tasks())
    check("默认内置三条配置", win.model_panel.combo.count() == 3)
    check("含 OpenAI 兼容（DeepSeek）引擎配置",
          any(p.engine == "openai" for p in win.model_panel._store.profiles))

    # ---- 2. 导入真实 PDF + 预演 --------------------------------------------------
    print("② 导入 PDF + 预演（dry-run）")
    test_pdf = os.path.join(_ROOT, "test1.pdf")
    if os.path.isfile(test_pdf):
        added = win.file_panel.add_paths([test_pdf])
        check("导入 1 个 PDF", added == 1)
        win._start_batch(dry_run=True)
        done = wait_until(app, lambda: win._worker is None)
        log_text = win.log_panel.body.toPlainText()
        check("预演批次结束", done, log_text[-500:])
        check("日志含「预演结束」", "预演结束" in log_text)
        check("预演不调用 API（无 正在处理第 行）", "正在处理第" not in log_text)
    else:
        check("跳过（test1.pdf 不存在）", True)

    # ---- 3. 伪 PPTX → PrepWorker 失败路径 ----------------------------------------
    print("③ PPT 预处理失败路径")
    # 测试环境里禁止真去启动 LibreOffice / PowerPoint（机器差异会毁掉确定性）
    ppt_to_pdf._via_soffice = lambda src, dest: (_ for _ in ()).throw(
        ppt_to_pdf.ConvertError("测试环境禁用 LibreOffice"))
    ppt_to_pdf._via_com = lambda src, dest: (_ for _ in ()).throw(
        ppt_to_pdf.ConvertError("测试环境禁用 PowerPoint COM"))
    fake_pptx = os.path.join(tmp, "fake.pptx")
    with open(fake_pptx, "wb") as f:
        f.write(b"PK\x03\x04" + b"\x00" * 128)
    win.file_panel.add_paths([fake_pptx])
    failed = wait_until(
        app,
        lambda: any(t.source == fake_pptx and t.status == TaskStatus.FAILED
                    for t in win.file_panel.tasks()))
    check("伪 PPTX 标记为「失败」", failed)
    task = next(t for t in win.file_panel.tasks() if t.source == fake_pptx)
    check("失败信息带解决提示", "LibreOffice" in task.message or "pywin32" in task.message,
          task.message)

    # ---- 4. 真转换线程的「终止」链路（渲染完成即取消，绝不触网）--------------------
    print("④ 协作式终止（小 PDF，取消落在首个网络请求之前）")
    tiny_pdf = os.path.join(tmp, "tiny.pdf")
    make_tiny_pdf(tiny_pdf)
    win.file_panel.add_paths([tiny_pdf])
    task = next(t for t in win.file_panel.tasks() if t.source == tiny_pdf)
    profile = win.model_panel.current_profile()
    outdir = os.path.join(tmp, "out")

    class CancelOnFirstPage(ConversionWorker):
        """看到第一个「正在处理第 N 页」就取消 —— 该回调发生在**渲染完成后、
        首次发请求前**、且在工作线程自己的调用栈里，因此下一次底层
        print（call_glm 的 “-> 模型 … 第 1 次尝试”）会立刻抛
        RequestCancelled，全程零网络请求、100% 确定。"""

        def _on_page_event(self, page, total):
            if page == 1:
                self.cancel()

    worker = CancelOnFirstPage()
    worker.configure([task], profile, ConvertOptions(outdir=outdir), dry_run=False)

    updated = {}
    log_lines = []
    worker.log_line.connect(lambda lv, t: log_lines.append(t))
    worker.file_updated.connect(lambda d: updated.update({d.get("uid"): d}))
    worker.start()

    def drained_and_done():
        worker.drain_events(5000)   # 主线程排空日志队列（生产代码里由定时器做）
        return not worker.isRunning()

    finished = wait_until(app, drained_and_done)
    worker.drain_events(10_000)
    check("工作线程正常退出", finished)
    check("任务状态 = 已取消", updated.get(task.uid, {}).get("status")
          == TaskStatus.CANCELLED.value, str(updated))
    check("日志含「[终止]」", any("终止" in ln for ln in log_lines))
    check("未触发网络调用（无模型尝试日志）",
          not any("次尝试" in ln for ln in log_lines))
    out_md = os.path.join(outdir, "tiny.md")
    check("没有产出正式 .md", not os.path.exists(out_md))

    # ---- 5. OpenAI 兼容引擎（DeepSeek）配置校验：缺 Key 必须快速失败、不触网 --------
    print("⑤ OpenAI 兼容引擎配置校验（缺 Key 快速失败）")
    ds_profile = ApiProfile(name="ds", engine="openai",
                            base_url="https://api.deepseek.com/v1",
                            api_key="", models="deepseek-chat")
    w5 = ConversionWorker()
    w5.configure([task], ds_profile, ConvertOptions(outdir=os.path.join(tmp, "o5")),
                 dry_run=False)
    batch5 = {}
    lines5 = []
    w5.batch_finished.connect(lambda s: batch5.update(s))
    w5.log_line.connect(lambda lv, t: lines5.append(t))
    w5.start()
    ok5 = wait_until(
        app, lambda: (w5.drain_events(5000), not w5.isRunning())[1], 20000)
    w5.drain_events(10_000)
    check("工作线程正常退出（配置错误快速失败）", ok5)
    check("错误信息提示需要 API Key",
          "API Key" in batch5.get("error", ""), str(batch5))
    check("未触网（无任何模型尝试日志）",
          not any("次尝试" in ln for ln in lines5), str(lines5[-3:]))

    # ---- 收尾 -------------------------------------------------------------------
    win.close()
    print("\n" + ("全部通过 ✅" if not _failures else f"{len(_failures)} 项失败 ❌"))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
