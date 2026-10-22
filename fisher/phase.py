# SPDX-License-Identifier: GPL-3.0-only
"""低频观测校正位置，连续相位负责折返和按键期限。时间均来自perf_counter。"""
import math

# 相对于轨道可见外框的鱼漂中心行程；左右装饰与端帽不属于中心行程。
LEFT_END = .083
RIGHT_END = .940


class PhaseClock:
    def __init__(self, rate, left=LEFT_END, right=RIGHT_END):
        self.rate = float(rate)
        self.left, self.right = float(left), float(right)
        self.length = self.right-self.left
        self.anchor_time = None
        self.anchor_phase = 0.0

    def phase_at(self, now):
        if self.anchor_time is None:
            return None
        return self.anchor_phase + self.rate*(now-self.anchor_time)

    def position_at(self, now):
        phase = self.phase_at(now)
        if phase is None:
            return None
        u = phase % (2*self.length)
        return self.left + (u if u < self.length else 2*self.length-u)

    def direction_at(self, now):
        phase = self.phase_at(now)
        if phase is None:
            return 1
        return 1 if phase % (2*self.length) < self.length else -1

    def observe(self, now, position):
        """把观测投到最接近预测的往返支路；不以两帧位移符号判断方向。"""
        position = min(self.right, max(self.left, position))
        u = position-self.left
        if self.anchor_time is None:
            phase = u                         # 首次观测默认向右。
        else:
            expected = self.phase_at(now)
            period = 2*self.length
            cycle = math.floor(expected/period)
            candidates = [base+k*period for k in range(cycle-1, cycle+2)
                          for base in (u, period-u)]
            phase = min(candidates, key=lambda p: abs(p-expected))
        self.anchor_time, self.anchor_phase = now, phase
        return phase

    def change_rate(self, rate, now):
        phase = self.phase_at(now)
        if phase is not None:
            self.anchor_time, self.anchor_phase = now, phase
        self.rate = float(rate)

    def next_visit(self, position, now):
        """返回下一次到达指定位置的时刻、方向和经过的端点数。"""
        phase = self.phase_at(now)
        if phase is None:
            return None
        u = min(self.length, max(0, position-self.left))
        period = 2*self.length
        candidates = []
        for base, direction in ((u, 1), (period-u, -1)):
            k = math.ceil((phase-base-1e-10)/period)
            dest = base+k*period
            if dest >= phase-1e-9:
                candidates.append((dest, direction))
        dest, direction = min(candidates)
        turns = max(0, math.floor((dest-1e-9)/self.length)-math.floor(phase/self.length))
        return now+max(0, dest-phase)/self.rate, direction, turns


class PhaseModel:
    """每轮至多一个待执行按键；同一趟不连按，下一趟允许重试。"""
    def __init__(self, cfg, latency, speed):
        self.cfg, self.latency, self.speed = cfg, latency, speed
        self.clock = PhaseClock(speed.value)
        self.track = None
        self.target = None
        self.target_clear = False
        self.last_press = -math.inf
        self.locked = False
        self.rearm_phase = None
        self.round_id = 1
        self.previous_sample = None
        self.segment = 0

    def _fraction(self, x):
        return (x-self.track[0])/(self.track[1]-self.track[0])

    def _pixel(self, x):
        return self.track[0]+x*(self.track[1]-self.track[0])

    def _target_limits(self):
        return (max(self.clock.left, self._fraction(self.target[0])),
                min(self.clock.right, self._fraction(self.target[1])))

    def commit(self, now):
        self.last_press = now
        self.previous_sample = None
        self.locked = True
        if self.target is None or self.track is None or self.clock.anchor_time is None:
            return
        effect = now+self.latency.seconds
        x, direction = self.clock.position_at(effect), self.clock.direction_at(effect)
        left, right = self._target_limits()
        # 经过当前目标的远端后解锁；只按时间冷却会在宽黄条内连续按键。
        distance = max(0, right-x if direction > 0 else x-left)
        self.rearm_phase = self.clock.phase_at(effect)+distance+.008

    def step(self, obs, frame_time, now):
        if obs.track_left is not None and obs.track_right is not None and obs.track_right > obs.track_left:
            self.track = (float(obs.track_left), float(obs.track_right))
        if self.track is None:
            return {"reason": "等待识别轨道边界"}
        old_target = self.target
        if obs.target is not None:
            visible = tuple(map(float, obs.target))
            # 浮标遮挡的单侧黄色碎片不能缩短已确认的目标。
            subset = (old_target is not None and old_target[0]-2 <= visible[0] and
                      visible[1] <= old_target[1]+2)
            if old_target is None or obs.geometry_clear or not subset:
                self.target = visible
                self.target_clear = bool(obs.geometry_clear)
        changed = (old_target is not None and self.target is not None and
                   max(abs(a-b) for a,b in zip(old_target,self.target)) > 3)
        if changed:
            self.round_id += 1
            self.locked = False
            self.rearm_phase = None

        observed = False
        if obs.marker is not None:
            x = self._fraction(float(obs.marker))
            # 轨道外的鱼竿/动效红色不作为浮标；已有相位仍继续行走。
            if self.clock.left-.03 <= x <= self.clock.right+.03:
                phase = self.clock.observe(frame_time, x)
                direction = self.clock.direction_at(frame_time)
                previous = self.previous_sample
                retain_anchor = False
                if self.speed.calibrating and previous is not None:
                    pt, px, pp = previous
                    dt = frame_time-pt
                    same_leg = math.floor(pp/self.clock.length) == math.floor(phase/self.clock.length)
                    if not same_leg:
                        self.segment += 1
                    if same_leg and not changed and 0 < dt < .25:
                        retain_anchor = True
                    elif same_leg and not changed and .25 <= dt <= 1.1:
                        self.speed.offer_sparse(abs(x-px)/dt, direction, self.segment,
                                                frame_time, self.last_press)
                # 高频采样保留区间起点，至少积累0.25秒后再估速；不额外截图。
                if self.speed.calibrating and not retain_anchor:
                    self.previous_sample = (frame_time,x,phase)
                observed = True
                self.clock.change_rate(self.speed.value, frame_time)
        if self.target is None or self.clock.anchor_time is None:
            return {"reason": "等待识别鱼漂和黄色目标"}

        if self.locked and now-self.last_press >= max(.35,self.cfg["min_action_interval_s"]):
            outside = observed and not self.target[0]-2 <= obs.marker <= self.target[1]+2
            passed = self.rearm_phase is not None and self.clock.phase_at(now) >= self.rearm_phase
            if outside or passed:
                self.locked = False
        if self.locked:
            return {"reason": "本次经过已按键，等待下一轮或下一次经过"}
        if now-self.last_press < self.cfg["min_action_interval_s"]:
            return {"reason": "按键间隔保护"}
        if observed and self.target[0] <= obs.marker <= self.target[1]:
            return self._decision(now, now, self.clock.direction_at(frame_time), 0, "inside")
        return self.plan(now)

    def plan(self, now):
        if self.target is None or self.track is None or self.clock.anchor_time is None or self.locked:
            return {"reason": "等待黄色目标"}
        left, right = self._target_limits()
        if left > right:
            return {"reason": "黄色目标位于轨道外"}
        # 中部留出两侧时间余量，窄条也不因安全边距大于条宽而被禁用。
        center = (left+right)/2
        effective_now = now+self.latency.seconds
        current = self.clock.position_at(effective_now)
        if left <= current <= right:
            return self._decision(now, now, self.clock.direction_at(effective_now), 0, "inside")
        arrival, direction, turns = self.clock.next_visit(center, effective_now)
        send_at = max(now, arrival-self.latency.seconds)
        remaining = (right-center if direction > 0 else center-left)/self.clock.rate
        return self._decision(send_at, now, direction, turns, "scheduled", send_at+remaining)

    def _decision(self, send_at, now, direction, turns, mode, valid_until=None):
        predicted = self.clock.position_at(send_at+self.latency.seconds)
        wait_ms = max(0,send_at-now)*1000
        return {"action":"HIT", "reason":"鱼漂位于黄条，立即点击" if mode=="inside" else f"已预约 {wait_ms:.0f} 毫秒后点击",
                "send_at":send_at, "valid_until":valid_until, "predicted_x":self._pixel(predicted),
                "velocity":direction*self.speed.value*(self.track[1]-self.track[0]),
                "round_id":self.round_id, "arrival_ms":wait_ms, "arrival_direction":direction,
                "planned_turns":turns, "endpoint_source":"normalized", "click_mode":mode,
                "latency_ms":self.latency.seconds*1000}
