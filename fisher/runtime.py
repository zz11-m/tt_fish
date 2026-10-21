# SPDX-License-Identifier: GPL-3.0-only
"""后台采样、图像识别与输入控制。"""
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import queue
import threading
import time
import cv2
import mss
import numpy as np
from .config import ROOT, capture_interval
from .controller import Controller, Decision
from .vision import Detector, annotate, write_image, LABELS
from .scheduling import CaptureSchedule, Hotkeys

def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

class Runtime:
    def __init__(self, win, hwnd, profile, cfg):
        self.win, self.hwnd, self.profile, self.cfg = win, hwnd, profile, dict(cfg)
        self.commands = queue.Queue()
        self.messages = queue.Queue(maxsize=8)
        self.stop = threading.Event()
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

    def emit(self, kind, data):
        if self.messages.full():
            try: self.messages.get_nowait()
            except queue.Empty: pass
        self.messages.put_nowait((kind, data))

    def command(self, name):
        self.commands.put(name)

    def capture_box(self):
        left, top, w, h = self.win.client_box(self.hwnd)
        if [w, h] != self.profile["client_size"]:
            raise OSError("窗口尺寸已变化，请停止监测并重新框选")
        x, y, cw, ch = self.profile["crop"]
        if min(x, y) < 0 or min(cw, ch) <= 0 or x+cw > w or y+ch > h:
            raise OSError("保存的框选范围无效，请重新框选")
        return {"left": left+x, "top": top+y, "width": cw, "height": ch}

    def pause(self, reason):
        self.active = False
        self.pending_start_at = None
        self.pause_reason = reason
        self.emit("notice", reason)
        self.publish_control()

    def publish_control(self):
        self.emit("control", {"active": self.active, "pending": self.pending_start_at is not None,
                              "dry_run": self.cfg["dry_run"], "reason": self.pause_reason})

    def activate(self, controller, now):
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
                    self.pause(f"已暂停；返回模拟器后按{self.toggle_key}可重新开始")
                else:
                    self.activate(controller, now)
            elif cmd == "arm":
                if self.recorder:
                    self.pause("正在录制手动操作，请等待录制结束后再启动")
                else:
                    self.active = False
                    self.pending_start_at = now+3
                    self.pause_reason = f"3秒后启动，请将所选模拟器置前；{self.pause_key}可取消"
                    self.emit("notice", self.pause_reason)
                    self.publish_control()
            elif cmd == "pause":
                self.pause(f"已暂停并取消待启动操作；{self.toggle_key}可重新开始")
            elif cmd == "snapshot" and self.latest is not None:
                path = ROOT / "snapshots" / f"game_{timestamp()}.png"
                write_image(path, self.latest)
                self.emit("notice", f"游戏截图已保存：{path.name}")
            elif cmd == "record":
                self.begin_recording()
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
        self.pause("录制期间暂停自动操作")
        self.close_recording()
        path = ROOT / "recordings" / f"manual_{timestamp()}.avi"
        path.parent.mkdir(exist_ok=True)
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
        try:
            cv2.setNumThreads(1)
            detector = Detector(ROOT / "assets/templates", self.cfg["template_threshold"])
            controller = Controller(self.cfg)
            controller.reset(time.perf_counter())
            log_dir = ROOT / "logs"
            log_dir.mkdir(exist_ok=True)
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
                    tick = time.perf_counter()
                    for cmd in self.hotkeys.poll(self.win.key_down):
                        self.commands.put(cmd)
                    self.process_controls(controller, tick)
                    if self.recorder and tick >= self.record_until:
                        self.close_recording()
                        self.emit("notice", "录制已保存到recordings；实际时间另存同名CSV")
                    if not self.win.foreground_matches(self.hwnd):
                        if self.active:
                            self.pause(f"模拟器失去前台焦点，已暂停；返回后按{self.toggle_key}重新开始")
                        if self.recorder:
                            self.close_recording()
                            self.emit("notice", "录制因模拟器失焦而结束，已保留已录部分")
                        self.stop.wait(.01)
                        continue
                    if self.stop.is_set():
                        break
                    if not schedule.due(tick):
                        self.stop.wait(min(.01, max(0, schedule.next_at-time.perf_counter())))
                        continue
                    deadline = None
                    capture_started = tick
                    try:
                        box = self.capture_box()
                        screen = sct.monitors[0]
                        if (box["left"] < screen["left"] or box["top"] < screen["top"] or
                            box["left"]+box["width"] > screen["left"]+screen["width"] or
                            box["top"]+box["height"] > screen["top"]+screen["height"]):
                            raise OSError("游戏画面超出可见桌面，请移回屏幕内")
                        t0 = time.perf_counter()
                        capture_started = t0
                        shot = sct.grab(box)
                        frame_time = (t0+time.perf_counter())/2
                        raw = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)[:, :, :3].copy()
                        obs, frame = detector.detect(raw)
                        self.latest = frame
                        now = time.perf_counter()
                        fps = 1/(frame_time-last_frame_time) if last_frame_time else 0
                        last_frame_time = frame_time
                        d = controller.step(obs, frame_time, now) if self.active else Decision(reason=self.pause_reason)
                        sent = False
                        if self.active and d.pause:
                            self.pause(d.reason)
                        if self.active and d.action and not self.cfg["dry_run"]:
                            # 在即将按键前再次核对窗口位置，避免使用移动前的截图。
                            def guard():
                                return self.capture_box() == box
                            press_time = time.perf_counter()
                            sent = self.win.tap_space(self.hwnd, self.cfg["key_hold_ms"],
                                                      self.input_cancelled, guard)
                            if sent:
                                controller.commit(d.action, press_time)
                                if self.win.interrupted:
                                    self.pause("按键期间收到停止信号或模拟器失焦，已释放空格并暂停")
                            else:
                                self.pause("按键前窗口或停止状态发生变化，已暂停")
                        if self.recorder:
                            self.recorder.write(frame)
                            self.record_meta.write(f"{self.record_frame},{frame_time-self.record_started:.6f}\n")
                            self.record_frame += 1
                        if self.active and not self.cfg["dry_run"] and obs.state == "RESULT":
                            deadline = controller.next_result_press_at
                        # 记录动作、状态变化及每秒一次采样；单次会话轮转保留约6 MiB。
                        if obs.state != last_state or sent or now-last_log >= 1:
                            row = {"t": frame_time, "state": obs.state, "variant": obs.variant, "confidence": obs.confidence,
                                   "marker": obs.marker, "target": obs.target, "prediction": d.predicted_x,
                                   "velocity": d.velocity, "uncertainty": d.uncertainty,
                                   "reason": d.reason, "proposed": d.action, "sent": sent,
                                   "active": self.active, "dry_run": self.cfg["dry_run"], "fps": fps}
                            log.info(json.dumps(row, ensure_ascii=False))
                            last_log, last_state = now, obs.state
                        if now-last_ui >= .25:
                            state_label = "开心收下" if obs.variant == "claim" else LABELS[obs.state]
                            info = {"state": state_label, "reason": d.reason, "fps": fps,
                                    "active": self.active, "dry_run": self.cfg["dry_run"],
                                    "pending": self.pending_start_at is not None, "pause_reason": self.pause_reason,
                                    "continuous": self.cfg["continuous_fishing"],
                                    "casts": controller.cast_count, "results": controller.result_count}
                            self.emit("frame", (annotate(frame, obs, d.predicted_x), info))
                            last_ui = now
                    except (OSError, ValueError) as exc:
                        self.pause(str(exc))
                        self.close_recording()
                        self.stop.wait(.25)
                    schedule.advance(capture_started, time.perf_counter(), deadline)
                    self.stop.wait(.001)
        except Exception as exc:
            import traceback
            if log:
                log.info(json.dumps({"fatal": traceback.format_exc()}, ensure_ascii=False))
            self.emit("notice", f"监测已停止：{exc}")
        finally:
            self.active = False
            self.pending_start_at = None
            self.close_recording()
            try: self.win.release()
            except OSError as exc: self.emit("notice", str(exc))
            if handler:
                handler.close()
            self.emit("finished", None)

    def input_cancelled(self):
        return self.stop.is_set() or self.win.key_down(self.hotkeys.pause_vk)
