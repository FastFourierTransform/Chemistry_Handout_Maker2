# -*- coding: utf-8 -*-
"""文件管理与任务列表面板。

* 多文件导入：文件对话框（文件/文件夹）或拖拽（文件与目录都收）；
* 任务列表：文件名 + 结果/产物路径（两行）/ 大小 / 页数 / 状态 / 操作；
* 一键清空 / 重置所有状态；
* 空列表时表格中央给一句下一步（不是只有一行灰字）；
* .pptx/.ppt 会先进入「准备中」状态（PrepWorker 转 PDF 缓存），
  就绪后自动变「等待中」——面板通过 prep_requested 信号把活交给主窗口。

失败原因和产物路径不能只躺在 tooltip 里：QTableWidget 的单元格只支持单行，
所以「文件」列由 ``_FileCellDelegate`` 自己画第二行（12px 小字，失败红色 /
成功灰色路径），行高随之在 34 / 46 之间切换。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui import theme
from gui.core.bridge import natural_key
from gui.core.models import FileTask, TaskStatus

_SUPPORTED_EXTS = (".pdf", ".pptx", ".ppt")

_STATUS_COLORS = {
    # 明亮主题配色（GitHub light 系）；七种状态各占一个可区分的色调
    TaskStatus.PREPARING: "#9a6700",   # 琥珀：正在准备
    TaskStatus.PENDING: "#57606a",     # 灰：等待
    TaskStatus.RUNNING: "#0969da",     # 蓝：进行中
    TaskStatus.SUCCESS: "#1a7f37",     # 绿：完成
    TaskStatus.FAILED: "#cf222e",      # 红：失败
    TaskStatus.CANCELLED: "#6e7781",   # 灰蓝：用户终止（终态，安静）
    TaskStatus.SKIPPED: "#8250df",     # 紫：已存在产物、没有重复花钱
}

# 「文件」列第二行的数据角色
_SUBTEXT_ROLE = Qt.ItemDataRole.UserRole + 1
_SUBTONE_ROLE = Qt.ItemDataRole.UserRole + 2


class _FileCellDelegate(QStyledItemDelegate):
    """文件列：两行（文件名 + 结果）。行高由 FilePanel 按有无第二行设置。"""

    SINGLE_HEIGHT = 34
    DOUBLE_HEIGHT = 46

    @staticmethod
    def _font(base: QFont, px: int) -> QFont:
        font = QFont(base)
        font.setPixelSize(px)
        font.setWeight(QFont.Weight.Normal)
        return font

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""          # 背景/选中态交给样式画，文字自己画（要两行）
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        rect = option.rect.adjusted(8, 0, -8, 0)
        sub = index.data(_SUBTEXT_ROLE) or ""
        painter.save()
        painter.setPen(QColor(theme.INK))

        main_font = self._font(option.font, theme.PX_BODY)
        main_metrics = QFontMetrics(main_font)
        painter.setFont(main_font)
        if not sub:
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                main_metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, rect.width()))
            painter.restore()
            return

        line1 = rect.adjusted(0, 3, 0, 0)
        line1.setHeight(main_metrics.height())
        painter.drawText(
            line1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            main_metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, rect.width()))

        sub_font = self._font(option.font, theme.PX_META)
        sub_metrics = QFontMetrics(sub_font)
        tone = index.data(_SUBTONE_ROLE) or theme.MUTED
        painter.setFont(sub_font)
        painter.setPen(QColor(tone))
        line2 = rect.adjusted(0, 3 + main_metrics.height(), 0, 0)
        line2.setHeight(sub_metrics.height())
        painter.drawText(
            line2, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            sub_metrics.elidedText(str(sub), Qt.TextElideMode.ElideMiddle, rect.width()))
        painter.restore()

    def sizeHint(self, option, index):  # noqa: N802 —— Qt 命名
        height = (self.DOUBLE_HEIGHT if index.data(_SUBTEXT_ROLE)
                  else self.SINGLE_HEIGHT)
        return QSize(0, height)


class _TaskTable(QTableWidget):
    """任务表：空列表时在表格里画出第一步（表格本体没有空态）。"""

    EMPTY_TEXT = "把课件拖到这里，或点上方「添加文件」"

    def paintEvent(self, event):  # noqa: N802 —— Qt 命名
        super().paintEvent(event)
        if self.rowCount():
            return
        painter = QPainter(self.viewport())
        font = QFont(self.font())
        font.setPixelSize(theme.PX_BODY)
        painter.setFont(font)
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(
            self.viewport().rect().adjusted(24, -20, -24, -20),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self.EMPTY_TEXT)
        painter.end()


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


class FilePanel(QWidget):
    changed = Signal()                 # 任务结构/状态变化（主窗口据此刷新按钮可用性）
    notice = Signal(str, str)          # (级别, 文本) → 日志面板
    prep_requested = Signal(list)      # 需要 PPT→PDF 预处理的任务列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: dict[int, FileTask] = {}
        self._rows: dict[int, int] = {}
        self._seen: set[str] = set()
        self._next_uid = 1

        # ---- 工具栏 ---------------------------------------------------------
        self.add_files_btn = QPushButton("添加文件 …")
        self.add_dir_btn = QPushButton("添加文件夹 …")
        self.clear_btn = QPushButton("清空列表")
        self.reset_btn = QPushButton("重置状态")
        self.count_label = QLabel("0 个文件")
        self.count_label.setObjectName("dimLabel")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.add_files_btn)
        toolbar.addWidget(self.add_dir_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(self.count_label)
        toolbar.addWidget(self.reset_btn)
        toolbar.addWidget(self.clear_btn)

        # ---- 任务表 ---------------------------------------------------------
        self.table = _TaskTable(0, 5)
        # 表头与单元格同左对齐（表头默认居中，数字列会显得和值错位）
        for column, title in enumerate(["文件", "大小", "页数", "状态", "操作"]):
            header_item = QTableWidgetItem(title)
            header_item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.table.setHorizontalHeaderItem(column, header_item)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setObjectName("fileTable")
        self.table.setItemDelegateForColumn(0, _FileCellDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.Stretch)
        header.setSectionResizeMode(1, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, header.ResizeMode.Fixed)
        self.table.setColumnWidth(4, 148)
        self.table.verticalHeader().setDefaultSectionSize(_FileCellDelegate.SINGLE_HEIGHT)

        hint = theme.HintLabel(
            "支持 .pdf / .pptx / .ppt —— 把文件或文件夹直接拖到窗口任意位置，"
            "或点「添加文件」；PPT 会自动先转成 PDF。")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(toolbar)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)

        # ---- 拖拽（面板自身也接收，落点在表格上时事件会先到这里）--------------
        self.setAcceptDrops(True)

        # ---- 信号 -----------------------------------------------------------
        self.add_files_btn.clicked.connect(self._pick_files)
        self.add_dir_btn.clicked.connect(self._pick_dir)
        self.clear_btn.clicked.connect(self.clear_all)
        self.reset_btn.clicked.connect(self.reset_all)

    # ---------------------------------------------------------------- 拖拽
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            if url.isLocalFile():
                paths.append(url.toLocalFile())
        if paths:
            self.add_paths(paths)
            event.acceptProposedAction()

    # ---------------------------------------------------------------- 导入
    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要转换的课件", "",
            "课件 (*.pdf *.pptx *.ppt);;PDF 文档 (*.pdf);;演示文稿 (*.pptx *.ppt);;所有文件 (*)")
        if files:
            self.add_paths(files)

    def _pick_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "选择课件文件夹")
        if directory:
            self.add_paths([directory])

    @staticmethod
    def _expand(paths) -> list[str]:
        """文件 / 目录统一展开成文件列表（目录递归收集支持的扩展名）。"""
        out: list[str] = []
        for raw in paths:
            if os.path.isdir(raw):
                for dirpath, dirnames, filenames in os.walk(raw):
                    dirnames.sort(key=natural_key)
                    for name in sorted(filenames, key=natural_key):
                        if name.lower().endswith(_SUPPORTED_EXTS):
                            out.append(os.path.join(dirpath, name))
            elif os.path.isfile(raw):
                out.append(raw)
        return out

    def add_paths(self, paths) -> int:
        """导入一批路径（文件或目录），返回新增数量。"""
        fresh = []
        for path in self._expand(paths):
            key = os.path.normcase(os.path.abspath(path))
            if key in self._seen:
                continue
            self._seen.add(key)
            task = FileTask(uid=self._next_uid, source=path,
                            size=os.path.getsize(path))
            self._next_uid += 1
            if task.needs_prep:
                task.status = TaskStatus.PREPARING
                task.message = "等待转成 PDF …"
            else:
                task.pdf = path
            self._tasks[task.uid] = task
            self._insert_row(task)
            fresh.append(task)

        prep = [t for t in fresh if t.needs_prep]
        if fresh:
            self.notice.emit("info", f"已导入 {len(fresh)} 个文件" + (
                f"（其中 {len(prep)} 个 PPT 需要先转 PDF）" if prep else ""))
        if prep:
            self.prep_requested.emit(prep)
        if fresh:
            self.changed.emit()
        return len(fresh)

    def _insert_row(self, task: FileTask):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[task.uid] = row

        name_item = QTableWidgetItem()
        name_item.setData(Qt.ItemDataRole.UserRole, task.uid)
        self.table.setItem(row, 0, name_item)
        self._refresh_name_cell(row, task)
        self.table.setItem(row, 1, QTableWidgetItem(human_size(task.size)))
        self.table.setItem(row, 2, QTableWidgetItem("—"))
        self._set_status_cell(row, task)
        self._set_action_cell(row, task)

    @staticmethod
    def _subline(task: FileTask) -> tuple[str, str]:
        """「文件」列第二行：失败原因 / 产物路径 / 准备说明（空串 = 不占行）。"""
        if task.status == TaskStatus.FAILED:
            return (task.message or "失败，但没有错误信息"), _STATUS_COLORS[TaskStatus.FAILED]
        if task.status in (TaskStatus.SUCCESS, TaskStatus.SKIPPED):
            return (task.output or task.message or "已存在产物"), theme.MUTED
        if task.status == TaskStatus.PREPARING:
            return (task.message or "等待转成 PDF …"), theme.MUTED
        return "", ""

    def _refresh_name_cell(self, row: int, task: FileTask):
        item = self.table.item(row, 0)
        if item is None:
            item = QTableWidgetItem()
            self.table.setItem(row, 0, item)
        item.setData(Qt.ItemDataRole.UserRole, task.uid)
        item.setText(os.path.basename(task.source))
        sub, tone = self._subline(task)
        item.setData(_SUBTEXT_ROLE, sub)
        item.setData(_SUBTONE_ROLE, tone)
        item.setToolTip(task.source + (f"\n{sub}" if sub else ""))
        self.table.setRowHeight(
            row, _FileCellDelegate.DOUBLE_HEIGHT if sub else _FileCellDelegate.SINGLE_HEIGHT)

    def _set_status_cell(self, row: int, task: FileTask):
        item = QTableWidgetItem(task.status.value)
        item.setForeground(QColor(_STATUS_COLORS.get(task.status, "#57606a")))
        if task.message:
            item.setToolTip(task.message)
        if task.output:
            item.setToolTip(f"{task.message}\n输出：{task.output}")
        self.table.setItem(row, 3, item)

    def _set_action_cell(self, row: int, task: FileTask):
        holder = QWidget()
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)
        remove_btn = QPushButton("移除")
        remove_btn.setObjectName("miniButton")
        remove_btn.clicked.connect(lambda _=False, u=task.uid: self.remove_task(u))
        retry_btn = QPushButton("重转")
        retry_btn.setObjectName("miniButton")
        retry_btn.setEnabled(task.status != TaskStatus.PREPARING)
        retry_btn.clicked.connect(lambda _=False, u=task.uid: self.retry_task(u))
        lay.addWidget(remove_btn)
        lay.addWidget(retry_btn)
        self.table.setCellWidget(row, 4, holder)

    # ---------------------------------------------------------------- 更新
    def update_task(self, d: dict):
        """主窗口转发工作线程的 file_updated 事件。"""
        uid = d.get("uid")
        task = self._tasks.get(uid)
        if task is None:
            return
        row = self._rows.get(uid)
        if row is None or row >= self.table.rowCount():
            return
        if "pdf" in d and d["pdf"]:
            task.pdf = d["pdf"]
        if "status" in d:
            try:
                task.status = TaskStatus(d["status"])
            except ValueError:
                pass
        if "message" in d:
            task.message = d["message"]
        if "output" in d:
            task.output = d["output"] or task.output
        if "seconds" in d:
            task.seconds = float(d["seconds"] or 0.0)
        if "pages" in d and d["pages"]:
            task.pages = int(d["pages"])
            self.table.setItem(row, 2, QTableWidgetItem(str(task.pages)))
        # 文件列的第二行（失败原因/产物路径）、状态/操作两列都按最新状态重绘
        self._refresh_name_cell(row, task)
        self._set_status_cell(row, task)
        self._set_action_cell(row, task)
        self._update_count_label()
        self.changed.emit()

    def _update_count_label(self):
        done = sum(1 for t in self._tasks.values() if t.status == TaskStatus.SUCCESS)
        failed = sum(1 for t in self._tasks.values() if t.status == TaskStatus.FAILED)
        text = f"{len(self._tasks)} 个文件"
        if done or failed:
            text += f" · 完成 {done} · 失败 {failed}"
        self.count_label.setText(text)

    # ---------------------------------------------------------------- 操作
    def remove_task(self, uid: int):
        task = self._tasks.pop(uid, None)
        if task is None:
            return
        row = self._rows.pop(uid, None)
        if row is not None:
            self.table.removeRow(row)
            # 行号前移：重建 row 索引映射
            for other_uid, other_row in list(self._rows.items()):
                if other_row > row:
                    self._rows[other_uid] = other_row - 1
        key = os.path.normcase(os.path.abspath(task.source))
        self._seen.discard(key)
        self.notice.emit("info", f"已移除：{os.path.basename(task.source)}")
        self._update_count_label()
        self.changed.emit()

    def retry_task(self, uid: int):
        task = self._tasks.get(uid)
        if task is None or task.status == TaskStatus.PREPARING:
            return
        task.output = ""
        task.seconds = 0.0
        if task.needs_prep:
            task.status = TaskStatus.PREPARING
            task.message = "重新转 PDF …"
            self.prep_requested.emit([task])
        else:
            task.status = TaskStatus.PENDING
            task.message = ""
        self.update_task({"uid": uid})
        self.notice.emit("info", f"已重置为待转换：{os.path.basename(task.source)}")

    def clear_all(self):
        if not self._tasks:
            return
        ret = QMessageBox.question(
            self, "清空列表", f"确定清空全部 {len(self._tasks)} 个任务？（不影响已产出的文件）")
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._tasks.clear()
        self._rows.clear()
        self._seen.clear()
        self.table.setRowCount(0)
        self._update_count_label()
        self.notice.emit("info", "任务列表已清空。")
        self.changed.emit()

    def reset_all(self):
        if not self._tasks:
            return
        ret = QMessageBox.question(
            self, "重置状态",
            "把所有任务重置为「等待中」（PPT 会重新准备）？\n"
            "已产出的 .md 不会删除；勾选「跳过已存在输出」可避免重复花费额度。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        prep = []
        for task in self._tasks.values():
            task.output = ""
            task.seconds = 0.0
            if task.needs_prep:
                task.status = TaskStatus.PREPARING
                task.message = "等待转成 PDF …"
                prep.append(task)
            else:
                task.status = TaskStatus.PENDING
                task.message = ""
            self.update_task({"uid": task.uid})
        if prep:
            self.prep_requested.emit(prep)
        self.notice.emit("info", "全部任务已重置。")
        self.changed.emit()

    # ---------------------------------------------------------------- 查询
    def tasks(self) -> list[FileTask]:
        return list(self._tasks.values())

    def startable_tasks(self) -> list[FileTask]:
        """「开始转换」会纳入的任务：状态可转 + PDF 已就绪。"""
        return [t for t in self._tasks.values()
                if t.status.can_start and t.pdf]
