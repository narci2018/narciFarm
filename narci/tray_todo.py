#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可运行入口：仅为定位托盘图标残留问题而构造的测试用入口。

真实项目入口仍是 main.py / python main.py。
"""
import os
import sys

# 仅用于本地怀疑目录时定位问题；不碰已安装的 narciFarm 包。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(PROJECT_DIR) and PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import narci.config as cfg  # noqa: E402
from narci.events import bus  # noqa: E402
