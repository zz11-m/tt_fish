# SPDX-License-Identifier: GPL-3.0-only
"""纯状态决策；不截图、不按键，可使用截图、录像和合成轨迹离线验证。"""
from collections import deque
from dataclasses import dataclass
import math
import random
import numpy as np
from .config import capture_interval
from .contact import ContactModel, DeliveryLatency
from .speed import SpeedCalibration, DEFAULT_SPEED
from .phase import PhaseModel

@dataclass
class Decision:
    action: str | None = None
    reason: str = "观察中"
    pause: bool = False
    predicted_x: float | None = None
    velocity: float = 0.0
    uncertainty: float = 0.0
    check_at: float | None = None
    send_at: float | None = None
    contact_x: float | None = None
    round_id: int = 0
    motion_event: str = ""
    fit_error: float = 0.0
    latency_ms: float = 0.0
    frame_age_ms: float = 0.0
    feedback: str = ""
    retry_count: int = 0
    pause_code: str = ""
    arrival_ms: float | None = None
    arrival_direction: int = 0
    planned_turns: int = 0
    endpoint_source: str = ""
    click_mode: str = ""
    valid_until: float | None = None

class Motion:
    def __init__(self, interval=1/60):
        self.points = deque(maxlen=10)
        self.velocity = 0.0
        self.reset_detected = False
        self.window = max(.13, interval*3.2)
        self.max_gap = max(.20, interval*2.5)
        self.max_age = max(.12, interval*1.5)

    def clear(self):
        self.points.clear()
        self.velocity = 0.0

    def add(self, t, x):
        self.reset_detected = False
        if self.points:
            last_t, last_x = self.points[-1]
            dt = t-last_t
            if dt <= 0:
                return
            if dt > self.max_gap or abs(x-last_x) > max(32, max(200, abs(self.velocity))*dt*2.5+12):
                self.reset_detected = dt <= self.max_gap
                self.clear()
        self.points.append((t, x))
        while self.points and t-self.points[0][0] > self.window:
            self.points.popleft()

    def estimate(self, t):
        if len(self.points) < 3 or t-self.points[-1][0] > self.max_age:
            return None
        ts = np.array([p[0]-t for p in self.points], dtype=float)
        xs = np.array([p[1] for p in self.points], dtype=float)
        if np.ptp(ts) < .025 or np.ptp(xs) < 1.0:
            return None
        # 短窗口线性回归；反弹、加速和抖动表现为残差，收缩可按键的安全范围。
        slope, intercept = np.polyfit(ts, xs, 1)
        residual = float(np.sqrt(np.mean((xs-(slope*ts+intercept))**2)))
        if abs(slope) > 1800 or residual > 8:
            return None
        self.velocity = float(slope)
        dt = float(np.median(np.diff(ts)))
        return float(intercept), float(slope), residual, dt

class Controller:
    def __init__(self, cfg, rng=None):
        self.cfg = cfg
        self.rng = rng if rng is not None else random.Random()
        self.latency = DeliveryLatency(cfg["input_latency_ms"], cfg.get("adaptive_latency", True))
        self.speed=SpeedCalibration(cfg.get("speed_fraction_per_s",DEFAULT_SPEED),cfg.get("calibrate_speed",False))
        self.reset(0.0)

    def reset(self, now):
        self.started = now
        self.state = "UNKNOWN"
        self.candidate = "UNKNOWN"
        self.candidate_count = 0
        self.pending_timing = None
        self.entered = now
        self.unknown_since = None
        self.last_press = -math.inf
        self.pending_ack = None
        self.cast_count = 0
        self.result_count = 0
        self.result_presses = 0
        self.next_result_press_at = None
        self.motion = Motion(capture_interval(self.cfg))
        self.contact = ContactModel(self.cfg, self.latency,self.speed)
        self.phase = PhaseModel(self.cfg, self.latency,self.speed)
        self.target = None
        self.target_candidate = None
        self.target_count = 0
        self.shot_locked = False
        self.shot_target = None
        self.round_reset_seen = False

    @staticmethod
    def distance(a, b):
        return max(abs(a[0]-b[0]), abs(a[1]-b[1]))

    def _transition(self, new, now):
        old = self.state
        self.state, self.entered = new, now
        if self.pending_ack and new != self.pending_ack[0]:
            self.pending_ack = None
        if new == "RESULT":
            self.result_count += 1
            self.result_presses = 0
            self.next_result_press_at = now + self.cfg["result_wait_s"]
        elif old == "RESULT":
            self.next_result_press_at = None
        if new == "TIMING" and old != "TIMING":
            # 短暂丢失不会走到这里（UNKNOWN不会覆盖已确认状态）。
            self.motion.clear()
            self.contact = ContactModel(self.cfg, self.latency,self.speed)
            self.phase = PhaseModel(self.cfg, self.latency,self.speed)
            self.target = self.target_candidate = None
            self.target_count = 0
            self.shot_locked = False
            self.shot_target = None

    def commit(self, action, now):
        """仅在真实按键成功后调用；离线回放可显式模拟提交。"""
        self.last_press = now
        if action == "CAST":
            self.cast_count += 1
            self.pending_ack = ("IDLE", now)
        elif action == "HOOK":
            self.pending_ack = ("BITE", now)
        elif action == "HIT":
            self.contact.commit(now)
            self.phase.commit(now)
            self.shot_locked = True
            self.shot_target = self.target
            self.round_reset_seen = False
        elif action == "CLOSE":
            self.result_presses += 1
            jitter = self.cfg["result_retry_jitter_ms"] / 1000
            self.next_result_press_at = now + self.cfg["result_retry_s"] + self.rng.uniform(-jitter, jitter)

    def step(self, obs, frame_time, now=None):
        now = frame_time if now is None else now
        if not self.cfg["continuous_fishing"] and now-self.started >= self.cfg["max_session_minutes"]*60:
            return Decision(reason="达到本次运行时长上限", pause=True, pause_code="duration_limit")
        if obs.state == "UNKNOWN":
            self.contact.motion.clear("unknown")
            self.contact.candidate, self.contact.target_count = None, 0
            self.candidate = "UNKNOWN"
            self.candidate_count = 0
            self.pending_timing = None
            self.unknown_since = now if self.unknown_since is None else self.unknown_since
            limit=self.cfg["unknown_timeout_s"]
            if limit and now-self.unknown_since >= limit:
                return Decision(reason="画面长时间无法确认，已暂停；检查遮挡、弹窗和框选范围", pause=True, pause_code="unknown_timeout")
            elapsed=max(0,now-self.unknown_since)
            return Decision(reason=f"画面未确认，已等待{elapsed:.0f}秒"+
                            (f"／{limit:g}秒" if limit else "（不自动超时）"))
        self.unknown_since = None
        if obs.state == self.candidate:
            self.candidate_count += 1
        else:
            self.candidate, self.candidate_count = obs.state, 1
            self.pending_timing = (obs, frame_time) if obs.state == "TIMING" else None
        fast_timing = (self.cfg.get("timing_mode") == "phase" and obs.confidence >= .68 and
                       (obs.state == "BITE" or (obs.state == "TIMING" and obs.track_left is not None)))
        if self.candidate_count < 2 and not fast_timing:
            check_at = now+.025 if obs.state == "TIMING" and self.cfg.get("timing_mode") == "contact" else None
            return Decision(reason="等待连续帧确认", check_at=check_at)
        if obs.state != self.state:
            self._transition(obs.state, now)
            if obs.state == "TIMING" and self.pending_timing is not None and self.cfg.get("timing_mode") != "phase":
                # 确认前不按键；确认后保留第一张判定图的边界和运动样本。
                previous, previous_time = self.pending_timing
                self._timing(previous, previous_time, now)
            self.pending_timing = None
        if self.pending_ack:
            if now-self.pending_ack[1] > self.cfg["action_ack_timeout_s"]:
                return Decision(reason="按键后画面未进入下一阶段，已暂停；请检查空格映射", pause=True, pause_code="action_ack_timeout")
            return Decision(reason="已按键，等待阶段变化")
        if self.state == "WAIT":
            expired = now-self.entered > self.cfg["wait_timeout_s"]
            return Decision(reason="等待上钩超时，已暂停" if expired else "等待鱼上钩",
                            pause=expired, pause_code="wait_timeout" if expired else "")
        if self.state == "IDLE":
            if not self.cfg["continuous_fishing"] and self.cast_count >= self.cfg["max_casts"]:
                return Decision(reason="达到下钩次数上限", pause=True, pause_code="cast_limit")
            if now-self.entered >= .30 and now-self.last_press >= .6:
                return Decision("CAST", "初始画面已确认，下钩一次")
        elif self.state == "BITE":
            if now-self.last_press >= self.cfg["min_action_interval_s"]:
                return Decision("HOOK", "上钩提示已确认，拉钩一次")
        elif self.state == "RESULT":
            if now-self.entered < self.cfg["result_wait_s"]:
                return Decision(reason="等待结果动画")
            if self.next_result_press_at is not None and now < self.next_result_press_at:
                return Decision(reason="等待结果页关闭")
            return Decision("CLOSE", "结果页持续可见，尝试空格关闭")
        elif self.state == "TIMING":
            return self._timing(obs, frame_time, now)
        return Decision(reason="等待画面稳定")

    def _timing(self, obs, t, now):
        limit=self.cfg.get("timing_timeout_s",120)
        if limit and now-self.entered > limit:
            return Decision(reason="判定阶段超时，已暂停",pause=True,pause_code="timing_timeout")
        if self.cfg.get("timing_mode") == "phase":
            result = Decision(**self.phase.step(obs,t,now))
            self.target, self.shot_locked = self.phase.target, self.phase.locked
            return result
        if self.cfg.get("timing_mode", "center") == "contact":
            result = Decision(**self.contact.step(obs, t, now))
            self.target = self.contact.target
            self.shot_locked = self.contact.locked
            return result
        if obs.marker is None or obs.target is None:
            self.motion.clear()
            # 没找到黄色区域不等于新一轮，不能因此解锁连按。
            return Decision(reason="判定条或浮标暂未识别")
        self.motion.add(t, obs.marker)
        reset = self.motion.reset_detected
        changed = False
        if obs.geometry_clear:
            if self.target_candidate and self.distance(obs.target, self.target_candidate) <= 2:
                self.target_count += 1
            else:
                self.target_candidate, self.target_count = obs.target, 1
            if self.target_count >= 2:
                changed = self.shot_target is not None and self.distance(obs.target, self.shot_target) >= 3
                self.target = obs.target
        if self.target is None:
            return Decision(reason="等待浮标离开黄条，确认完整目标边界")
        if self.shot_locked:
            self.round_reset_seen = self.round_reset_seen or reset
            # 短暂识别失败可能只是浮标遮住轨道端点，不能单独当成新一轮证据。
            if now-self.last_press >= .28 and (changed or self.round_reset_seen):
                self.shot_locked = False
                self.round_reset_seen = False
                if not changed:
                    self.target = self.target_candidate = None
                    self.target_count = 0
                self.motion.clear()
                self.motion.add(t, obs.marker)
                return Decision(reason="检测到下一轮变化，重新估计速度")
            if now-self.last_press > 6:
                return Decision(reason="按键后未确认下一轮，已暂停", pause=True, pause_code="hit_ack_timeout")
            return Decision(reason="本轮已触发，等待黄条变化或浮标复位")
        if now-self.last_press < self.cfg["min_action_interval_s"]:
            return Decision(reason="按键间隔保护")
        estimate = self.motion.estimate(t)
        if estimate is None:
            return Decision(reason="估计浮标速度")
        x, v, residual, dt = estimate
        age = max(0.0, now-t)
        if age > .08:
            return Decision(reason="截图处理延迟过高，本帧跳过", velocity=v)
        delay = self.cfg["input_latency_ms"]/1000 + age
        predicted = x + v*delay
        uncertainty = max(1.5, 2*residual) + abs(v)*(self.cfg["latency_uncertainty_ms"]/1000 + dt*.25)
        margin = self.cfg["target_margin_px"] + uncertainty
        left, right = self.target
        safe_left, safe_right = left+margin, right-margin
        d = Decision(predicted_x=predicted, velocity=v, uncertainty=uncertainty)
        if safe_left >= safe_right:
            d.reason = "黄条过窄或速度不稳定，等待更可靠的机会"
        elif safe_left <= predicted <= safe_right:
            d.action, d.reason = "HIT", "预测生效位置位于黄条安全范围"
        else:
            d.reason = "等待浮标进入可命中范围"
        return d
