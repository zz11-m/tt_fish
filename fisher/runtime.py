# SPDX-License-Identifier: GPL-3.0-only
"""后台采样、图像识别与输入控制。"""
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
import time
import cv2
import mss
import numpy as np
from .config import ROOT, DATA_ROOT, capture_interval
from .controller import Controller, Decision
from .vision import Detector, annotate, write_image, LABELS
from .scheduling import CaptureSchedule, Hotkeys
from . import TRIAL_BUILD
from .trial_capture import TrialRecorder
from .jsonio import dumps
from .contact import MAX_CONTACT_WAIT, MAX_TRACK_AGE
from .speed import load_profile, save_profile, reference_profile

def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

class Runtime:
    def __init__(self, win, hwnd, profile, cfg):
        self.win, self.hwnd, self.profile, self.cfg = win, hwnd, profile, dict(cfg)
        self.commands = queue.Queue()
        self.messages = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.cancel_input = threading.Event()
        self.pending_hit = None
        self.capture_budget = .060
        self.active = False
        self.pending_start_at = None
        self.hotkeys = Hotkeys(cfg)
        self.toggle_key = cfg["toggle_hotkey"]
        self.pause_key = cfg["pause_hotkey"]
        self.pause_reason = f"仅监测：切回模拟器按{self.toggle_key}，或点击启动钓鱼"
        self.thread = threading.Thread(target=self.run, daemon=False)
        self.recorder = self.record_meta = None
        self.record_until = 0
        self.latest = None
        self.preview_enabled = threading.Event()
        if cfg.get("show_preview", False):
            self.preview_enabled.set()
        self.trial = None
        self.diagnostic_status = ""
        self.feedback_samples = 0
        self.feedback_until = 0.0
        self.external_space_down = False
        self.controller = None
        self.speed_status = "读取速度参数…"
        self.calibration_complete = False
        self.next_speed_save = 0.0

    def emit(self, kind, data):
        if kind == "diagnostic_status":
            self.diagnostic_status = data
        try:
            self.messages.put_nowait((kind, data))
        except queue.Full:
            try: self.messages.get_nowait()
            except queue.Empty: pass
            try: self.messages.put_nowait((kind, data))
            except queue.Full: pass

    def command(self, name):
        if name in ("pause", "record") or (name == "toggle" and self.active):
            self.cancel_input.set()
        self.commands.put(name)

    def capture_box(self):
        left, top, w, h = self.win.client_box(self.hwnd)
        if [w, h] != self.profile["client_size"]:
            raise OSError("窗口尺寸已变化，请停止监测并重新框选")
        x, y, cw, ch = self.profile["crop"]
        if min(x, y) < 0 or min(cw, ch) <= 0 or x+cw > w or y+ch > h:
            raise OSError("保存的框选范围无效，请重新框选")
        return {"left": left+x, "top": top+y, "width": cw, "height": ch}

    def pause(self, reason, code="manual_pause", cause="pause"):
        self.active = False
        self.pending_hit = None
        self.cancel_input.set()
        self.feedback_samples = 0
        self.pending_start_at = None
        self.win.release()
        self.persist_calibration()
        self.finish_trial(cause, code)
        self.pause_reason = reason
        self.emit("notice", reason)
        self.publish_control()

    def finish_trial(self, cause, code="unspecified"):
        if self.trial:
            try:
                self.trial.finish(cause, code)
            except (OSError, ValueError, TypeError):
                self.emit("diagnostic_status", "测试记录保存失败，请检查磁盘空间和目录权限。")

    def record_fault(self, exc, component):
        if self.trial:
            try:
                self.trial.fault(exc, component)
            except (OSError, ValueError, TypeError):
                self.emit("diagnostic_status", "异常记录未能写入，请保留已有测试文件。")

    def publish_control(self):
        self.emit("control", {"active": self.active, "pending": self.pending_start_at is not None,
                              "dry_run": self.cfg["dry_run"], "reason": self.pause_reason})

    def activate(self, controller, now):
        self.controller=controller
        self.pending_start_at = None
        if self.stop.is_set():
            return
        if self.recorder:
            self.pause("正在录制手动操作，请等待录制结束后再启动")
        elif not self.win.foreground_matches(self.hwnd):
            self.pause(f"未启动：所选模拟器不在前台。切回模拟器后按{self.toggle_key}")
        else:
            # 激活前验证窗口尺寸；实际发送空格前仍会再次检查焦点及位置。
            try:
                self.capture_box()
            except (OSError, ValueError) as exc:
                self.pause(str(exc))
                return
            controller.reset(now)
            self.pending_hit = None
            self.cancel_input.clear()
            self.feedback_samples = 0
            self.external_space_down = False
            if self.trial:
                try:
                    self.trial.start(self.cfg)
                except OSError:
                    self.pause("无法创建测试记录，请检查数据目录权限和磁盘空间")
                    return
            self.active = True
            self.pause_reason = "观察运行中，不发送空格" if self.cfg["dry_run"] else "自动钓鱼运行中"
            self.emit("notice", self.pause_reason)
            self.publish_control()

    def process_controls(self, controller, now):
        """控制命令独立于截图间隔处理。"""
        if self.stop.is_set():
            self.active = False
            self.pending_start_at = None
            return
        while not self.commands.empty():
            cmd = self.commands.get_nowait()
            if cmd == "toggle":
                if self.active:
                    self.pause(f"已暂停；返回模拟器后按{self.toggle_key}可重新开始", "toggle_pause")
                else:
                    self.activate(controller, now)
            elif cmd == "arm":
                if self.recorder:
                    self.pause("正在录制手动操作，请等待录制结束后再启动")
                else:
                    if self.active:
                        self.pause("重新等待启动", "arming")
                    self.active = False
                    self.pending_start_at = now+3
                    self.pause_reason = f"3秒后启动，请将所选模拟器置前；{self.pause_key}可取消"
                    self.emit("notice", self.pause_reason)
                    self.publish_control()
            elif cmd == "pause":
                self.pause(f"已暂停并取消待启动操作；{self.toggle_key}可重新开始")
            elif cmd == "snapshot" and self.latest is not None:
                path = DATA_ROOT / "snapshots" / f"game_{timestamp()}.png"
                write_image(path, self.latest)
                self.emit("notice", f"游戏截图已保存：{path.name}")
            elif cmd == "record":
                self.begin_recording()
            elif cmd == "show_preview":
                self.preview_enabled.set()
            elif cmd == "hide_preview":
                self.preview_enabled.clear()
        if self.pending_start_at is not None and now >= self.pending_start_at:
            self.activate(controller, now)

    def close_recording(self):
        if self.recorder:
            self.recorder.release()
            self.recorder = None
        if self.record_meta:
            self.record_meta.close()
            self.record_meta = None

    def begin_recording(self):
        self.pause("录制期间暂停自动操作", "recording")
        self.close_recording()
        path = DATA_ROOT / "recordings" / f"manual_{timestamp()}.avi"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.recorder = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 1/capture_interval(self.cfg), (336, 598))
        if not self.recorder.isOpened():
            self.close_recording()
            raise OSError("无法创建录像，请使用模拟器录屏并保留原视频")
        self.record_meta = path.with_suffix(".csv").open("w", encoding="utf-8")
        self.record_meta.write("frame,elapsed_seconds\n")
        self.record_started = time.perf_counter()
        self.record_until = self.record_started+15
        self.record_frame = 0
        self.emit("notice", "录制15秒：自动按键已暂停，请在模拟器中手动完成钓鱼")

    def run(self):
        log = handler = None
        component, exit_cause = "startup", "stop"
        try:
            cv2.setNumThreads(1)
            if TRIAL_BUILD:
                self.trial = TrialRecorder(DATA_ROOT, self.emit)
            detector = Detector(ROOT / "assets/templates", self.cfg["template_threshold"])
            try:
                profile=load_profile(DATA_ROOT/"speed_profile.json",self.cfg["speed_fraction_per_s"])
            except (OSError,ValueError,TypeError):
                profile=reference_profile()
                self.emit("notice","速度参数无法读取，使用参考速度；可重新校准。")
            self.cfg["speed_fraction_per_s"]=profile["fraction_per_second"]
            controller = Controller(self.cfg)
            self.controller=controller
            controller.reset(time.perf_counter())
            if TRIAL_BUILD:
                log_dir = DATA_ROOT / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(log_dir / f"session_{timestamp()}.jsonl",
                                              maxBytes=2*1024*1024, backupCount=2, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(message)s"))
                log = logging.Logger("fishing_session", level=logging.INFO)
                log.addHandler(handler)
            schedule = CaptureSchedule(self.cfg)
            last_ui = last_log = 0.0
            last_state = None
            last_frame_time = None
            fps = 0.0
            notice = self.pause_reason
            self.emit("notice", notice)
            self.publish_control()
            with mss.mss() as sct:
                while not self.stop.is_set():
                    component = "controls"
                    tick = time.perf_counter()
                    for cmd in self.hotkeys.poll(self.win.key_down):
                        self.commands.put(cmd)
                    self.process_controls(controller, tick)
                    if self.recorder and tick >= self.record_until:
                        self.close_recording()
                        self.emit("notice", "录制已保存到recordings；实际时间另存同名CSV")
                    if not self.win.foreground_matches(self.hwnd):
                        if self.active:
                            self.pause(f"模拟器失去前台焦点，已暂停；返回后按{self.toggle_key}重新开始", "focus_lost")
                        if self.recorder:
                            self.close_recording()
                            self.emit("notice", "录制因模拟器失焦而结束，已保留已录部分")
                        self.stop.wait(.01)
                        continue
                    if self.stop.is_set():
                        break
                    self.poll_trial_space(tick)
                    self.process_pending_hit(controller)
                    tick = time.perf_counter()
                    # 期限已接近时先发送按键，避免截图与识别占用狭窄的命中窗口。
                    if self.pending_hit is not None:
                        due = self.pending_hit[0].send_at
                        remaining = due-tick
                        if 0 < remaining <= self.capture_budget:
                            self.stop.wait(min(.010,remaining))
                            continue
                    if not schedule.due(tick):
                        delay = max(0,schedule.next_at-time.perf_counter())
                        if self.pending_hit is not None:
                            delay = min(delay,max(0,self.pending_hit[0].send_at-time.perf_counter()))
                        self.stop.wait(min(.01,delay))
                        continue
                    deadline = None
                    check_at = None
                    capture_started = tick
                    try:
                        component = "capture"
                        box = self.capture_box()
                        screen = sct.monitors[0]
                        if (box["left"] < screen["left"] or box["top"] < screen["top"] or
                            box["left"]+box["width"] > screen["left"]+screen["width"] or
                            box["top"]+box["height"] > screen["top"]+screen["height"]):
                            raise OSError("游戏画面超出可见桌面，请移回屏幕内")
                        t0 = time.perf_counter()
                        capture_started = t0
                        shot = sct.grab(box)
                        capture_end = time.perf_counter()
                        frame_time = (t0+capture_end)/2
                        raw = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)[:, :, :3].copy()
                        component = "recognition"
                        obs, frame = detector.detect(raw)
                        self.latest = frame
                        now = time.perf_counter()
                        fps = 1/(frame_time-last_frame_time) if last_frame_time else 0
                        last_frame_time = frame_time
                        controller.contact.observe_processing((now-frame_time)*1000)
                        component = "decision"
                        d = controller.step(obs, frame_time, now) if self.active else Decision(reason=self.pause_reason)
                        decided_at = time.perf_counter()
                        self.capture_budget = min(.200,max(.030,(decided_at-t0)*1.5+.010))
                        self.pending_hit = None     # 新画面替换旧计划，包括未知/其他阶段。
                        if self.active and self.trial:
                            component = "trace_frame"
                            self.trial.frame(obs, d, controller, frame, frame_time, decided_at,
                                             (capture_end-t0)*1000, (decided_at-capture_end)*1000)
                        check_at = d.check_at
                        sent = False
                        if self.active and d.pause:
                            self.pause(d.reason, d.pause_code or "controller_pause")
                        if self.active and d.action and not self.cfg["dry_run"]:
                            component = "dispatch"
                            if d.action == "HIT" and self.cfg.get("timing_mode") == "phase":
                                self.pending_hit = (d,dict(box),frame_time)
                                sent = self.process_pending_hit(controller)
                            else:
                                sent = self.dispatch(d, controller, box, frame_time, decided_at)
                        self.persist_calibration()
                        speed=controller.speed
                        self.calibration_complete=speed.completed
                        self.speed_status=(f"校准中：{len(speed.samples)} 个有效片段" if speed.calibrating else
                                           f"固定速度：{speed.value*100:.2f}%条宽/秒"+
                                           (" · 校准已保存" if speed.saved else " · 校准未保存" if speed.completed else ""))
                        if self.recorder:
                            component = "manual_recording"
                            self.recorder.write(frame)
                            self.record_meta.write(f"{self.record_frame},{frame_time-self.record_started:.6f}\n")
                            self.record_frame += 1
                        if self.active and not self.cfg["dry_run"] and obs.state == "RESULT":
                            deadline = controller.next_result_press_at
                        # 记录动作、状态变化及每秒一次采样；单次会话轮转保留约6 MiB。
                        if log and (obs.state != last_state or sent or now-last_log >= 1):
                            component = "log"
                            row = {"t": frame_time, "state": obs.state, "variant": obs.variant, "confidence": obs.confidence,
                                   "marker": obs.marker, "target": obs.target, "prediction": d.predicted_x,
                                   "velocity": d.velocity, "uncertainty": d.uncertainty,
                                   "reason": d.reason, "proposed": d.action, "sent": sent,
                                   "active": self.active, "dry_run": self.cfg["dry_run"], "fps": fps}
                            log.info(dumps(row))
                            last_log, last_state = now, obs.state
                        if now-last_ui >= .25:
                            component = "preview"
                            state_label = "开心收下" if obs.variant == "claim" else LABELS[obs.state]
                            info = {"state": state_label, "reason": d.reason, "fps": fps,
                                    "active": self.active, "dry_run": self.cfg["dry_run"],
                                    "pending": self.pending_start_at is not None, "pause_reason": self.pause_reason,
                                    "continuous": self.cfg["continuous_fishing"],
                                    "casts": controller.cast_count, "results": controller.result_count}
                            preview = annotate(frame, obs, d.predicted_x) if self.preview_enabled.is_set() else None
                            self.emit("frame", (preview, info))
                            last_ui = now
                    except (OSError, ValueError, TypeError) as exc:
                        self.record_fault(exc, component)
                        self.pause(str(exc), "runtime_error", "error")
                        self.close_recording()
                        self.stop.wait(.25)
                    schedule.advance(capture_started, time.perf_counter(), deadline)
                    if self.active and self.cfg.get("timing_mode") != "phase":
                        schedule.request_earlier(check_at, capture_started, time.perf_counter())
                        self.schedule_feedback(schedule, capture_started, time.perf_counter())
                        if controller.speed.calibrating and obs.state=="TIMING":
                            schedule.request_earlier(capture_started+1/15,capture_started,time.perf_counter())
                    self.stop.wait(.001)
        except Exception as exc:
            import traceback
            exit_cause = "error"
            self.record_fault(exc, component)
            if log:
                log.info(dumps({"fatal": traceback.format_exc()}))
            self.emit("notice", f"监测已停止：{exc}")
        finally:
            self.active = False
            self.pending_hit = None
            self.pending_start_at = None
            self.close_recording()
            try: self.win.release()
            except OSError as exc: self.emit("notice", str(exc))
            self.finish_trial(exit_cause, "runtime_error" if exit_cause == "error" else "stop")
            self.persist_calibration()
            if self.trial:
                self.trial.join()
            if handler:
                handler.close()
            self.emit("finished", None)

    def process_pending_hit(self, controller):
        """由控制循环调用，执行期限不依赖下一次截图到达。"""
        if self.pending_hit is None:
            return False
        if not self.active or self.cfg["dry_run"] or self.input_cancelled() or controller.state != "TIMING":
            self.pending_hit = None
            return False
        decision,box,frame_time = self.pending_hit
        now = time.perf_counter()
        if now < decision.send_at:
            return False
        if decision.valid_until is not None and now > decision.valid_until:
            # 线程唤醒错过整段目标时，立即计算下一次经过；不等下一张图解锁。
            decision = Decision(**controller.phase.plan(now))
            if decision.action is None:
                self.pending_hit = None
                return False
            self.pending_hit = (decision,box,frame_time)
            if decision.send_at > now:
                return False
        self.pending_hit = None
        return self.dispatch(decision,controller,box,frame_time,now)

    def dispatch(self, decision, controller, box, frame_time, decided_at):
        """到期输入保留焦点、窗口与暂停保护；相位计划不受截图年龄限制。"""
        requested_at = max(decided_at, decision.send_at or decided_at)
        contact = decision.action == "HIT" and self.cfg.get("timing_mode") == "contact"
        phase = decision.action == "HIT" and self.cfg.get("timing_mode") == "phase"
        max_frame_age = float("inf") if phase else MAX_TRACK_AGE if contact else .080 if decision.action == "HIT" else .250
        skipped = None
        if contact and requested_at-decided_at > MAX_CONTACT_WAIT+.001:
            skipped = "distant_deadline"
        while skipped is None and time.perf_counter() < requested_at:
            if self.input_cancelled() or not self.win.foreground_matches(self.hwnd):
                skipped = "cancelled"
                break
            self.stop.wait(min(.002, max(0, requested_at-time.perf_counter())))
        now = time.perf_counter()
        if skipped is None:
            if now-frame_time > max_frame_age:
                skipped = "stale_frame"
            elif (contact or phase) and decision.valid_until is not None and now > decision.valid_until:
                skipped = "late_dispatch"
            elif contact and decision.valid_until is None and now-requested_at > .008:
                skipped = "late_dispatch"
        sent = False
        if skipped is None:
            def guard():
                nonlocal skipped
                geometry_matches = self.capture_box() == box
                at = time.perf_counter()
                if at-frame_time > max_frame_age:
                    skipped = "stale_frame"
                elif (contact or phase) and decision.valid_until is not None and at > decision.valid_until:
                    skipped = "late_dispatch"
                elif contact and decision.valid_until is None and at-requested_at > .012:
                    skipped = "late_dispatch"
                return skipped is None and geometry_matches
            sent = self.win.tap_space(self.hwnd, self.cfg["key_hold_ms"], self.input_cancelled, guard)
        if self.trial:
            self.trial.input(decision.action, requested_at, self.win, sent, skipped)
        if sent:
            actual = getattr(self.win, "last_key_down_at", None)
            begin=getattr(self.win,"last_send_begin",None)
            end=getattr(self.win,"last_send_end",None)
            blocked=begin is not None and end is not None and end-begin>.050
            if blocked:
                # 长调用中点不是可靠按下时刻；以返回时刻开始冷却，排除延迟学习。
                controller.latency.rejected+=1
            elif actual is not None:
                controller.latency.observe(requested_at, actual)
            controller.commit(decision.action,end if blocked else actual if actual is not None else now)
            if blocked:
                controller.contact.motion.clear("input_stall")
            if decision.action == "HIT" and self.trial:
                self.feedback_samples = 8
                self.feedback_until = time.perf_counter()+.65
            if self.win.interrupted:
                self.pause("按键期间收到停止信号或模拟器失焦，已释放空格并暂停", "input_cancelled")
        elif skipped in ("stale_frame", "late_dispatch", "distant_deadline"):
            if phase and self.active:
                new = Decision(**controller.phase.plan(time.perf_counter()))
                if new.action:
                    self.pending_hit = (new,box,frame_time)
            else:
                controller.contact.motion.clear("dispatch_late")
        else:
            self.pause("按键前窗口或停止状态发生变化，已暂停", "input_cancelled")
        return sent

    def persist_calibration(self):
        if self.controller is None:
            return
        speed=self.controller.speed
        if not speed.completed or speed.saved or time.perf_counter()<self.next_speed_save:
            return
        try:
            save_profile(DATA_ROOT/"speed_profile.json",speed.profile())
            speed.saved=True
        except OSError:
            self.next_speed_save=time.perf_counter()+5
            self.emit("notice","速度校准已完成，但参数保存失败；请检查目录权限。")

    def schedule_feedback(self, schedule, started, finished):
        # 只在测试段的真实判定输入后短暂加密，仍不补抓积压帧。
        if self.feedback_samples > 0 and finished <= self.feedback_until:
            schedule.request_earlier(started+.050, started, finished)
            self.feedback_samples -= 1
        elif finished > self.feedback_until:
            self.feedback_samples = 0

    def poll_trial_space(self, now):
        if not (self.active and self.trial and self.trial.current is not None):
            return
        released = getattr(self.win, "last_key_up_at", None)
        if getattr(self.win, "owns_space", False) or (released is not None and now-released < .080):
            return
        down = bool(self.win.key_down(0x20))
        if down != self.external_space_down:
            self.trial.external_space(now, down)
            self.external_space_down = down

    def input_cancelled(self):
        return self.stop.is_set() or self.cancel_input.is_set() or self.win.key_down(self.hotkeys.pause_vk)
