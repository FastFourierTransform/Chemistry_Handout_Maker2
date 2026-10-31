# -*- coding: utf-8 -*-
"""主窗口：文件面板 + 模型面板 + 日志面板 + 选项栏 + 进度/执行控制。

职责只有「编排」：
* 把三个面板的信号接起来（文件导入 → PPT 预处理 → 状态刷新 → 日志）；
* 把「开始转换」变成一次 ConversionWorker（QThread）的启动，
  工作线程的日志/进度/状态信号再喂回日志面板与进度条；
* 持久化窗口几何信息与转换选项（ConfigStore）；
* 拖拽导入、菜单、关闭时的工作线程善后。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, QByteArray, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from gui.core import bridge
from gui.core.config_store import ConfigStore, DEFAULT_PATH
from gui.core.models import ConvertOptions, TaskStatus
from gui.core.worker import CheckWorker, ConversionWorker, PrepWorker
from gui.widgets.file_panel import FilePanel
from gui.widgets.log_panel import LogPanel
from gui.widgets.model_panel import ModelPanel
from gui.widgets.overwrite_dialog import OverwriteDialog


def _fmt_secs(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class MainWindow(QMainWindow):
    def __init__(self, store: ConfigStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("Handout-Maker2 · 化学讲义批量工厂")
        self.setMinimumSize(1080, 640)
        self.setAcceptDrops(True)

        # 工作线程引用（防止被 GC；同一时刻最多一个转换批次）
        self._worker: ConversionWorker | None = None
        self._prep_worker: PrepWorker | None = None
        self._prep_queue: list = []
        self._check_worker: CheckWorker | None = None
        self._batch_started = 0.0
        self._total_pages = 0
        self._done_pages = 0
        self._page_in_file = 0
        self._done_files = 0
        self._n_files = 0
        self._phase = "idle"      # idle / running / pausing / paused / stopping

        # 日志/页进度经工作线程的队列 → 本定时器在主线程排空 → 变成 Qt 信号。
        # 信号绝不在底层 print 的调用栈里发射（见 worker 模块 docstring 的血泪史）。
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(100)
        self._drain_timer.timeout.connect(self._drain_worker)

        self._build_ui()
        self._build_menu()
        self._wire()
        self._restore_ui_state()
        self._warn_missing_deps()
        self._refresh_controls()   # 空列表时主操作就是不可点的（此前启动即高亮）

    # ================================================================ UI 构建
    def _build_ui(self):
        # ---- 左右两个工作面板 ------------------------------------------------
        self.file_panel = FilePanel()
        self.model_panel = ModelPanel(self.store)

        work_splitter = QSplitter(Qt.Orientation.Horizontal)
        work_splitter.addWidget(self.file_panel)
        work_splitter.addWidget(self.model_panel)
        work_splitter.setStretchFactor(0, 3)
        work_splitter.setStretchFactor(1, 2)
        # 首次显示的比例（配置里有保存值时会被 _restore_ui_state 覆盖）：
        # 右侧配置面板是窄栏，按 60/40 分，预览卡才放得下整条模型链
        work_splitter.setSizes([3, 2])
        work_splitter.setChildrenCollapsible(False)
        self.work_splitter = work_splitter

        # ---- 日志面板 + 竖直分栏 ----------------------------------------------
        self.log_panel = LogPanel()
        body_splitter = QSplitter(Qt.Orientation.Vertical)
        body_splitter.addWidget(work_splitter)
        body_splitter.addWidget(self.log_panel)
        body_splitter.setStretchFactor(0, 3)
        body_splitter.setStretchFactor(1, 1)
        body_splitter.setChildrenCollapsible(False)
        self.body_splitter = body_splitter

        # ---- 转换选项（对应 batch.py 的批量参数）-------------------------------
        options_group = QGroupBox("转换选项")
        options_grid = QVBoxLayout(options_group)
        options_grid.setContentsMargins(12, 10, 12, 12)
        options_grid.setSpacing(9)

        def option_label(text: str) -> QLabel:
            """选项标签：13px、比输入值暗一档（见 gui/theme.py 的字阶表）。"""
            label = QLabel(text)
            label.setObjectName("fieldLabel")
            return label

        row1 = QHBoxLayout()
        row1.setSpacing(7)
        row1.addWidget(option_label("输出目录"))
        self.outdir_edit = QLineEdit()
        self.outdir_edit.setPlaceholderText("留空 = 与源文件同目录")
        self.outdir_browse = QPushButton("浏览 …")
        self.outdir_browse.setObjectName("miniButton")
        row1.addWidget(self.outdir_edit, 1)
        row1.addWidget(self.outdir_browse)
        row1.addSpacing(16)
        row1.addWidget(option_label("只转指定页"))
        self.pages_edit = QLineEdit()
        self.pages_edit.setPlaceholderText("全部（如 1-3,7）")
        # 132px：容得下占位符「全部（如 1-3,7）」的全部字符（13px，含内边距）
        self.pages_edit.setFixedWidth(132)
        self.pages_edit.setToolTip("对整批生效，试参数用；留空 = 转整份")
        row1.addWidget(self.pages_edit)
        row1.addSpacing(16)
        row1.addWidget(option_label("JSON 报表"))
        self.report_edit = QLineEdit()
        self.report_edit.setPlaceholderText("留空 = 不写报表")
        self.report_browse = QPushButton("浏览 …")
        self.report_browse.setObjectName("miniButton")
        row1.addWidget(self.report_edit, 1)
        row1.addWidget(self.report_browse)
        options_grid.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        self.skip_box = QCheckBox("跳过已存在输出")
        self.skip_box.setToolTip("重跑续传：已完整落盘的产物直接跳过，不重复发请求")
        self.verify_box = QCheckBox("转换后体检")
        self.verify_box.setToolTip("每份产物跑一遍 verify 的检查（表格结构/化学记号/符号）")
        self.html_box = QCheckBox("体检时生成 HTML 预览")
        self.html_box.setToolTip("KaTeX 预览页写到 输出目录/preview/（隐含体检）")
        self.stop_box = QCheckBox("失败即停")
        self.precheck_box = QCheckBox("启动前自检")
        self.precheck_box.setToolTip("开跑前先发一张探针图做 API 连通性检查，不通就整批不启动")
        row2.addWidget(self.skip_box)
        row2.addWidget(self.verify_box)
        row2.addWidget(self.html_box)
        row2.addWidget(self.stop_box)
        row2.addWidget(self.precheck_box)
        row2.addStretch(1)
        options_grid.addLayout(row2)

        # ---- 进度条 + 执行控制 --------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        self.progress_label = QLabel("就绪。拖入课件或点「添加文件」开始。")
        self.progress_label.setObjectName("dimLabel")

        self.dry_run_btn = QPushButton("预演")
        self.dry_run_btn.setToolTip(
            "只列出会转哪些、输出到哪：不调用 API、不写文件（省钱，Ctrl+P）")
        self.start_btn = QPushButton("开始转换")
        self.start_btn.setObjectName("primaryButton")
        self.start_btn.setMinimumWidth(120)
        self.start_btn.setToolTip("开始转换（Ctrl+Enter）")
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setMinimumWidth(80)
        self.stop_btn = QPushButton("终止")
        self.stop_btn.setObjectName("dangerButton")
        self.stop_btn.setMinimumWidth(80)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, 1)
        progress_row.addSpacing(8)
        progress_row.addWidget(self.dry_run_btn)   # 省钱入口挨着主操作，而不是藏在工具栏
        progress_row.addWidget(self.start_btn)
        progress_row.addWidget(self.pause_btn)
        progress_row.addWidget(self.stop_btn)

        control = QVBoxLayout()
        control.setSpacing(4)
        control.addLayout(progress_row)
        control.addWidget(self.progress_label)

        # ---- 中央区总装 --------------------------------------------------------
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        layout.addWidget(body_splitter, 1)
        layout.addWidget(options_group)
        layout.addLayout(control)
        self.setCentralWidget(central)

        self._refresh_status_summary()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("文件(&F)")
        act_add = QAction("添加文件 …", self)
        act_add.setShortcut(QKeySequence.StandardKey.Open)
        act_add.triggered.connect(self.file_panel._pick_files)
        act_dir = QAction("添加文件夹 …", self)
        act_dir.triggered.connect(self.file_panel._pick_dir)
        act_clear = QAction("清空列表", self)
        act_clear.triggered.connect(self.file_panel.clear_all)
        act_start = QAction("开始转换", self)
        act_start.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])
        act_start.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act_start.triggered.connect(self._start_from_shortcut)
        act_dry = QAction("预演（不调用 API）", self)
        act_dry.setShortcut(QKeySequence("Ctrl+P"))
        act_dry.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        act_dry.triggered.connect(self._dry_run_from_shortcut)
        act_open_cfg = QAction("打开配置目录", self)
        act_open_cfg.triggered.connect(self._open_config_dir)
        act_quit = QAction("退出", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addActions([act_add, act_dir, act_clear])
        file_menu.addSeparator()
        file_menu.addActions([act_start, act_dry])
        file_menu.addSeparator()
        file_menu.addActions([act_open_cfg, act_quit])

        view_menu = self.menuBar().addMenu("视图(&V)")
        act_log = QAction("折叠 / 展开日志", self)
        act_log.setShortcut(QKeySequence("Ctrl+L"))
        act_log.triggered.connect(self.log_panel.toggle_folded)
        view_menu.addAction(act_log)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        act_readme = QAction("使用说明（架构与用法）", self)
        act_readme.triggered.connect(self._open_readme)
        act_about = QAction("关于", self)
        act_about.triggered.connect(self._about)
        help_menu.addActions([act_readme, act_about])

    # ================================================================ 信号编排
    def _wire(self):
        fp, mp, lp = self.file_panel, self.model_panel, self.log_panel
        fp.changed.connect(self._refresh_controls)
        fp.notice.connect(lp.append)
        fp.prep_requested.connect(self._enqueue_prep)
        mp.test_requested.connect(self._run_api_check)
        mp.profiles_edited.connect(
            lambda: lp.append("info", "配置已保存到 " + str(DEFAULT_PATH)))
        mp.profile_changed.connect(self._refresh_status_summary)
        mp.profiles_edited.connect(self._refresh_status_summary)

        self.dry_run_btn.clicked.connect(lambda: self._start_batch(dry_run=True))
        self.start_btn.clicked.connect(lambda: self._start_from_shortcut())
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.stop_btn.clicked.connect(self._request_stop)

        self.outdir_browse.clicked.connect(self._pick_outdir)
        self.report_browse.clicked.connect(self._pick_report)
        for widget in (self.outdir_edit, self.pages_edit, self.report_edit,
                       self.skip_box, self.verify_box, self.html_box,
                       self.stop_box, self.precheck_box):
            signal = widget.textChanged if isinstance(widget, QLineEdit) else widget.toggled
            signal.connect(self._save_options)
        self.log_panel.folded.connect(
            lambda folded: self.store.set_ui(log_visible=not folded))
        self.log_panel.auto_scroll.toggled.connect(
            lambda on: self.store.set_ui(auto_scroll=bool(on)))

    # ================================================================ 选项持久化
    def _collect_options(self) -> ConvertOptions:
        return ConvertOptions(
            outdir=self.outdir_edit.text().strip(),
            skip_existing=self.skip_box.isChecked(),
            verify=self.verify_box.isChecked(),
            html_preview=self.html_box.isChecked(),
            stop_on_error=self.stop_box.isChecked(),
            precheck=self.precheck_box.isChecked(),
            pages=self.pages_edit.text().strip(),
            report=self.report_edit.text().strip(),
        )

    def _save_options(self, *_):
        self.store.set_options(self._collect_options())
        self._refresh_status_summary()

    def _load_options_into_ui(self):
        o = self.store.options
        self.outdir_edit.setText(o.outdir)
        self.pages_edit.setText(o.pages)
        self.report_edit.setText(o.report)
        self.skip_box.setChecked(o.skip_existing)
        self.verify_box.setChecked(o.verify)
        self.html_box.setChecked(o.html_preview)
        self.stop_box.setChecked(o.stop_on_error)
        self.precheck_box.setChecked(o.precheck)

    def _pick_outdir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if directory:
            self.outdir_edit.setText(directory)

    def _pick_report(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 JSON 报表", "batch_report.json", "JSON 报表 (*.json)")
        if path:
            self.report_edit.setText(path)

    # ================================================================ 拖拽
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile()]
        if paths:
            self.file_panel.add_paths(paths)
            event.acceptProposedAction()

    # ================================================================ PPT 预处理
    def _enqueue_prep(self, tasks):
        if not tasks:
            return
        self._prep_queue.extend(tasks)
        self._maybe_start_prep()

    def _maybe_start_prep(self):
        if self._prep_worker is not None or not self._prep_queue:
            return
        batch, self._prep_queue = self._prep_queue[:], []
        worker = PrepWorker(batch, self)
        worker.prepared.connect(self._on_prepared)
        worker.finished.connect(self._on_prep_finished)
        self._prep_worker = worker
        worker.start()

    def _on_prepared(self, uid: int, pdf: str, error: str):
        if error:
            self.file_panel.update_task({
                "uid": uid, "status": TaskStatus.FAILED.value,
                "message": f"PPT 转 PDF 失败：{error}"})
            self.log_panel.append("error", f"[失败] PPT 转 PDF（uid={uid}）：{error}")
        else:
            self.file_panel.update_task({
                "uid": uid, "pdf": pdf, "status": TaskStatus.PENDING.value,
                "message": "PPT 已转成 PDF 缓存，等待转换"})
            self.log_panel.append("ok", f"PPT 已转成 PDF 缓存：{pdf}")

    def _on_prep_finished(self):
        self._prep_worker = None
        self._maybe_start_prep()

    # ================================================================ API 自检
    def _run_api_check(self, index: int):
        profile = self.model_panel.current_profile()
        if profile is None or self._check_worker is not None:
            return
        self.log_panel.append("info", f"测试连接：{profile.name}（引擎 {profile.engine}）…")
        self.model_panel.set_testing(True)
        worker = CheckWorker(profile, self)
        worker.finished_ok.connect(self._on_check_finished)
        worker.finished.connect(self._on_check_done)
        self._check_worker = worker
        worker.start()

    def _on_check_finished(self, ok: bool, message: str):
        self.log_panel.append("ok" if ok else "error", message)
        box = QMessageBox.information if ok else QMessageBox.warning
        box(self, "连接测试通过" if ok else "连接测试未通过", message)

    def _on_check_done(self):
        self._check_worker = None
        self.model_panel.set_testing(False)

    # ================================================================ 批次执行
    def _start_batch(self, dry_run: bool):
        tasks = self.file_panel.startable_tasks()
        profile = self.model_panel.current_profile()
        if self._worker is not None:
            return
        if not tasks:
            self.log_panel.append("warn", "没有可转换的任务（先在左侧导入文件）。")
            return
        if profile is None:
            self.log_panel.append("error", "没有可用的模型配置（先在右侧新建一条）。")
            return

        options = self._collect_options()
        # 覆盖护栏：真实转换前先看目标路径，已存在的产物默认不覆盖
        if not dry_run and not self._confirm_overwrite(tasks, options):
            return
        options = self._collect_options()   # 弹窗里可能勾上了「跳过已存在输出」
        self._save_options()

        worker = ConversionWorker(self)
        worker.configure(tasks, profile, options, dry_run=dry_run)
        worker.log_line.connect(self.log_panel.append)
        worker.file_updated.connect(self._on_file_updated)
        worker.page_progress.connect(self._on_page_progress)
        worker.state_changed.connect(self._on_worker_state)
        worker.batch_finished.connect(self._on_batch_finished)
        self._worker = worker

        self._batch_started = time.monotonic()
        self._n_files = len(tasks)
        self._done_files = 0
        self._done_pages = 0
        self._page_in_file = 0
        # 总页数由工作线程在开跑前逐文件 count_pages 后经 file_updated 发回，
        # GUI 线程不做任何 PDF 解析（大批量时保持界面不卡）。
        self._total_pages = 0
        self._counted_uids: set = set()
        self.progress.setValue(0)
        self._set_phase("running")
        self.progress_label.setText(f"{'预演' if dry_run else '转换'}队列：{len(tasks)} 个文件")

        self._set_running(True)
        self.pause_btn.setText("暂停")
        self._drain_timer.start()
        worker.start()

    def _drain_worker(self):
        if self._worker is not None:
            self._worker.drain_events()

    def _on_file_updated(self, d: dict):
        self.file_panel.update_task(d)
        uid = d.get("uid")
        # 每个 uid 第一次带 pages 的事件 = 工作线程开跑前的统计，累加为总页数
        if uid is not None and uid not in self._counted_uids and d.get("pages"):
            self._counted_uids.add(uid)
            self._total_pages += int(d["pages"])
        status = d.get("status")
        if status in (TaskStatus.SUCCESS.value, TaskStatus.SKIPPED.value,
                      TaskStatus.FAILED.value, TaskStatus.CANCELLED.value):
            self._done_files += 1
            self._page_in_file = 0
            if status in (TaskStatus.SUCCESS.value, TaskStatus.SKIPPED.value):
                self._done_pages += int(d.get("pages") or 0)
            self._update_progress()

    def _on_page_progress(self, uid: int, page: int, total: int):
        self._page_in_file = max(0, page)
        self._update_progress()

    def _update_progress(self):
        n = max(1, self._n_files)
        if self._total_pages > 0:
            frac = (self._done_pages + self._page_in_file) / self._total_pages
        else:
            frac = self._done_files / n
        frac = min(1.0, max(0.0, frac))
        self.progress.setValue(int(frac * 1000))

        done = min(self._done_files, n)
        elapsed = _fmt_secs(time.monotonic() - self._batch_started)
        if self._phase == "paused":
            text = f"已暂停 · {done}/{n} 个文件 · 已用 {elapsed}（点「继续」恢复）"
        elif self._phase == "pausing":
            text = f"正在暂停 · 当前文件跑完后停下（{done}/{n} 个文件）"
        elif self._phase == "stopping":
            text = f"正在终止 · 等当前这次请求结束后退出（{done}/{n} 个文件）"
        else:
            text = f"{done}/{n} 个文件 · 已用 {elapsed}"
            if self._total_pages and self._done_pages:
                remain_pages = max(0, self._total_pages - self._done_pages - self._page_in_file)
                eta = (time.monotonic() - self._batch_started) / self._done_pages * remain_pages
                text += f" · 预计剩余约 {_fmt_secs(eta)}（按每页均速估）"
        self.progress_label.setText(text)

    def _on_worker_state(self, state: str):
        """工作线程确认的状态（暂停点是它真正停下时才发）。"""
        if state == "paused":
            self.pause_btn.setText("继续")
            self.pause_btn.setObjectName("primaryButton")
            self._set_phase("paused")
        elif state == "running":
            self.pause_btn.setText("暂停")
            self.pause_btn.setObjectName("")
            self._set_phase("running")
        else:
            return
        self.pause_btn.style().unpolish(self.pause_btn)
        self.pause_btn.style().polish(self.pause_btn)

    def _set_phase(self, phase: str):
        """把相位同时写进进度条和进度文字（QSS 用 [phase=...] 选择器上色）。"""
        self._phase = phase
        for widget in (self.progress, self.progress_label):
            widget.setProperty("phase", phase)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        if phase != "idle" and self._worker is not None:
            self._update_progress()

    def _start_from_shortcut(self):
        """Ctrl+Enter：与点「开始转换」完全同一条路径，只是按钮禁用时不触发。"""
        if self.start_btn.isEnabled():
            self._start_batch(dry_run=False)

    def _dry_run_from_shortcut(self):
        """Ctrl+P：预演（不调用 API、不写文件）。"""
        if self.dry_run_btn.isEnabled():
            self._start_batch(dry_run=True)

    def _toggle_pause(self):
        if self._worker is None:
            return
        if self.pause_btn.text() == "继续":
            # 恢复：置标志即可；工作线程离开暂停点时自己发 state_changed
            self._worker.resume()
            self._on_worker_state("running")
        else:
            # 暂停：置标志即可；「已暂停」的确认由工作线程真正停下时发出。
            # 这中间显示「正在暂停」，否则按钮已经变形、进度却纹丝不动。
            self._worker.pause()
            self._set_phase("pausing")
            self.log_panel.append("warn", "[暂停] 已收到暂停请求，当前文件跑完后暂停 ...")

    def _request_stop(self):
        if self._worker is None:
            return
        self._worker.cancel()
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("正在终止 …")
        self.pause_btn.setEnabled(False)   # 已经在收尾，不允许再「继续」
        self._set_phase("stopping")
        self.log_panel.append("warn", "[终止] 已收到终止请求，等待当前请求结束后退出 ...")

    def _on_batch_finished(self, summary: dict):
        # 先把队列里剩余的事件排空，再停定时器（顺序保证日志一行不丢）
        if self._worker is not None:
            self._worker.drain_events(10_000)
        self._drain_timer.stop()
        self._set_running(False)
        self._set_phase("idle")
        self._worker = None
        if summary.get("error") == "precheck_failed":
            self.progress_label.setText("自检未通过，整批未启动。")
        elif summary.get("dry_run"):
            self.progress_label.setText(f"预演完成：{summary.get('total', 0)} 个文件，未调用 API。")
        else:
            self.progress_label.setText(
                f"完成：成功 {summary.get('ok', 0)} / 跳过 {summary.get('skip', 0)}"
                f" / 失败 {summary.get('fail', 0)} / 取消 {summary.get('cancel', 0)}"
                f" · 用时 {_fmt_secs(summary.get('elapsed', 0))}")

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.dry_run_btn.setEnabled(not running)
        self.pause_btn.setEnabled(running)
        self.stop_btn.setEnabled(running)
        # 两个按钮的文字/样式都要回位：跑完还挂着「正在终止 …」或蓝色「继续」会误导
        self.stop_btn.setText("终止")
        self.pause_btn.setText("暂停")
        if self.pause_btn.objectName():
            self.pause_btn.setObjectName("")
            self.pause_btn.style().unpolish(self.pause_btn)
            self.pause_btn.style().polish(self.pause_btn)

    def _refresh_controls(self):
        self.start_btn.setEnabled(
            bool(self.file_panel.startable_tasks()) and self._worker is None)
        self.dry_run_btn.setEnabled(
            bool(self.file_panel.tasks()) and self._worker is None)

    # ================================================================ 覆盖护栏
    def _confirm_overwrite(self, tasks, options) -> bool:
        """开跑前检查目标路径：已存在的产物默认不覆盖（老师手改过的定稿会没）。

        返回 True = 继续（可能已经把「跳过已存在输出」勾上），False = 取消。
        """
        if options.skip_existing:
            return True
        try:
            jobs = bridge.plan_jobs([t.source for t in tasks], options.outdir or "")
        except Exception:  # noqa: BLE001 —— 规划失败交给工作线程去报错
            return True
        existing = [job.dest for job in jobs if os.path.isfile(job.dest)]
        if not existing:
            return True

        dialog = OverwriteDialog(existing, self)
        dialog.exec()
        if dialog.choice == "overwrite":
            return True
        if dialog.choice == "skip":
            self.skip_box.setChecked(True)   # 落盘：下次开跑同样跳过
            self.log_panel.append(
                "warn", f"[覆盖] 已改为跳过 {len(existing)} 份已存在的输出。")
            return True
        return False

    # ================================================================ 杂项
    def _refresh_status_summary(self, *_):
        """状态栏改成「产物落在哪 + 同名文件怎么处理」——比重复一遍拖拽说明有用。"""
        outdir = self.outdir_edit.text().strip() or "与源文件同目录"
        mode = "跳过" if self.skip_box.isChecked() else "覆盖（先确认）"
        self.statusBar().showMessage(f"输出：{outdir} · 同名产物：{mode}")

    def _open_config_dir(self):
        directory = os.path.dirname(DEFAULT_PATH)
        os.makedirs(directory, exist_ok=True)
        os.startfile(directory)  # noqa: S606 —— Windows 资源管理器

    def _open_readme(self):
        readme = os.path.join(os.path.dirname(DEFAULT_PATH), "..", "GUI_README.md")
        readme = os.path.normpath(readme)
        if os.path.isfile(readme):
            os.startfile(readme)  # noqa: S606
        else:
            QMessageBox.information(self, "使用说明", f"未找到文档：{readme}")

    def _about(self):
        QMessageBox.about(
            self, "关于",
            "Handout-Maker2 桌面客户端\n\n"
            "PDF / PPT 课件 → 结构化 Markdown 化学讲义\n"
            "核心引擎：handout_maker.py / handout_maker2.py + batch.py\n"
            "界面：PySide6（Qt 6）\n\n"
            f"配置文件：{DEFAULT_PATH}")

    def _warn_missing_deps(self):
        missing = bridge.check_runtime_deps()
        if missing:
            self.log_panel.append(
                "warn", "[警告] 缺少运行依赖：" + "；".join(missing))

    # ================================================================ 窗口状态
    def _restore_ui_state(self):
        ui = self.store.ui
        geo = ui.get("geometry")
        if isinstance(geo, str) and geo:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode("ascii")))
            except Exception:  # noqa: BLE001
                pass
        for key, splitter in (("splitter_main", self.work_splitter),
                              ("splitter_body", self.body_splitter)):
            sizes = ui.get(key)
            if isinstance(sizes, (list, tuple)) and len(sizes) == 2 and sum(sizes) > 0:
                splitter.setSizes([int(s) for s in sizes])
        self._load_options_into_ui()
        if ui.get("log_visible") is False:
            self.log_panel.set_folded(True)
        self.log_panel.auto_scroll.setChecked(bool(ui.get("auto_scroll", True)))

    def _save_ui_state(self):
        self.store.set_ui(
            geometry=self.saveGeometry().toBase64().data().decode("ascii"),
            splitter_main=list(self.work_splitter.sizes()),
            splitter_body=list(self.body_splitter.sizes()),
            log_visible=not self.log_panel.is_folded,
            auto_scroll=self.log_panel.auto_scroll.isChecked(),
        )

    # ================================================================ 关闭善后
    def closeEvent(self, event):
        self._drain_timer.stop()
        if self._worker is not None and self._worker.isRunning():
            ret = QMessageBox.question(
                self, "转换仍在进行",
                "批量转换还没有跑完。\n\n"
                "「终止并退出」：当前请求结束后停止并退出（半成品 .part 会保留）；\n"
                "「留在界面」：继续转换。")
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            if not self._worker.wait(5000):
                self.log_panel.append(
                    "warn", "[终止] 等待超时，强制结束工作线程（半成品已落盘，可续传）。")
                self._worker.terminate()
                self._worker.wait(2000)
        self._save_options()
        self._save_ui_state()
        event.accept()
