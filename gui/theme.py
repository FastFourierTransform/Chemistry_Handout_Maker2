# -*- coding: utf-8 -*-
"""界面排版系统：字体族、字阶、字色、行距的唯一事实来源。

**字阶（px，Qt 逻辑像素，随系统缩放）**

===========  ======  ======  ==========================  ==================================
角色         字号    字重    字色                        用在哪
===========  ======  ======  ==========================  ==================================
显示          16      700     ink                         面板标题（「模型 / API 配置」）
正文 / 值     13      400     ink                         输入框、值、控件、成句中文
字段标签      13      400     label                       表单标签、预览卡左列
提示 / 说明    12      400     muted                       卡片脚注、引擎说明、参数说明、占位符
结构标签      12      700     muted                       QGroupBox 标题、表头、面板小标题
机器 / 数据   12      400     ink                         日志流、URL、模型链（等宽）
===========  ======  ======  ==========================  ==================================

**三条硬规则**

1. 只有 400 / 700 两档字重 —— 微软雅黑 UI 只有 Regular 与 Bold，500/600 会被静默
   吸附到这两档，写出来只会误导后来的人；
2. 13px 是「值」的尺寸、12px 是「说明与结构」的尺寸，两个档位各自只承担一种职责；
   层次再靠字重、字色、留白和字体族补，不靠把正文调小；
3. 界面只有两个字体族：微软雅黑 UI（中文优先的 Windows 桌面栈，自带拉丁字形）
   与 Cascadia Mono（机器输出 / 标识符，12px）。

**为什么有 ``prose()``**：Qt 的 QSS 没有 ``line-height``，多行中文只能用富文本
给段落行距；``prose()`` 把行距固定成 1.55，避免每处提示各写各的。

**为什么没有缩放参数**：QSS 的 px 是设备无关像素，Qt 会按系统 DPI 缩放；
现阶段不做应用内字号缩放。要做的话只需要把下面三个 PX_* 常量乘一个系数，
再让 ``ui_font()`` 跟着走 —— 界面里没有第二处写死字号的地方。
"""

from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QSizePolicy

# ---------------------------------------------------------------- 字体族
# 中文优先：微软雅黑 UI 是 Win8+ 的界面字体（自带拉丁字形，不必再挂 Segoe UI）。
UI_FAMILIES = '"Microsoft YaHei UI", "Microsoft YaHei", sans-serif'
MONO_FAMILIES = '"Cascadia Mono", "Consolas", monospace'
# 富文本里的 font-family（属性写法，带引号）
_MONO_INLINE = "font-family:'Cascadia Mono','Consolas';"

# ---------------------------------------------------------------- 字阶
PX_DISPLAY = 16   # 面板 / 页面标题
PX_BODY = 13      # 正文、值、控件
PX_META = 12      # 结构标签、表头、说明、日志、等宽数据

# ---------------------------------------------------------------- 字色
INK = "#1f2328"          # 值、正文、标题          15.8:1 on #ffffff
LABEL = "#3d444d"        # 字段标签                 9.9:1
MUTED = "#57606a"        # 提示、结构标签、占位符    6.4:1
PLACEHOLDER = MUTED      # 占位符也是要读的说明：沿用提示色（Qt 默认约 3.2:1，不达标）

PROSE_LINE_HEIGHT = 1.55  # 中文段落行距（Qt 富文本）


# ---------------------------------------------------------------- 应用级
def ui_font() -> QFont:
    """应用字体：与 QSS 的字阶一致（原先 9.5pt 与 QSS 的 13px 是两套权威）。"""
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(PX_BODY)
    return font


def stylesheet() -> str:
    """整份 QSS。所有字号只在这里出现一次，其余界面代码不写死字号。"""
    return f"""
QWidget {{
    color: {INK};
    font-family: {UI_FAMILIES};
    font-size: {PX_BODY}px;
}}
QMainWindow, QDialog {{
    background: #f6f8fa;
}}
/* ---- 标题：显示级（16/700） ------------------------------------------ */
QLabel#sectionTitle {{
    font-size: {PX_DISPLAY}px;
    font-weight: 700;
    color: {INK};
}}
/* ---- 结构标签：12/700（分组框、卡片、面板小标题） ---------------------- */
QLabel#groupHeading {{
    font-size: {PX_META}px;
    font-weight: 700;
    color: {MUTED};
}}
/* ---- 字段标签：13/400，比「值」暗一档 -------------------------------- */
QLabel#fieldLabel {{
    color: {LABEL};
}}
/* ---- 说明 / 元数据：12/400（多行时用 theme.prose() 给 1.55 行距） ------ */
QLabel#dimLabel {{
    font-size: {PX_META}px;
    color: {MUTED};
}}
/* ---- 机器 / 数据：12px 等宽 ------------------------------------------ */
QLabel#codeValue {{
    font-family: {MONO_FAMILIES};
    font-size: {PX_META}px;
    color: {INK};
}}
QGroupBox {{
    border: 1px solid #d0d7de;
    border-radius: 8px;
    margin-top: 15px;
    padding-top: 9px;
    background: #ffffff;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    font-size: {PX_META}px;
    font-weight: 700;
    color: {MUTED};
}}
QPushButton {{
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 5px 14px;
    color: #24292f;
}}
QPushButton:hover {{ background: #f3f4f6; border-color: #8c959f; }}
QPushButton:pressed {{ background: #eaeef2; }}
QPushButton:focus {{ border-color: #2f6fed; }}   /* 键盘焦点：和输入框同一个信号 */
QPushButton:disabled {{
    color: #8c959f;
    background: #f6f8fa;
    border-color: #d8dee4;
}}
QPushButton#primaryButton {{
    background: #2f6fed;
    border: 1px solid #2f6fed;
    color: #ffffff;
    font-weight: 700;
    padding: 6px 22px;
}}
QPushButton#primaryButton:hover {{ background: #3d7bf5; border-color: #3d7bf5; }}
QPushButton#primaryButton:focus {{ background: #2358cf; border-color: #1a4fc4; }}
QPushButton#primaryButton:disabled {{
    background: #a9c3f2;
    border-color: #a9c3f2;
    color: #f2f6fc;
}}
QPushButton#dangerButton {{
    background: #ffffff;
    border: 1px solid #f0a8a8;
    color: #cf222e;
}}
QPushButton#dangerButton:hover {{ background: #fff0f0; }}
QPushButton#dangerButton:disabled {{
    background: #fafbfc;
    border-color: #eadada;
    color: #c2a1a1;
}}
QPushButton#miniButton {{ padding: 2px 10px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 4px 8px;
    color: {INK};
    placeholder-text-color: {PLACEHOLDER};
    selection-background-color: #2f6fed;
    selection-color: #ffffff;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QPlainTextEdit:focus {{
    border: 1px solid #2f6fed;
}}
QPlainTextEdit {{
    font-family: {MONO_FAMILIES};
    font-size: {PX_META}px;
    background: #fbfcfd;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: #ffffff;
    border: 1px solid #d0d7de;
    selection-background-color: #2f6fed;
    selection-color: #ffffff;
}}
QTableWidget {{
    background: #ffffff;
    alternate-background-color: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 8px;
    gridline-color: transparent;
}}
QTableWidget::item {{ padding: 2px 6px; border: none; }}
QTableWidget::item:selected {{ background: #ddf4ff; color: {INK}; }}
QTableWidget:focus {{ border: 1px solid #2f6fed; }}   /* 键盘焦点：表格也要看得出来 */
QHeaderView::section {{
    background: #f6f8fa;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid #d0d7de;
    padding: 6px 8px;
    font-size: {PX_META}px;
    font-weight: 700;
}}
QProgressBar {{
    background: #eaeef2;
    border: 1px solid #d0d7de;
    border-radius: 7px;
    height: 18px;
    text-align: center;
    font-size: {PX_META}px;
    font-weight: 700;
    color: {INK};
}}
QProgressBar::chunk {{
    background: #2f6fed;
    border-radius: 6px;
}}
/* ---- 批次相位：暂停是待办（琥珀），终止是收工（灰） --------------------- */
QProgressBar[phase="pausing"]::chunk,
QProgressBar[phase="paused"]::chunk {{
    background: #bf8700;
}}
QProgressBar[phase="stopping"]::chunk {{
    background: #8c959f;
}}
QLabel#dimLabel[phase="pausing"],
QLabel#dimLabel[phase="paused"] {{
    color: #9a6700;
}}
QLabel#dimLabel[phase="stopping"] {{
    color: {MUTED};
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: #c1c8d0;
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: #9aa3af; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{
    background: #c1c8d0;
    border-radius: 4px;
    min-width: 24px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QSplitter::handle {{ background: #f6f8fa; }}
QSplitter::handle:hover {{ background: #2f6fed; }}
QMenuBar {{ background: #ffffff; border-bottom: 1px solid #d0d7de; }}
QMenuBar::item {{ padding: 5px 10px; }}
QMenuBar::item:selected {{ background: #eaeef2; border-radius: 5px; }}
QMenu {{
    background: #ffffff;
    border: 1px solid #d0d7de;
    padding: 4px;
}}
QMenu::item {{ padding: 5px 26px 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: #2f6fed; color: #ffffff; }}
QMenu::separator {{ height: 1px; background: #d8dee4; margin: 4px 8px; }}
QStatusBar {{ background: #ffffff; color: {MUTED}; border-top: 1px solid #d0d7de; }}
QToolTip {{
    background: {INK};
    color: #ffffff;
    border: none;
    padding: 4px 8px;
}}
/* 覆盖确认弹窗里的产物清单：只读的名单，不是控件 */
QLabel#pathBox {{
    background: #f7f9fb;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 8px 10px;
}}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
QMessageBox {{ background: #ffffff; }}
QMessageBox QLabel {{ color: {INK}; }}
"""


# ---------------------------------------------------------------- 富文本零件
def prose(text: str, color: str = MUTED) -> str:
    """多行中文提示：12px + 1.55 行距（QSS 无 line-height，只能走富文本）。"""
    return (f'<div style="font-size:{PX_META}px; '
            f'line-height:{PROSE_LINE_HEIGHT}; '
            f'color:{color};">{html.escape(text)}</div>')


def mono(text: str) -> str:
    """等宽片段：URL、模型标识符、参数串（12px，与日志同族）。"""
    return (f'<span style="{_MONO_INLINE} font-size:{PX_META}px;">'
            f'{html.escape(text)}</span>')


def annotated(note: str, value: str, mono_value: bool = True) -> str:
    """「（备注）值」：备注走 muted 12px，值走等宽 12px —— 元数据与数据分开。"""
    parts = []
    if note:
        parts.append(f'<span style="color:{MUTED}; font-size:{PX_META}px;">'
                     f'{html.escape(note)}</span>')
    if value:
        parts.append(mono(value) if mono_value else html.escape(value))
    return " ".join(parts) if parts else ""


class _WrappedText(QLabel):
    """会按真实宽度折行的富文本标签。

    细节：QLabel 对富文本的 ``sizeHint`` 是与宽度无关的方形估算（实测一句 3 行
    的说明会报 124px 高），``minimumSizeHint`` 也不含折行信息 —— 放进
    QFormLayout / QBoxLayout 时，行会被撑高（多余高度再摊回别的行）或被压扁
    （文字叠到下一行上）。这里让 sizeHint / minimumSizeHint 都等于「当前宽度
    下真正需要的高度」，宽度则刻意报小值 —— 文字不该反过来决定容器宽度。
    """

    BUDGET_WIDTH = 480   # 还没有真实宽度时的折行预算

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def _wrapped_height(self) -> int:
        width = self.width() if self.width() > 0 else self.BUDGET_WIDTH
        height = self.heightForWidth(width)
        return height if height > 0 else super().sizeHint().height()

    def sizeHint(self):  # noqa: N802 —— Qt 命名
        return QSize(160, self._wrapped_height())

    def minimumSizeHint(self):  # noqa: N802 —— Qt 命名
        return QSize(60, self._wrapped_height())

    def resizeEvent(self, event):  # noqa: N802 —— Qt 命名
        width_changed = event.oldSize().width() != event.size().width()
        super().resizeEvent(event)
        if width_changed:
            self.updateGeometry()


class HintLabel(_WrappedText):
    """说明文字：12px、1.55 行距、muted 色。"""

    BUDGET_WIDTH = 480

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("dimLabel")
        if text:
            self.setText(prose(text))

    def set_hint(self, text: str) -> None:
        """设置提示正文（自动加行距与字色包装）。"""
        self.setText(prose(text))


class ValueLabel(_WrappedText):
    """预览卡里的「值」：可折行的富文本，等宽片段由 annotated() 拼。"""

    BUDGET_WIDTH = 380

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("—")

