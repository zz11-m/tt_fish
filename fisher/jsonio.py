# SPDX-License-Identifier: GPL-3.0-only
"""运行记录的JSON编码边界；只补充NumPy标量，不隐式转换任意对象。"""
import json
import numpy as np


def scalar_default(value):
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    raise TypeError(f"不支持的JSON字段类型：{type(value).__name__}")


def dumps(value, **options):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      default=scalar_default, **options)
