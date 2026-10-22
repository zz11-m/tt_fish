# SPDX-License-Identifier: GPL-3.0-only
"""测试构建专用：数值轨迹、判定条局部图与暂停时的诊断包。"""
from collections import deque
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid
import zipfile
import cv2
import numpy as np
from . import __version__
from .packaging import source_payloads
from .config import ROOT
from .jsonio import dumps

CONFIG_KEYS = ("timing_mode", "adaptive_latency", "calibrate_speed", "speed_fraction_per_s", "contact_overlap_ratio", "capture_every_n_frames", "input_latency_ms",
               "key_hold_ms", "continuous_fishing", "dry_run", "show_preview", "template_threshold",
               "min_action_interval_s", "unknown_timeout_s", "timing_timeout_s", "toggle_hotkey", "pause_hotkey", "snapshot_hotkey")
GUIDE = """# 比例速度与入条判定诊断数据

events中的JSONL按记录序号保存；t为本段开始后的单调时钟秒数，不是录像帧号。
frame含336×598归一化坐标、鱼漂左右边缘、完整目标缓存、当前可见黄条、估计速度、
分段编号、折返点候选、计算耗时、截图年龄、计划点击和重新截图时间。
格式5含双向到达时间、折返次数、端点来源、入条或条内补救模式及输入有效期限。
同时保留轨道全宽、比例速度及校准状态。velocity是预测采用速度，observed_velocity
与observed_speed_fraction来自实际短窗口位移；velocity_source标明固定或校准估计。
固定常数不能作为鱼漂恒速的证据；speed中的片段数量和离群值筛选状态用于核对校准。
input区分计划时刻、输入调用与SendInput返回附近的按下时刻、释放时刻及是否发送成功。
frame_seq关联输入依据；last_hit_seq及since_hit_ms关联后续帧。feedback与observed_transition
分别记录稳定目标变化、重试解锁与图像状态变化，不等于游戏内部命中回执。
external_space只记录测试运行且模拟器在前台时、自身注入窗口之外观察到的空格边沿，
可能漏掉短按和鼠标操作，不能作为无人干预的证明；不记录其他普通按键。
按下时刻只说明本机输入注入，不等于游戏实际接收或命中。
fish_id按确认的判定阶段计数，round_id按清楚可见的黄条变化计数；无法观察的阶段不补造。
motion_event中的freeze/gap/speed_change/turn表示模型重置原因，不直接证明游戏机制。
error记录异常类型、处理环节和公开源码位置，不保存异常原文或本机绝对路径。
end区分普通暂停、停止与异常退出，并带固定原因代码；meta汇总成功写入的各状态帧数。
crops只含判定条周围189×50局部图，文件名对应frame序号，可用于核对遮挡和轮廓识别。
画面持续未确认时每2秒保留局部条形图，合计最多12张，不采集完整画面。
meta.json仅保存必要设置，不含窗口标题、句柄、屏幕坐标、配置profile或本机路径。
source保存对应版本公开源码；不含运行环境、原始配置、完整截图、录像或其他日志。
没有HIT时也保留有限的判定条样本。图像编码和压缩在暂停后执行，避免阻塞临近按键。

分析时分别比较同一轮、不同轮、不同鱼的有符号速度、残差及样本间隔；暂停和折返前后分段。
对比实际输入时刻与入条的预测时刻，并用后续目标变化检查结果；不把目标变化延迟当成纯输入延迟。
"""


class TrialRecorder:
    def __init__(self, data_root, emit=None):
        self.root = Path(data_root)
        self.emit = emit or (lambda *args: None)
        self.current = None
        self.workers = []
        self.last_bundle = None
        self.last_error = None

    def start(self, cfg):
        if self.current is not None:
            return
        ident = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        directory = self.root / "trial_records" / ident
        directory.mkdir(parents=True)
        stream = (directory / "events_000.jsonl").open("w", encoding="utf-8", buffering=65536)
        self.current = dict(directory=directory, stream=stream, start=time.perf_counter(), rows=0,
                            chunk=0, bytes=0, cfg={k:cfg[k] for k in CONFIG_KEYS if k in cfg},
                            ring=deque(maxlen=6), crops=0, after=0, fish=0, previous_state="UNKNOWN",
                            selected={}, periodic_count=0, next_periodic=0, state_counts={},
                            sent_actions={}, errors=0, last_flush=time.perf_counter(),
                            last_frame_seq=None, last_hit_seq=None, last_hit_at=None,
                            last_frame_at=None, last_bar_at=-float("inf"), external_space_presses=0,
                            unknown_crops=0,next_unknown_crop=0,unknown_since=None)

    def _write(self, kind, now, values):
        item = self.current
        if item is None:
            return
        row = {"kind": kind, "seq": item["rows"], "t": round(now-item["start"], 6), **values}
        line = dumps(row, separators=(",", ":"))+"\n"
        if item["bytes"] > 16*1024*1024:
            item["stream"].close()
            item["chunk"] += 1
            item["bytes"] = 0
            item["stream"] = (item["directory"] / f'events_{item["chunk"]:03}.jsonl').open("w", encoding="utf-8", buffering=65536)
        item["stream"].write(line)
        item["bytes"] += len(line.encode("utf-8"))
        item["rows"] += 1
        if now-item["last_flush"] >= 1:
            item["stream"].flush()
            item["last_flush"] = now

    def frame(self, obs, decision, controller, frame, frame_time, decision_time, capture_ms, process_ms):
        item = self.current
        if item is None:
            return
        fish = item["fish"]
        new_fish = obs.state == "TIMING" and (item["previous_state"] not in ("TIMING", "UNKNOWN") or fish == 0)
        if new_fish:
            fish += 1
        motion = controller.contact.motion
        rel = lambda value: None if value is None else round(value-item["start"], 6)
        row = dict(state=obs.state, variant=obs.variant, confidence=obs.confidence, fish_id=fish,
                   cast_id=controller.cast_count, round_id=decision.round_id,
                   marker=obs.marker, marker_left=obs.marker_left, marker_right=obs.marker_right,
                   track_left=obs.track_left,track_right=obs.track_right,
                   speed=controller.speed.snapshot(),
                   observed_velocity=motion.observed_velocity,velocity_source=motion.estimate_source,
                   observed_speed_fraction=(None if motion.observed_velocity is None or motion.track_width is None
                                            else motion.observed_velocity/motion.track_width),
                   visible_target=obs.target, target=controller.target, geometry_clear=obs.geometry_clear,
                   velocity=decision.velocity, prediction=decision.predicted_x, contact_x=decision.contact_x,
                   fit_error=decision.fit_error, motion_event=decision.motion_event, segment=motion.segment,
                   endpoints=motion.endpoints(), turn_candidates={k:list(v) for k,v in motion.turns.items()},
                   action=decision.action, check_at=rel(decision.check_at), send_at=rel(decision.send_at),
                   arrival_ms=decision.arrival_ms,arrival_direction=decision.arrival_direction,
                   planned_turns=decision.planned_turns,endpoint_source=decision.endpoint_source,
                   click_mode=decision.click_mode,valid_until=rel(decision.valid_until),
                   decision_at=rel(decision_time), capture_ms=capture_ms, processing_ms=process_ms,
                   frame_age_ms=max(0, decision_time-frame_time)*1000, latency=controller.latency.snapshot(),
                   reason=decision.reason, scores=obs.scores, feedback=decision.feedback,
                   retry_count=decision.retry_count, pause_code=decision.pause_code,
                   last_hit_seq=item["last_hit_seq"],
                   since_hit_ms=None if item["last_hit_at"] is None else max(0,frame_time-item["last_hit_at"])*1000,
                   frame_interval_ms=None if item["last_frame_at"] is None else (frame_time-item["last_frame_at"])*1000,
                   observed_transition=(item["previous_state"]+":"+obs.state
                                        if obs.state not in ("UNKNOWN",item["previous_state"]) else None))
        seq = item["rows"]
        self._write("frame", frame_time, row)
        item["last_frame_seq"], item["last_frame_at"] = seq, frame_time
        item["fish"] = fish
        if obs.state != "UNKNOWN":
            item["previous_state"] = obs.state
            item["unknown_since"]=None
        elif item["unknown_since"] is None:
            item["unknown_since"]=frame_time
        item["state_counts"][obs.state] = item["state_counts"].get(obs.state, 0)+1
        if obs.state == "TIMING":
            item["last_bar_at"] = frame_time
        if obs.state == "TIMING" or (obs.state == "UNKNOWN" and frame_time-item["last_bar_at"] <= .5):
            item["ring"].append((seq, frame[376:426, 77:266].copy()))
            if decision.feedback in ("target_changed", "retry_armed"):
                for before_seq, image in item["ring"]:
                    self._save_crop(before_seq, image)
            if item["after"]:
                self._save_crop(seq, item["ring"][-1][1])
                item["after"] -= 1
            if new_fish or frame_time >= item["next_periodic"]:
                self._save_crop(seq, item["ring"][-1][1], periodic=True)
                item["next_periodic"] = frame_time+1
        elif (obs.state=="UNKNOWN" and frame_time-item["unknown_since"]>=1 and
              item["unknown_crops"]<12 and frame_time>=item["next_unknown_crop"]):
            self._save_crop(seq,frame[376:426,77:266].copy())
            item["unknown_crops"]+=1
            item["next_unknown_crop"]=frame_time+2

    def _save_crop(self, seq, image, periodic=False):
        item = self.current
        if item is None or (periodic and (item["periodic_count"] >= 60 or len(item["selected"])>=180)):
            return
        if seq in item["selected"]:
            return
        if image.shape != (50,189,3):
            raise ValueError("判定条局部图尺寸不正确")
        if len(item["selected"])>=180:
            # 关键新样本替换最早局部图，长时间运行也能留下后来的窄条与输入。
            item["selected"].pop(next(iter(item["selected"])))
        item["selected"][seq] = image
        item["crops"] = len(item["selected"])
        if periodic:
            item["periodic_count"] += 1

    def input(self, action, requested_at, win, sent, skipped=None):
        item = self.current
        if item is None:
            return
        relative = lambda t: None if t is None else round(t-item["start"], 6)
        input_seq = item["rows"]
        begin,end=getattr(win,"last_send_begin",None),getattr(win,"last_send_end",None)
        call_ms=(end-begin)*1000 if sent and begin is not None and end is not None else None
        self._write("input", time.perf_counter(), {
            "action": action, "requested_at": relative(requested_at), "sent": bool(sent), "skipped": skipped,
            "input_call_ms":call_ms,"input_time_uncertain":bool(call_ms is not None and call_ms>50),
            "frame_seq": item["last_frame_seq"], "fish_id": item["fish"],
            "send_begin": relative(getattr(win, "last_send_begin", None)) if sent else None,
            "send_end": relative(getattr(win, "last_send_end", None)) if sent else None,
            "key_down_at": relative(getattr(win, "last_key_down_at", None)) if sent else None,
            "key_up_at": relative(getattr(win, "last_key_up_at", None)) if sent else None})
        if sent:
            item["sent_actions"][action] = item["sent_actions"].get(action, 0)+1
        if action == "HIT":
            for seq, image in item["ring"]:
                self._save_crop(seq, image)
            item["after"] = 12
            if sent:
                item["last_hit_seq"] = input_seq
                item["last_hit_at"] = getattr(win, "last_key_down_at", None)

    def external_space(self, now, down):
        if self.current is None:
            return
        self._write("external_space", now, {"down": bool(down), "fish_id": self.current["fish"],
                                           "frame_seq": self.current["last_frame_seq"]})
        self.current["external_space_presses"] += int(bool(down))

    def fault(self, exc, component):
        if self.current is None:
            return
        locations = []
        for entry in traceback.extract_tb(exc.__traceback__):
            try:
                name = Path(entry.filename).resolve().relative_to(ROOT.resolve()).as_posix()
            except ValueError:
                continue
            if name.startswith("fisher/") or name in ("main.py", "replay.py"):
                locations.append({"file": name, "line": entry.lineno, "function": entry.name})
        self._write("error", time.perf_counter(), {"exception": type(exc).__name__,
                    "component": component, "locations": locations[-6:]})
        self.current["errors"] += 1

    def finish(self, cause="pause", reason_code="unspecified"):
        item = self.current
        if item is None:
            return
        try:
            allowed = {"manual_pause", "toggle_pause", "focus_lost", "controller_pause", "runtime_error",
                       "stop", "arming", "recording", "activation_failed", "input_cancelled", "unspecified",
                       "duration_limit", "cast_limit", "unknown_timeout", "action_ack_timeout",
                       "wait_timeout", "timing_timeout", "hit_ack_timeout"}
            self._write("end", time.perf_counter(), {
                "cause": cause if cause in ("pause", "stop", "error") else "pause",
                "reason_code": reason_code if reason_code in allowed else "unspecified"})
        finally:
            try:
                item["stream"].close()
            finally:
                self.current = None
        metadata = {"schema_version": 5, "application": __version__, "settings": item["cfg"],
                    "rows": item["rows"], "fish_count": item["fish"], "crop_count": item["crops"],
                    "state_counts": item["state_counts"], "sent_actions": item["sent_actions"], "errors": item["errors"],
                    "external_space_presses": item["external_space_presses"],
                    "environment": {"python": ".".join(map(str,sys.version_info[:3])), "numpy": np.__version__, "opencv": cv2.__version__},
                    "normalized_size": [336,598], "crop_rect": [77,376,189,50],
                    "input_clock": "host SendInput interval, not game acknowledgement"}
        (item["directory"] / "meta.json").write_text(dumps(metadata, indent=2), encoding="utf-8")
        summary = f'黄条 {item["state_counts"].get("TIMING",0)} 帧，判定按键 {item["sent_actions"].get("HIT",0)} 次'
        worker = threading.Thread(target=self._export, args=(item["directory"],item["selected"],summary), daemon=False)
        self.workers = [w for w in self.workers if w.is_alive()]
        self.workers.append(worker)
        self.emit("diagnostic_status", "正在打包测试数据…")
        worker.start()

    def _export(self, directory, crops, summary):
        try:
            for seq, crop in crops.items():
                path = directory / "crops" / f"frame_{seq:08}.png"
                path.parent.mkdir(exist_ok=True)
                ok, buf = cv2.imencode(".png", crop, [cv2.IMWRITE_PNG_COMPRESSION, 1])
                if not ok:
                    raise OSError("无法编码判定条局部图")
                buf.tofile(str(path))
            crops.clear()
            output = self.root / "trial_exports" / f"tt_fish_trial_{directory.name}.zip"
            output.parent.mkdir(parents=True, exist_ok=True)
            temp = output.with_suffix(".zip.tmp")
            checksums = []
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                def add(name, data):
                    archive.writestr(name, data)
                    checksums.append(f"{sha256(data).hexdigest()}  {name}")
                add("README.md", GUIDE.encode("utf-8"))
                selected = [directory/"meta.json", *directory.glob("events_[0-9][0-9][0-9].jsonl"),
                            *(directory/"crops").glob("frame_[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].png")]
                for path in sorted(selected):
                    if path.is_file() and not path.is_symlink():
                        add(path.relative_to(directory).as_posix(), path.read_bytes())
                for name, data in source_payloads().items():
                    add("source/"+name, data)
                archive.writestr("SHA256SUMS", "\n".join(checksums)+"\n")
            with zipfile.ZipFile(temp) as archive:
                if archive.testzip() is not None:
                    raise OSError("诊断包校验失败")
            temp.replace(output)
            self.last_bundle = output
            self.emit("diagnostic_status", f"已保存：{output.name}\n{summary}")
        except Exception:
            self.last_error = "测试数据打包失败，原始记录仍保留在trial_records。"
            self.emit("diagnostic_status", self.last_error)

    def join(self):
        for worker in self.workers:
            worker.join()
