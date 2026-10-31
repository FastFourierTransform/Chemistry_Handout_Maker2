# -*- coding: utf-8 -*-
"""模型与 API 管理面板：配置档案下拉选择 + 预览 + 新建/编辑/删除/测试连接。

排版结构（见 gui/theme.py 的字阶表）：
* 第一行：面板标题（显示级 16/700）+ 四个次要动作按钮右对齐；
* 第二行：配置下拉独占整行 —— 整条模型降级链能完整显示，不再被控件挤到硬裁；
* 卡片：标签列固定 78px、值左对齐、行距 10px；卡片按内容收高，行距不被面板高度摊开；
* 窗口压到最小时整块内容由滚动区兜底（宁可滚动，也不把行压扁让文字叠在一起）。

配置的持久化由 ConfigStore 负责（JSON 原子写），面板每次改动立刻落盘，
下次启动自动加载。面板本身不 import 底层脚本 —— 引擎清单与默认值都来自
core.bridge（引擎注册表）与 core.models（参数注册表）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from gui import theme
from gui.core import bridge
from gui.core.config_store import ConfigStore
from gui.core.models import ApiProfile, PARAM_SPECS
from gui.widgets.profile_dialog import ProfileDialog

# 卡片左列宽度：容得下最长的标签「模型降级链」（5 个汉字 = 65px）+ 13px 栏间距
_LABEL_COLUMN = 78


class ModelPanel(QWidget):
    profile_changed = Signal(int)   # 下拉框选择变化（配置已落盘）
    profiles_edited = Signal()      # 新建/编辑/删除后（配置已落盘）
    test_requested = Signal(int)    # 请求后台跑 API 连通性自检

    def __init__(self, store: ConfigStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._testing = False

        title = QLabel("模型 / API 配置")
        title.setObjectName("sectionTitle")

        self.combo = QComboBox()
        self.combo.setMinimumWidth(180)
        # 下拉框独占一行、横向拉伸：用最小内容长度算 sizeHint，
        # 别让最长的配置名反过来把面板撑宽（窄窗口下会被裁）
        self.combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(10)

        self.add_btn = QPushButton("新建")
        self.edit_btn = QPushButton("编辑")
        self.del_btn = QPushButton("删除")
        self.test_btn = QPushButton("测试连接")
        for button in (self.add_btn, self.edit_btn, self.del_btn, self.test_btn):
            button.setObjectName("miniButton")   # 次要操作：与标题同排，用紧凑按钮

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.add_btn)
        header.addWidget(self.edit_btn)
        header.addWidget(self.del_btn)
        header.addWidget(self.test_btn)

        picker = QHBoxLayout()
        picker.setContentsMargins(0, 0, 0, 0)
        picker.addWidget(self.combo, 1)

        # ---- 当前配置预览（只读，一眼看清要跑哪套配置）-----------------------
        preview = QGroupBox("当前配置预览")
        card = QVBoxLayout(preview)
        card.setContentsMargins(14, 12, 14, 12)
        card.setSpacing(12)

        self.pv_engine = self._value_label()
        self.pv_url = self._value_label()
        self.pv_models = self._value_label()
        self.pv_key = self._value_label()
        self.pv_params = self._value_label()
        self.pv_note = theme.HintLabel()

        rows = (
            ("引擎", self.pv_engine), ("Base URL", self.pv_url),
            ("模型降级链", self.pv_models), ("API Key", self.pv_key),
            ("高级参数", self.pv_params),
        )
        # 一行一个 HBox（而不是 QGridLayout）：QGridLayout 不认 heightForWidth，
        # 长 URL 折行时会和下一行叠在一起；QBoxLayout 会把折行高度算进行高。
        rows_box = QVBoxLayout()
        rows_box.setContentsMargins(0, 0, 0, 0)
        rows_box.setSpacing(10)
        for name, widget in rows:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(13)
            label = QLabel(name)
            label.setObjectName("fieldLabel")
            label.setFixedWidth(_LABEL_COLUMN)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            row.addWidget(label)
            row.addWidget(widget, 1)
            rows_box.addLayout(row)

        card.addLayout(rows_box)
        card.addWidget(self.pv_note)   # 卡片按内容收高，行距不被面板高度摊开

        # 面板内容整体可滚动：窗口压到最小高度时内容保持自己的行高，
        # 由滚动条兜底，而不是把预览卡的行压扁、让文字叠在一起。
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(9)
        body_layout.addLayout(header)
        body_layout.addLayout(picker)
        body_layout.addWidget(preview)
        body_layout.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.scroll)

        # 面板最小宽度 = 内容最小宽度 + 滚动条：低于这个值标题行会被裁
        # （分栏拖动 / 窗口缩到最小时由分栏遵守这个下限）
        self.setMinimumWidth(body.minimumSizeHint().width() + 14)

        # ---- 信号 -----------------------------------------------------------
        self.combo.currentIndexChanged.connect(self._on_combo)
        self.add_btn.clicked.connect(self._add_profile)
        self.edit_btn.clicked.connect(self._edit_profile)
        self.del_btn.clicked.connect(self._delete_profile)
        self.test_btn.clicked.connect(lambda: self.test_requested.emit(self.combo.currentIndex()))

        self.reload()

    @staticmethod
    def _value_label() -> QLabel:
        """预览卡里的「值」：可折行的深色正文（12px 等宽由 annotated() 片段给）。"""
        label = theme.ValueLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        return label

    # ---------------------------------------------------------------- 数据
    def reload(self):
        """从 ConfigStore 重建下拉框（启动 / 增删改后调用）。"""
        self.combo.blockSignals(True)
        self.combo.clear()
        for profile in self._store.profiles:
            suffix = ""
            if profile.models:
                suffix = f" · {profile.models.split(',')[0]}"
            self.combo.addItem(f"{profile.name}{suffix}", profile)
            tip = profile.name
            if profile.models:
                tip += f"\n模型降级链：{profile.models}"
            self.combo.setItemData(self.combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.combo.setCurrentIndex(self._store.selected_index)
        self.combo.blockSignals(False)
        self._refresh_preview()

    def current_profile(self) -> ApiProfile | None:
        data = self.combo.currentData()
        return data if isinstance(data, ApiProfile) else None

    # ---------------------------------------------------------------- 预览
    def _refresh_preview(self):
        profile = self.current_profile()
        self.combo.setToolTip(self.combo.currentText())
        if profile is None:
            for w in (self.pv_engine, self.pv_url, self.pv_models, self.pv_key, self.pv_params):
                w.setText("—")
            self.pv_note.set_hint("还没有配置：点「新建」添加一个模型 API。")
            return
        spec = bridge.engine_spec(profile.engine)
        self.pv_engine.setText(spec.label)
        if profile.base_url:
            self.pv_url.setText(theme.annotated("", profile.base_url))
            self.pv_url.setToolTip(profile.base_url)
        elif spec.use_module_defaults:
            self.pv_url.setText(
                theme.annotated("（引擎默认）", spec.module.DEFAULT_BASE_URL))
            self.pv_url.setToolTip(spec.module.DEFAULT_BASE_URL)
        else:
            self.pv_url.setText(theme.annotated("（必填，未填写）", "", mono_value=False))
        if profile.models:
            self.pv_models.setText(theme.annotated("", profile.models))
            self.pv_models.setToolTip(profile.models)
        else:
            self.pv_models.setText(
                theme.annotated("（引擎默认）", spec.models_label))
        if profile.api_key.strip():
            key_text = "已设置（●●●●●●●）"
        elif spec.require_key:
            key_text = f"未设置（必填，或用环境变量 {spec.key_envs_label}）"
        else:
            key_text = "未设置（用环境变量 / 脚本兜底）"
        self.pv_key.setText(key_text)
        overrides = []
        for key, raw in profile.params.items():
            spec_p = PARAM_SPECS.get(key)
            if spec_p and profile.engine in spec_p.engines:
                overrides.append(f"{spec_p.label}={raw}")
        self.pv_params.setText("；".join(overrides) if overrides else "（全部用默认值）")
        self.pv_note.set_hint(
            f"转换时将以这套配置调用引擎入口函数（api_key/base_url/models + "
            f"{len(PARAM_SPECS)} 个可调参数）。")

    # ---------------------------------------------------------------- 交互
    def _on_combo(self, index: int):
        if index >= 0:
            self._store.set_selected_index(index)
            self._refresh_preview()
            self.profile_changed.emit(index)

    def _add_profile(self):
        dialog = ProfileDialog(None, self)
        if dialog.exec() == ProfileDialog.DialogCode.Accepted:
            profiles = self._store.profiles
            profiles.append(dialog.profile())
            self._store.set_profiles(profiles)
            self._store.set_selected_index(len(profiles) - 1)
            self.reload()
            self.profiles_edited.emit()

    def _edit_profile(self):
        profile = self.current_profile()
        if profile is None:
            return
        dialog = ProfileDialog(profile, self)
        if dialog.exec() == ProfileDialog.DialogCode.Accepted:
            profiles = self._store.profiles
            profiles[self.combo.currentIndex()] = dialog.profile()
            self._store.set_profiles(profiles)
            self.reload()
            self.profiles_edited.emit()

    def _delete_profile(self):
        index = self.combo.currentIndex()
        if index < 0:
            return
        if self.combo.count() <= 1:
            QMessageBox.information(self, "删除配置", "至少保留一条配置。")
            return
        name = self.combo.itemText(index)
        ret = QMessageBox.question(
            self, "删除配置",
            f"确定删除配置「{name}」？\n（配置文件会立刻落盘，删除后不可恢复）")
        if ret == QMessageBox.StandardButton.Yes:
            profiles = self._store.profiles
            del profiles[index]
            self._store.set_profiles(profiles)
            self.reload()
            self.profiles_edited.emit()

    def set_testing(self, testing: bool):
        self._testing = testing
        self.test_btn.setEnabled(not testing)
        self.test_btn.setText("检测中 …" if testing else "测试连接")
