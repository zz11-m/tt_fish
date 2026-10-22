# SPDX-License-Identifier: GPL-3.0-only
"""截图节奏与按键边沿检测。"""
import math
from .config import capture_interval, hotkey_vk


class CaptureSchedule:
    def __init__(self, cfg):
        self.interval = capture_interval(cfg)
        self.next_at = 0.0

    def request_earlier(self, deadline, started, finished):
        if deadline is not None:
            self.next_at = min(self.next_at, max(started+.025, finished, deadline))

    def due(self, now):
        return now >= self.next_at

    def advance(self, started, finished, deadline=None):
        target = started + self.interval
        # 将结果页剩余采样均匀排到下一次关闭时刻；不额外增加截图次数。
        if deadline is not None and deadline >= target:
            slots = max(1, math.floor((deadline-started) / self.interval + 1e-9))
            target = started + (deadline-started)/slots
        # 卡顿后不补抓积压帧，避免连续截图造成负载尖峰。
        self.next_at = max(target, finished)


class Hotkeys:
    def __init__(self, cfg):
        self.bindings = (
            (hotkey_vk(cfg["toggle_hotkey"]), "toggle"),
            (hotkey_vk(cfg["snapshot_hotkey"]), "snapshot"),
            (hotkey_vk(cfg["pause_hotkey"]), "pause"),
        )
        self.pause_vk = hotkey_vk(cfg["pause_hotkey"])
        self.previous = {key: False for key, _ in self.bindings}

    def poll(self, key_down):
        commands = []
        for key, command in self.bindings:
            down = key_down(key)
            if down and not self.previous[key]:
                commands.append(command)
            self.previous[key] = down
        return commands
