# SPDX-License-Identifier: GPL-3.0-only
"""比例速度、抗离群值校准与本机参数保存；不保存原始轨迹。"""
from collections import Counter, deque
import json
import math
from pathlib import Path
from statistics import median

DEFAULT_SPEED = .606


def reference_profile():
    return {"schema_version": 1, "fraction_per_second": DEFAULT_SPEED,
            "sample_count": 0, "relative_mad": 0.0, "source": "reference"}


def validate_profile(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("速度参数格式不正确")
    value = data.get("fraction_per_second")
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not .1 <= value <= 5:
        raise ValueError("比例速度应为0.1～5条宽/秒")
    count, spread = data.get("sample_count",0), data.get("relative_mad",0)
    if isinstance(count,bool) or not isinstance(count,int) or not 0 <= count <= 10000:
        raise ValueError("校准样本数无效")
    if isinstance(spread,bool) or not isinstance(spread,(int,float)) or not math.isfinite(spread) or not 0 <= spread <= 1:
        raise ValueError("校准离散度无效")
    return {"schema_version":1,"fraction_per_second":float(value),"sample_count":count,
            "relative_mad":float(spread),"source":"calibrated" if data.get("source")=="calibrated" else "reference"}


def load_profile(path, default_value=DEFAULT_SPEED):
    path=Path(path)
    return validate_profile(json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else validate_profile(
        dict(reference_profile(),fraction_per_second=default_value))


def save_profile(path, data):
    path=Path(path)
    data=validate_profile(data)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    temp.replace(path)


class SpeedCalibration:
    def __init__(self, value=DEFAULT_SPEED, enabled=False):
        self.value=float(value)
        self.calibrating=bool(enabled)
        self.completed=False
        self.saved=False
        self.samples=deque(maxlen=96)
        self.last_end=-math.inf
        self.rejected=0
        self.inliers=0
        self.relative_mad=0.0
        self.proposal=None
        self.stable_updates=0

    def offer(self, points, velocity, error, track_width, segment, now, last_press):
        if not self.calibrating or len(points)<3:
            return
        points=list(points)
        start,end=points[0][0],points[-1][0]
        # 相邻窗口不重复使用同一段位移，也不采纳输入动效、短片段和大残差。
        if start < self.last_end or end-start < .20 or now-last_press < .65:
            return
        self.last_end=end
        rate=abs(velocity)/track_width
        direction=1 if velocity>0 else -1
        steps=[(b[1]-a[1])/(b[0]-a[0])/track_width for a,b in zip(points,points[1:]) if b[0]>a[0]]
        if (not .1 <= rate <= 5 or error/track_width > .008 or not steps or
            any(v*direction <= 0 for v in steps) or
            median(abs(abs(v)-rate) for v in steps) > rate*.18 or
            any(b[0]-a[0] > .18 for a,b in zip(points,points[1:]))):
            self.rejected+=1
            return
        self.samples.append((rate,direction,segment))
        self._finish_if_stable()

    def offer_sparse(self, rate, direction, segment, now, last_press):
        """低频模式只校准未跨端点的位移区间，不增加截图或写入轨迹。"""
        if not self.calibrating or now-last_press < .65:
            return
        if not math.isfinite(rate) or not max(.1,self.value*.4) <= rate <= min(5,self.value*1.6):
            self.rejected += 1
            return
        self.samples.append((rate,direction,segment))
        self._finish_if_stable()

    def _finish_if_stable(self):
        if len(self.samples)<18:
            return
        middle=median(s[0] for s in self.samples)
        mad=median(abs(s[0]-middle) for s in self.samples)
        cutoff=max(middle*.025,3*1.4826*mad)
        good=[s for s in self.samples if abs(s[0]-middle)<=cutoff]
        self.inliers=len(good)
        if len(good)<18 or len(good)<len(self.samples)*.75:
            return
        rates=sorted(s[0] for s in good)
        middle=median(rates)
        self.relative_mad=median(abs(v-middle) for v in rates)/middle
        directions=Counter(s[1] for s in good)
        if (min(directions.get(-1,0),directions.get(1,0))<4 or len({s[2] for s in good})<3 or
            self.relative_mad>.025 or rates[int(len(rates)*.9)]-rates[int(len(rates)*.1)] > middle*.08):
            self.stable_updates=0
            return
        self.stable_updates=self.stable_updates+1 if self.proposal and abs(middle-self.proposal)<=middle*.015 else 1
        self.proposal=middle
        if self.stable_updates>=3:
            self.value=middle
            self.calibrating=False
            self.completed=True

    def profile(self):
        return {"schema_version":1,"fraction_per_second":self.value,"sample_count":self.inliers,
                "relative_mad":self.relative_mad,"source":"calibrated"}

    def snapshot(self):
        return {"fraction_per_second":self.value,"calibrating":self.calibrating,"completed":self.completed,
                "saved":self.saved,"windows":len(self.samples),"inliers":self.inliers,
                "rejected":self.rejected,"relative_mad":self.relative_mad}
