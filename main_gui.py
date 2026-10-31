# -*- coding: utf-8 -*-
"""GUI 启动器：``python main_gui.py``（或在 VS Code 里直接运行本文件）。

为什么多这一层薄壳：底层脚本（batch.py / handout_maker.py …）都平铺在
工程根目录，GUI 包（gui/）需要保证工程根目录在 sys.path 上才能 import 到它们。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
