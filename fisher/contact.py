# SPDX-License-Identifier: GPL-3.0-only
"""入条预测、比例速度与有界的本机输入耗时学习。"""
from collections import deque
import math
from statistics import median
from .speed import SpeedCalibration, DEFAULT_SPEED

MAX_CONTACT_WAIT = .035
MAX_TRACK_AGE = .120


def next_entry(x, velocity, left_entry, right_entry, left_end, right_end):
    """返回到下一入条点的秒数、入条方向与折返次数；当前已越过时走对侧入口。"""
    speed = abs(velocity)
    if speed <= 0 or not all(math.isfinite(v) for v in
                            (x, velocity, left_entry, right_entry, left_end, right_end)):
        return None
    if not left_end <= x <= right_end or not left_end <= left_entry <= right_entry <= right_end:
        return None
    if velocity > 0:
        if x <= left_entry:
            return (left_entry-x)/speed, 1, 0, left_entry
        return (right_end-x+right_end-right_entry)/speed, -1, 1, right_entry
    if x >= right_entry:
        return (x-right_entry)/speed, -1, 0, right_entry
    return (x-left_end+left_entry-left_end)/speed, 1, 1, left_entry


class DeliveryLatency:
    def __init__(self, base_ms, learn=True):
        self.base_ms = float(base_ms)
        self.learn = learn
        self.host_ms = 0.0
        self.samples = deque(maxlen=31)
        self.rejected = 0

    @property
    def total_ms(self):
        return min(300.0, self.base_ms + self.host_ms)

    @property
    def seconds(self):
        return self.total_ms/1000

    def observe(self, requested_at, key_down_at):
        delay = (key_down_at-requested_at)*1000
        if not math.isfinite(delay) or not 0 <= delay <= 50:
            self.rejected += 1
            return
        if not self.learn:
            return
        if len(self.samples) >= 5:
            middle = median(self.samples)
            mad = median(abs(x-middle) for x in self.samples)
            if abs(delay-middle) > max(8, 6*mad):
                self.rejected += 1
                return
        self.samples.append(delay)
        # 每次最多调整2ms，总补偿不超过30ms；不把卡顿、按住时长当成输入延迟。
        goal = min(30.0, median(self.samples))
        self.host_ms += max(-2.0, min(2.0, (goal-self.host_ms)*.25))

    def snapshot(self):
        return {"base_ms": self.base_ms, "host_ms": self.host_ms, "total_ms": self.total_ms,
                "samples": len(self.samples), "rejected": self.rejected}


class SegmentMotion:
    def __init__(self, interval):
        self.interval = interval
        self.points = deque(maxlen=6)
        self.velocity = 0.0
        self.event = "warming"
        self.fit_error = 0.0
        self.segment = 0
        self.turns = {"left": deque(maxlen=8), "right": deque(maxlen=8)}

    def clear(self, event="lost"):
        self.points.clear()
        self.velocity = 0.0
        self.fit_error = 0.0
        self.event = event
        self.segment += 1

    def add(self, t, x):
        self.event = "tracking"
        if self.points:
            old_t, old_x = self.points[-1]
            dt, dx = t-old_t, x-old_x
            if dt <= 0:
                return
            v = self.velocity
            if dt > max(.18, self.interval*1.8):
                self.clear("gap")
            elif abs(dx) > max(35, abs(v)*dt*2.0+8):
                self.clear("jump")
            elif abs(v) > 10 and abs(dx) < .6 and abs(v)*dt > 1.2:
                self.clear("freeze")
            elif abs(v) > 10 and dx*v < -1:
                edge = (old_x+x+v*dt)/2
                if 75 <= edge <= 266:
                    self.turns["right" if v > 0 else "left"].append(edge)
                self.clear("turn")
            elif abs(v) > 10 and abs(dx-v*dt) > max(3.0, abs(v*dt)*.45, abs(v)*.033):
                self.clear("speed_change")
        self.points.append((t, x))

    def estimate(self):
        if len(self.points) < 3:
            return None
        points = list(self.points)
        dt = points[-1][0]-points[0][0]
        if dt < .05:
            return None
        # 多个跨度的斜率中位数减轻显示帧量化影响；不把相邻截图当成连续渲染。
        slopes = [(b[1]-a[1])/(b[0]-a[0]) for i,a in enumerate(points) for b in points[i+1:]]
        v = median(slopes)
        if not 8 <= abs(v) <= 1400 or any(s*v <= 0 for s in slopes):
            return None
        intercept = median(x-v*(t-points[-1][0]) for t,x in points)
        residuals = [x-(intercept+v*(t-points[-1][0])) for t,x in points]
        error = math.sqrt(sum(e*e for e in residuals)/len(residuals))
        spread = median(abs(s-v) for s in slopes)
        self.fit_error = error
        if error > max(2.2,abs(v)*.008) or spread > max(12, abs(v)*.20):
            return None
        self.velocity = v
        return points[-1][1], v, error

    def endpoints(self):
        result = {}
        for name, values in self.turns.items():
            if len(values) >= 3 and max(values)-min(values) <= 4:
                result[name] = median(values)
        return result


class RatioMotion(SegmentMotion):
    """固定幅值只做方向、位移一致性检查；校准时才运行完整斜率估计。"""
    def __init__(self, interval, speed):
        super().__init__(interval)
        self.points=deque(maxlen=8)
        self.speed=speed
        self.track_width=None
        self.observed_velocity=None
        self.estimate_source="fixed"

    def clear(self,event="lost"):
        super().clear(event)
        self.observed_velocity=None

    def set_width(self, width):
        if self.track_width is not None and abs(width-self.track_width)>max(3,self.track_width*.02):
            self.clear("track_changed")
        self.track_width=width

    def estimate(self):
        self.observed_velocity=None
        self.estimate_source="calibration" if self.speed.calibrating else "fixed"
        if self.speed.calibrating:
            result=super().estimate()
            self.observed_velocity=None if result is None else result[1]
            return result
        if len(self.points)<3 or self.track_width is None:
            return None
        # 固定幅值只核对最近约0.35秒；避免旧显示帧的相位误差长期阻塞下一次经过。
        points=[p for p in self.points if self.points[-1][0]-p[0] <= .35]
        if len(points)<3:
            return None
        elapsed=points[-1][0]-points[0][0]
        if elapsed<.10:
            return None
        distance=points[-1][1]-points[0][1]
        if abs(distance)<1:
            return None
        direction=1 if distance>0 else -1
        if any((b[1]-a[1])*direction<=0 for a,b in zip(points,points[1:])):
            return None
        expected=self.speed.value*self.track_width
        self.observed_velocity=distance/elapsed
        if abs(abs(distance/elapsed)-expected)>max(expected*.14,2.0/elapsed):
            self.event="speed_mismatch"
            return None
        v=direction*expected
        anchor=median(x-v*(t-points[-1][0]) for t,x in points)
        error=math.sqrt(sum((x-(anchor+v*(t-points[-1][0])))**2 for t,x in points)/len(points))
        error=max(error,abs(anchor-points[-1][1]))
        self.fit_error=error
        if error>max(2.2,self.track_width*.012):
            self.event="speed_mismatch"
            return None
        self.velocity=v
        return points[-1][1],v,error


class ContactModel:
    def __init__(self, cfg, latency, speed=None):
        self.cfg, self.latency = cfg, latency
        self.speed=speed if speed is not None else SpeedCalibration(cfg.get("speed_fraction_per_s",DEFAULT_SPEED),cfg.get("calibrate_speed",False))
        self.motion = RatioMotion(cfg["capture_every_n_frames"]/60,self.speed)
        self.target = self.candidate = None
        self.target_count = 0
        self.locked = False
        self.shot_target = None
        self.round_id = 1
        self.last_press = -math.inf
        self.next_process_ms = 6.0
        self.warmup_segment = -1
        self.warmup_checks = 0
        self.retry_count = 0
        self.confirmed_at = -math.inf
        self.calibration_segment=object()

    def commit(self, now):
        self.locked = True
        self.shot_target = self.target
        self.last_press = now
        self.candidate, self.target_count = None, 0

    def observe_processing(self, milliseconds):
        if 0 <= milliseconds <= 60:
            self.next_process_ms = .85*self.next_process_ms + .15*milliseconds

    def step(self, obs, frame_time, now):
        data = dict(reason="确认接触轨迹", round_id=self.round_id,
                    frame_age_ms=max(0, now-frame_time)*1000, latency_ms=self.latency.total_ms,
                    retry_count=self.retry_count)
        # 过期截图不能改变边界、解除按键锁或延续速度模型。
        if data["frame_age_ms"] > MAX_TRACK_AGE*1000:
            self.motion.clear("stale")
            self.candidate, self.target_count = None, 0
            return dict(data, reason="截图已过时，取消预测", motion_event="stale")
        if obs.marker is None:
            self.motion.clear("missing_marker")
            self.candidate, self.target_count = None, 0
            return dict(data, reason="浮标未确认，取消预测", motion_event=self.motion.event)
        if obs.track_left is None or obs.track_right is None or obs.track_right<=obs.track_left:
            self.motion.clear("missing_track")
            self.candidate,self.target_count=None,0
            return dict(data,reason="轨道宽度未确认，等待完整轨道",motion_event="missing_track")
        self.motion.set_width(obs.track_right-obs.track_left)
        self.motion.add(frame_time, obs.marker)
        data["motion_event"] = self.motion.event
        if obs.geometry_clear and obs.target is not None:
            if self.candidate and max(abs(a-b) for a,b in zip(self.candidate, obs.target)) <= 1.5:
                self.target_count += 1
            else:
                self.candidate, self.target_count = obs.target, 1
            if self.target_count >= 2:
                old_target = self.target
                self.target = obs.target
                self.confirmed_at = frame_time
                if old_target and (max(abs(a-b) for a,b in zip(old_target, self.target)) >= 3 or
                        (old_target[1]-old_target[0])-(self.target[1]-self.target[0]) >= 2):
                    self.round_id += 1
                    self.retry_count = 0
                    data.update(round_id=self.round_id, retry_count=0, feedback="target_changed")
        else:
            self.candidate, self.target_count = None, 0
        estimate = self.motion.estimate()
        data["motion_event"] = self.motion.event
        if estimate is not None:
            data.update(velocity=estimate[1], fit_error=estimate[2])
            self.speed.offer(self.motion.points,estimate[1],estimate[2],self.motion.track_width,
                             (self.calibration_segment,self.motion.segment),now,self.last_press)
        if self.target is None:
            return dict(data, reason="等待确认完整黄条边界")
        if self.locked:
            changed = self.shot_target and (
                max(abs(a-b) for a,b in zip(self.shot_target, self.target)) >= 3 or
                (self.shot_target[1]-self.shot_target[0])-(self.target[1]-self.target[0]) >= 2)
            if changed and now-self.last_press >= self.cfg["min_action_interval_s"]:
                self.locked = False
                data["round_id"] = self.round_id
                data["motion_event"] = "round_continues"
            elif (now-self.last_press >= .8 and self.retry_count < 2 and
                  self.confirmed_at > self.last_press and obs.geometry_clear and
                  self.target_count >= 2 and estimate is not None and
                  (obs.marker_right < self.target[0] or obs.marker_left > self.target[1])):
                # 相同边界仍在、鱼漂已离开且继续运动：仅解锁下一次经过，不立即补按。
                self.locked = False
                self.retry_count += 1
                data.update(retry_count=self.retry_count, feedback="retry_armed")
            else:
                reason = "重试次数已达上限，等待阶段结束" if self.retry_count >= 2 else "已触发，观察黄条与鱼漂变化"
                return dict(data, reason=reason)
        if estimate is None:
            if self.warmup_segment != self.motion.segment:
                self.warmup_segment = self.motion.segment
                self.warmup_checks = 4
            check_at = None
            if self.warmup_checks:
                self.warmup_checks -= 1
                check_at = now+.050
            reason="轨迹与固定速度不符，请校准速度" if self.motion.event=="speed_mismatch" else "重新确认运动方向与速度"
            return dict(data, reason=reason, fit_error=self.motion.fit_error, check_at=check_at)
        x, v, error = estimate
        data.update(velocity=v, fit_error=error)
        if obs.marker_left is None or obs.marker_right is None:
            return dict(data, reason="浮标宽度未确认")
        if now-self.last_press < self.cfg["min_action_interval_s"]:
            return dict(data, reason="按键间隔保护")
        left, right = self.target
        # 可见前缘重叠不等于游戏命中：默认重叠约60%鱼漂宽度，中心刚入条。
        overlap=(obs.marker_right-obs.marker_left)*self.cfg.get("contact_overlap_ratio",.60)
        left_entry = left-(obs.marker_right-obs.marker)+overlap
        right_entry = right+(obs.marker-obs.marker_left)-overlap
        # 极窄条不把修正点推到远侧之外；旧外缘对照值仍可由重叠比例指定。
        left_entry=min(left_entry,(left+right)/2)
        right_entry=max(right_entry,(left+right)/2)
        entry=left_entry if v>0 else right_entry
        delay = (self.latency.total_ms + data["frame_age_ms"])/1000
        predicted = x+v*delay
        uncertainty=error+abs(v)*(.003+data["frame_age_ms"]/10000)
        ends=self.motion.endpoints()
        # 外框推导值仅安排复核；真正按键始终要求最新单向轨迹，不跨折返盲发。
        lo=ends.get("left",obs.track_left+max(4,x-obs.marker_left))
        hi=ends.get("right",obs.track_right-max(4,obs.marker_right-x))
        source="observed" if len(ends)==2 else "geometry"
        plan=next_entry(x,v,left_entry,right_entry,lo,hi)
        data.update(predicted_x=predicted, contact_x=entry, uncertainty=uncertainty,
                    endpoint_source=source)
        if plan is not None:
            eta,direction,turns,destination=plan
            data.update(arrival_ms=max(0,eta-(now-frame_time))*1000,
                        arrival_direction=direction,planned_turns=turns)
        if uncertainty > max(4.0,min(6.0,(right-left)/3)):
            return dict(data, reason="运动误差偏大，等待稳定")
        tau = (entry-predicted)/v
        age=data["frame_age_ms"]/1000
        wait_limit = min(MAX_CONTACT_WAIT, max(0, MAX_TRACK_AGE-.002-age))
        # 计算到远侧安全边缘的期限；调度稍晚但仍在条内时也可执行，过期则取消。
        safe_left,safe_right=left+max(1,uncertainty),right-max(1,uncertainty)
        safe_exit=safe_right if v>0 else safe_left
        remaining=(safe_exit-predicted)/v
        valid_until=now+min(MAX_TRACK_AGE-.002-age,max(0,remaining))
        turn_eta=((hi-x)/v if v>0 else (lo-x)/v)
        before_turn=turn_eta >= delay
        if before_turn and -min(.012,1/abs(v)) <= tau <= wait_limit:
            return dict(data, action="HIT", reason="预测鱼漂达到入条位置",
                        send_at=now+max(0, tau), click_mode="entry",
                        planned_turns=0,arrival_direction=1 if v>0 else -1,
                        arrival_ms=max(0,(entry-x)/v-age)*1000,
                        valid_until=max(now+max(0,tau),valid_until))
        if before_turn and tau<0 and safe_left<=predicted<=safe_right and valid_until>now:
            return dict(data,action="HIT",reason="错过入条瞬间，鱼漂仍在黄条内",
                        send_at=now,valid_until=valid_until,click_mode="inside",
                        planned_turns=0,arrival_direction=1 if v>0 else -1,arrival_ms=0.)
        if plan is not None and plan[2]:
            # 长时间计划只决定何时复核；端点估计误差、卡顿和新黄条均会被新帧修正。
            processing=max(self.next_process_ms,data["frame_age_ms"])/1000
            check_at=now+max(.025,plan[0]-delay-processing-.060)
            return dict(data,reason=f"等待折返，从{'左' if plan[1]>0 else '右'}侧入条",
                        check_at=check_at)
        if tau < 0:
            return dict(data,reason="已离开可命中范围，等待下一次经过")
        if tau < .35:
            # 本帧已变慢时立即扩大复核提前量，不能等待平滑均值追上实际耗时。
            processing = max(self.next_process_ms, data["frame_age_ms"])/1000
            check_at = now+max(0, tau-processing-.025)
            return dict(data, reason="接近黄条，安排短时复核", check_at=check_at)
        return dict(data, reason="等待接近黄条")
