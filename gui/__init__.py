# -*- coding: utf-8 -*-
"""Handout-Maker2 桌面客户端（PySide6）。

分层约定（也是本包与底层脚本的唯一解耦点）：

    widgets/  ──信号/槽──▶  main_window  ──▶  core/worker（QThread）
                                              │
                                              └──▶ core/bridge ──▶ batch.py / handout_maker*.py

任何 widget 都**不得** import batch.py / handout_maker.py；只有 core/bridge.py 可以。
"""
