# -*- coding: utf-8 -*-
"""配置文件编辑弹窗：引擎 + 网关 + Key + 模型链 + 高级参数表。

排版结构（见 gui/theme.py 的字阶表）：
* 左列字段标签 13/400 灰、右列值 13/400 深 —— 一眼分得清「名字」和「填的东西」；
* 字段说明（引擎说明、模型链提示）用 12px 灰 + 1.55 行距，挂在字段下方；
* 高级参数表：参数名列固定、值列固定、说明列吃掉剩余宽度并自动折行 ——
  说明文字不再被裁掉（此前 620px 宽的弹窗里每条说明都以「…」结尾）；
* 「保存」是弹窗里唯一的主操作，用主按钮样式。

高级参数表（ParamsTable）完全由 models.PARAM_SPECS 注册表**自动生成**：
新增一个可调参数 = 在注册表加一条 ParamSpec，弹窗、桥接层都会自动跟上，
不需要改这里的任何代码。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QSizePolicy, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from gui import theme
from gui.core import bridge
from gui.core.models import ApiProfile, PARAM_SPECS, coerce_param

_PARAM_COLUMN = 156   # 容得下最长的参数名「输出上限 max_tokens」
_VALUE_COLUMN = 132   # 输入控件一列宽，8 行的控件左边缘对齐


def _field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("fieldLabel")
    return label


class ParamsTable(QTableWidget):
    """高级参数表：一列一个注册表参数，值控件按 kind 自动挑类型。"""

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        # 表头与单元格同样左对齐（表头默认居中，和左对齐的单元格对不上）
        for column, title in enumerate(["参数", "值", "说明"]):
            header_item = QTableWidgetItem(title)
            header_item.setTextAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.setHorizontalHeaderItem(column, header_item)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, _PARAM_COLUMN)
        self.setColumnWidth(1, _VALUE_COLUMN)
        self._fitting = False

    # -------------------------------------------------------------- 填充/收集
    def populate(self, engine: str, overrides: dict) -> None:
        """按引擎重建表格；overrides 里已有的用户覆盖值优先于默认值。"""
        self.setRowCount(0)
        specs = [s for s in PARAM_SPECS.values() if engine in s.engines]
        for spec in specs:
            row = self.rowCount()
            self.insertRow(row)
            name_item = QTableWidgetItem(spec.label)
            name_item.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.setItem(row, 0, name_item)
            tip_item = QTableWidgetItem(spec.tip)
            tip_font = QFont(self.font())
            tip_font.setPixelSize(theme.PX_META)
            tip_item.setFont(tip_font)
            tip_item.setForeground(QColor(theme.MUTED))
            self.setItem(row, 2, tip_item)

            raw = overrides.get(spec.key)
            value = coerce_param(spec, raw)
            editor = self._make_editor(spec, value)
            self.setCellWidget(row, 1, editor)
        self._fit_rows()

    def _fit_rows(self) -> None:
        """按当前列宽折行定行高：说明列变窄时行会变高，文字不会被裁。"""
        if self._fitting:
            return
        self._fitting = True
        try:
            self.resizeRowsToContents()
            for row in range(self.rowCount()):
                editor = self.cellWidget(row, 1)
                needed = (editor.sizeHint().height() + 8) if editor else 0
                if self.rowHeight(row) < needed:
                    self.setRowHeight(row, needed)
        finally:
            self._fitting = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_rows()

    @staticmethod
    def _make_editor(spec, value):
        if spec.kind == "int":
            w = QSpinBox()
            w.setRange(1, 100000)
            w.setValue(int(value))
            return w
        if spec.kind == "float":
            w = QDoubleSpinBox()
            w.setRange(0.0, 2.0)
            w.setDecimals(2)
            w.setSingleStep(0.1)
            w.setValue(float(value))
            return w
        if spec.kind == "bool":
            holder = QWidget()
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(10, 0, 0, 0)
            box = QCheckBox()
            box.setChecked(bool(value))
            lay.addWidget(box)
            lay.addStretch(1)
            holder._box = box  # type: ignore[attr-defined]
            return holder
        if spec.kind == "choice":
            w = QComboBox()
            w.addItems(spec.choices)
            w.setCurrentText(str(value))
            return w
        w = QLineEdit(str(value) if value not in (None, "") else "")
        return w

    def collect(self) -> dict:
        """收集所有**不同于默认值**的参数（只存覆盖值，配置保持最小）。"""
        out: dict = {}
        for row in range(self.rowCount()):
            key = self.item(row, 0).data(Qt.ItemDataRole.UserRole)
            spec = PARAM_SPECS[key]
            value = self._read_editor(self.cellWidget(row, 1), spec)
            if value is None:
                continue
            if value == spec.default:
                out.pop(key, None)
            else:
                out[key] = "true" if value is True else ("false" if value is False
                                                         else str(value))
        return out

    @staticmethod
    def _read_editor(widget, spec):
        if spec.kind == "int":
            return widget.value()
        if spec.kind == "float":
            return round(widget.value(), 4)
        if spec.kind == "bool":
            return widget._box.isChecked()  # type: ignore[attr-defined]
        if spec.kind == "choice":
            return widget.currentText()
        text = widget.text().strip()
        return text or None


class ProfileDialog(QDialog):
    """新建 / 编辑一个模型 API 配置档案。"""

    def __init__(self, profile: ApiProfile | None, parent=None):
        super().__init__(parent)
        self._source = profile or ApiProfile()
        self.setWindowTitle("编辑配置" if profile else "新建配置")
        self.setMinimumWidth(640)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setVerticalSpacing(9)
        form.setHorizontalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.name_edit = QLineEdit(self._source.name)
        form.addRow(_field_label("配置名称"), self.name_edit)

        self.engine_combo = QComboBox()
        for spec in bridge.ENGINE_SPECS.values():
            self.engine_combo.addItem(spec.label, spec.id)
        self.engine_combo.setCurrentIndex(
            max(0, self.engine_combo.findData(self._source.engine)))
        form.addRow(_field_label("引擎 / 网关类型"), self.engine_combo)
        self.engine_note = theme.HintLabel()
        form.addRow("", self.engine_note)

        self.url_edit = QLineEdit(self._source.base_url)
        self.url_edit.setPlaceholderText("留空 = 引擎默认网关")
        form.addRow(_field_label("API Base URL"), self.url_edit)

        key_row = QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        self.key_edit = QLineEdit(self._source.api_key)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("留空 = 环境变量 / 脚本兜底值")
        self.key_show = QCheckBox("显示")
        self.key_show.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.key_show)
        form.addRow(_field_label("API Key"), key_row)

        self.models_edit = QLineEdit(self._source.models)
        form.addRow(_field_label("模型（降级链）"), self.models_edit)
        self.models_hint = theme.HintLabel()
        form.addRow("", self.models_hint)

        # 高级参数表：由注册表自动生成
        params_group = QGroupBox("高级参数")
        params_lay = QVBoxLayout(params_group)
        params_lay.setContentsMargins(12, 10, 12, 12)
        params_lay.setSpacing(6)
        self.params_note = theme.HintLabel("（留空/默认即用引擎内置值）")
        self.params_table = ParamsTable()
        params_lay.addWidget(self.params_note)
        params_lay.addWidget(self.params_table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("保存")
        ok_btn.setObjectName("primaryButton")   # 弹窗里唯一的主操作
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        # 表单放进固定高度的宿主：对话框变高时，多出来的高度全给参数表，
        # 字段行距不会被撑开（否则 5 个字段会被摊成 50px 的行距）
        self._form = form
        self.form_host = QWidget()
        self.form_host.setLayout(form)
        self.form_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._fitting_form = False
        layout.addWidget(self.form_host)
        layout.addWidget(params_group, 1)
        layout.addWidget(buttons)

        self._overrides = dict(self._source.params)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        self._on_engine_changed()

    # -------------------------------------------------------------- 行为
    def _engine_id(self) -> str:
        return self.engine_combo.currentData()

    def _on_engine_changed(self):
        # 切换引擎时先把当前表里改过的值收进 overrides，再按新引擎重建表
        self._overrides.update(self.params_table.collect())
        spec = bridge.engine_spec(self._engine_id())
        self.engine_note.set_hint(spec.note)
        models_label = spec.models_label
        if spec.use_module_defaults:
            self.url_edit.setPlaceholderText(
                f"留空 = 引擎默认网关（{spec.module.DEFAULT_BASE_URL}）")
            self.models_edit.setPlaceholderText(f"留空 = 引擎默认（{models_label}）")
            self.key_edit.setPlaceholderText("留空 = 环境变量 / 脚本兜底值")
        else:
            # OpenAI 兼容引擎：没有通用默认网关，必填
            self.url_edit.setPlaceholderText("必填，如 https://api.deepseek.com/v1")
            self.models_edit.setPlaceholderText(f"如 {models_label}（逗号分隔即降级链）")
            if spec.require_key:
                self.key_edit.setPlaceholderText(
                    f"必填，或设置环境变量 {spec.key_envs_label}")
            else:
                self.key_edit.setPlaceholderText("留空 = 环境变量")
        self.models_hint.set_hint(f"逗号分隔即降级链，如 {models_label}")
        self.params_table.populate(self._engine_id(), self._overrides)
        self._fit_form()
        self._fit_height()

    # -------------------------------------------------------------- 版式
    def showEvent(self, event):  # noqa: N802 —— Qt 命名
        super().showEvent(event)
        self._fit_height()

    def resizeEvent(self, event):  # noqa: N802 —— Qt 命名
        super().resizeEvent(event)
        self._fit_form()

    def _fit_height(self) -> None:
        """让参数表一次放下所有行（受屏幕高度限制）。

        表格默认 sizeHint 是固定值，8 个参数必然出现半行被裁的滚动区；
        打开时把弹窗长到刚好放下整行，屏幕上放不下才退回滚动。
        """
        if self._fitting_form or not self.isVisible():
            return
        table = self.params_table
        needed = (sum(table.rowHeight(r) for r in range(table.rowCount()))
                  + table.horizontalHeader().height() + 2)
        extra = needed - table.viewport().height()
        if extra <= 0:
            return
        available = self.screen().availableGeometry().height() if self.screen() else 0
        target = self.height() + extra
        if available:
            target = min(target, int(available * 0.92))
        if target > self.height():
            self._fitting_form = True
            try:
                self.resize(self.width(), target)
            finally:
                self._fitting_form = False

    def _fit_form(self) -> None:
        """把表单宿主钉成「当前宽度下表单真正需要的高度」。

        QFormLayout 有 heightForWidth：宽度一变，说明文字可能多折一行。
        高度精确后，多出来的高度全部归参数表，字段行距恒定不变。
        """
        if self._fitting_form or not self.isVisible():
            return
        width = self.form_host.width()
        if width <= 0:
            return
        needed = self._form.heightForWidth(width)
        if needed > 0 and self.form_host.height() != needed:
            self._fitting_form = True
            try:
                self.form_host.setFixedHeight(needed)
            finally:
                self._fitting_form = False

    def _on_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            self.name_edit.setFocus()
            self.name_edit.setToolTip("配置名称不能为空")
            return  # 简单校验：名称必填（弹窗不关，用户补上即可）
        spec = bridge.engine_spec(self._engine_id())
        if not spec.use_module_defaults and not self.url_edit.text().strip():
            QMessageBox.warning(
                self, "缺少 Base URL",
                f"引擎「{spec.label}」没有通用默认网关，必须填写 API Base URL"
                f"（如 https://api.deepseek.com/v1）。")
            self.url_edit.setFocus()
            return
        self.accept()

    def profile(self) -> ApiProfile:
        self._overrides.update(self.params_table.collect())
        engine = self._engine_id()
        # 只保留本引擎适用的参数键（切换引擎时表里看不见的键不落盘，配置保持干净）
        valid = {k for k, s in PARAM_SPECS.items() if engine in s.engines}
        params = {k: v for k, v in self._overrides.items() if k in valid}
        return ApiProfile(
            name=self.name_edit.text().strip() or "未命名",
            engine=engine,
            base_url=self.url_edit.text().strip(),
            api_key=self.key_edit.text().strip(),
            models=self.models_edit.text().strip(),
            params=params,
        )
