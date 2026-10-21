# SPDX-License-Identifier: GPL-3.0-only
"""配置校验、旧版迁移与原子保存。"""
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_VERSION = 2
REFERENCE_FPS = 60
HOTKEYS = tuple(f"F{i}" for i in range(1, 13))
DEFAULTS = {
    "config_version": CONFIG_VERSION,
    "dry_run": False, "continuous_fishing": False,
    "capture_every_n_frames": 6,
    "toggle_hotkey": "F8", "pause_hotkey": "F9", "snapshot_hotkey": "F10",
    "input_latency_ms": 40, "latency_uncertainty_ms": 12, "key_hold_ms": 35,
    "target_margin_px": 3, "min_action_interval_s": 0.24,
    "result_wait_s": 3.0, "result_retry_s": 1.0, "result_retry_jitter_ms": 40,
    "unknown_timeout_s": 12, "wait_timeout_s": 120,
    "action_ack_timeout_s": 5, "max_casts": 20,
    "max_session_minutes": 15, "template_threshold": 0.68, "profile": None,
}


def capture_interval(cfg):
    return cfg["capture_every_n_frames"] / REFERENCE_FPS


def hotkey_vk(name):
    return 0x70 + HOTKEYS.index(name)


def validate_config(data):
    if not isinstance(data, dict):
        raise ValueError("配置文件应为JSON对象")
    cfg = dict(DEFAULTS)
    cfg.update(data)
    for key in ("dry_run", "continuous_fishing"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} 必须为 true 或 false")
    limits = {
        "capture_every_n_frames": (1, 60), "input_latency_ms": (0, 250),
        "latency_uncertainty_ms": (0, 100), "key_hold_ms": (10, 150),
        "target_margin_px": (0, 20), "min_action_interval_s": (0.15, 5),
        "result_wait_s": (1, 30), "result_retry_s": (0.5, 15),
        "result_retry_jitter_ms": (0, 100), "unknown_timeout_s": (2, 120),
        "wait_timeout_s": (10, 600), "action_ack_timeout_s": (2, 20),
        "max_casts": (1, 10000), "max_session_minutes": (1, 1440),
        "template_threshold": (0.5, 0.99),
    }
    for key, (lo, hi) in limits.items():
        x = cfg[key]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo <= x <= hi:
            raise ValueError(f"配置 {key} 应为 {lo}～{hi} 的有限数值，实际为 {x!r}")
    for key in ("max_casts", "capture_every_n_frames"):
        if int(cfg[key]) != cfg[key]:
            raise ValueError(f"配置 {key} 必须为整数")
        cfg[key] = int(cfg[key])
    for key in ("toggle_hotkey", "pause_hotkey", "snapshot_hotkey"):
        value = cfg[key]
        if not isinstance(value, str) or value.strip().upper() not in HOTKEYS:
            raise ValueError(f"配置 {key} 应为 F1～F12 中的一个键")
        cfg[key] = value.strip().upper()
    if len({cfg[k] for k in ("toggle_hotkey", "pause_hotkey", "snapshot_hotkey")}) != 3:
        raise ValueError("开始／暂停、紧急暂停、保存截图的快捷键不能重复")
    cfg["config_version"] = CONFIG_VERSION
    cfg.pop("capture_fps", None)
    cfg.pop("result_max_presses", None)
    return cfg


def load_config(path=None):
    path = Path(path) if path else ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("配置文件应为JSON对象")
    if "config_version" not in data:
        # 旧版保留框选及输入参数，截图与结果页采用新版默认节奏。
        for key in ("result_wait_s", "result_retry_s", "result_retry_jitter_ms", "capture_every_n_frames"):
            data[key] = DEFAULTS[key]
    return validate_config(data)


def save_config(cfg):
    cfg = validate_config(cfg)
    path = ROOT / "config.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
