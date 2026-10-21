# SPDX-License-Identifier: GPL-3.0-only
"""直接运行即可检查样例识别、状态决策和输入保护；不控制模拟器。"""
import sys
import unittest
from pathlib import Path

if __name__ == "__main__":
    root=Path(__file__).resolve().parent
    suite=unittest.defaultTestLoader.discover(str(root/"tests"),pattern="test_*.py")
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
