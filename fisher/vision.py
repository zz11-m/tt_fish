# SPDX-License-Identifier: GPL-3.0-only
"""截图归一化、局部模板和颜色几何检测。所有坐标单位为336×598图像像素。"""
from dataclasses import dataclass, field
from pathlib import Path
import cv2
import numpy as np

from .templates import W, H, TEMPLATE_DEFINITIONS

LABELS = {"IDLE": "准备下钩", "WAIT": "等待上钩", "BITE": "需要拉钩",
          "TIMING": "黄条判定", "RESULT": "结果展示", "UNKNOWN": "未确认画面"}

def read_image(path):
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图片：{path}")
    return img

def write_image(path, image):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise IOError("图片编码失败")
    data.tofile(str(path))

def normalize(image):
    if image is None or image.ndim != 3 or min(image.shape[:2]) < 100:
        raise ValueError("截图尺寸无效")
    return cv2.resize(image[:, :, :3], (W, H), interpolation=cv2.INTER_AREA)

@dataclass
class Observation:
    state: str = "UNKNOWN"
    confidence: float = 0.0
    marker: float | None = None
    target: tuple[float, float] | None = None
    track_y: float | None = None
    geometry_clear: bool = False
    scores: dict = field(default_factory=dict)
    variant: str | None = None
    marker_left: float | None = None
    marker_right: float | None = None
    track_left: float | None = None
    track_right: float | None = None

class Detector:
    def __init__(self, asset_dir, threshold=0.68):
        # 仅加载控件局部图，不依赖包含玩家资料的完整游戏截图。
        self.templates = {}
        for name, (box, pad) in TEMPLATE_DEFINITIONS.items():
            x1, y1, x2, y2 = box
            template = read_image(Path(asset_dir) / f"{name}.png")
            if template.shape[:2] != (y2-y1, x2-x1):
                raise ValueError(f"识别模板尺寸错误：{name}")
            self.templates[name] = (template, box, pad)
        self.threshold = threshold

    def match(self, img, name):
        template, (x1, y1, x2, y2), pad = self.templates[name]
        roi = img[max(0, y1-pad):min(H, y2+pad), max(0, x1-pad):min(W, x2+pad)]
        score = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
        return float(cv2.minMaxLoc(score)[1])

    @staticmethod
    def track_bounds(hsv):
        # 读取轨道两侧深色外框；统一以可见全宽为比例单位，不能混用截图宽度。
        band=hsv[395:410,77:266,2]<55
        columns=np.flatnonzero(np.sum(band,axis=0)>=3)
        if len(columns)<8 or float(np.mean(band))>.65:
            return None,None
        left,right=int(columns[0])+77,int(columns[-1])+77
        if not 120<=right-left<=188:
            return None,None
        return float(left),float(right)

    @staticmethod
    def timing_geometry(hsv):
        return Detector.timing_details(hsv)[:4]

    @staticmethod
    def timing_details(hsv):
        x0, y0, x1, y1 = 77, 376, 266, 426
        roi = hsv[y0:y1, x0:x1]
        red = (((roi[:, :, 0] < 13) | (roi[:, :, 0] > 172)) &
               (roi[:, :, 1] > 120) & (roi[:, :, 2] > 95)).astype(np.uint8)
        n, labels, stats, centers = cv2.connectedComponentsWithStats(red)
        markers = []
        for i in range(1, n):
            x, y, w, h, area = map(int, stats[i])
            # 红色鱼漂会被白色描边/高光分成几段；主体可能比高度稍宽。
            crosses_track = y+y0 < 411 and y+y0+h > 394
            if 2 <= w <= 22 and 10 <= h <= 40 and area >= 25 and h >= w*.55 and crosses_track:
                markers.append((int(area), float(centers[i][0] + x0), float(x+x0-1), float(x+x0+w)))
        best = max(markers) if markers else None
        marker = best[1] if best else None
        bounds = best[2:] if best else (None, None)
        # 黄色必须为横向色块，且位于窄轨道内部；紫色拉钩面板在外层优先排除。
        yellow = cv2.inRange(hsv[391:414, x0:x1], (18, 95, 160), (38, 255, 255))
        n, labels, stats, centers = cv2.connectedComponentsWithStats(yellow)
        parts = []
        for i in range(1, n):
            x, y, w, h, area = map(int, stats[i])
            if w >= 2 and 4 <= h <= 18 and area >= 10:
                parts.append((x+x0, x+x0+w-1, y+391, h, area))
        if not parts:
            return marker, None, None, False, *bounds
        # 浮标可能遮挡黄色条；合并同一行、相隔不超过浮标宽度的两段。
        parts.sort()
        merged = []
        for p in parts:
            covered_gap = (merged and marker is not None and
                           merged[-1][1] <= marker <= p[0] and
                           p[0]-merged[-1][1] <= bounds[1]-bounds[0]+4)
            if merged and (p[0]-merged[-1][1] <= 17 or covered_gap) and abs(p[2]-merged[-1][2]) <= 3:
                a = merged.pop()
                merged.append((a[0], p[1], min(a[2], p[2]), max(a[3], p[3]), a[4]+p[4]))
            else:
                merged.append(p)
        left, right, y, height, area = max(merged, key=lambda a: a[4])
        clear = bool(marker is not None and (marker < left-12 or marker > right+12))
        return marker, (float(left), float(right)), float(y+height/2), clear, *bounds

    def detect(self, image):
        img = normalize(image)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        scores = {}
        def score(name):
            if name not in scores:
                scores[name] = self.match(img, name)
            return scores[name]
        btn = hsv[455:524, 131:203]
        green = float(np.mean((btn[:, :, 0] >= 35) & (btn[:, :, 0] <= 85) &
                              (btn[:, :, 1] > 95) & (btn[:, :, 2] > 115)))
        purple = float(np.mean((btn[:, :, 0] >= 120) & (btn[:, :, 0] <= 164) &
                               (btn[:, :, 1] > 90) & (btn[:, :, 2] > 110)))
        panel = hsv[356:387, 88:253]
        panel_purple = float(np.mean((panel[:, :, 0] >= 120) & (panel[:, :, 0] <= 167) & (panel[:, :, 1] > 95)))
        center = hsv[180:400, 10:326]
        # 红橙渐变包含HSV红色的两端；仍需重量图标和暗背景联合确认。
        panel_hue = (((center[:, :, 0] >= 88) & (center[:, :, 0] <= 116)) |
                     ((center[:, :, 0] >= 120) & (center[:, :, 0] <= 167)) |
                     (center[:, :, 0] <= 38) | (center[:, :, 0] >= 168))
        result_color = float(np.mean(panel_hue & (center[:, :, 1] > 110) & (center[:, :, 2] > 80)))
        dim = float(np.mean(hsv[475:542, 125:215, 2]))
        claim = hsv[507:548, 96:242]
        claim_green = float(np.mean((claim[:, :, 0] >= 32) & (claim[:, :, 0] <= 85) &
                                    (claim[:, :, 1] > 95) & (claim[:, :, 2] > 140)))
        dark_edges = float(np.mean(hsv[130:430, :65, 2] < 80))
        scores.update(green=green, purple=purple, panel_purple=panel_purple,
                      result_color=result_color, dim=dim, claim_green=claim_green, dark_edges=dark_edges)
        if claim_green > .5 and dark_edges > .65:
            claim_score = min(score("claim_title"), score("claim_button"))
            if claim_score >= max(.72, self.threshold):
                return Observation("RESULT", claim_score, scores=scores, variant="claim"), img
        if result_color > .45 and dim < 165 and score("weight") >= self.threshold:
            return Observation("RESULT", score("weight"), scores=scores, variant="record"), img
        if panel_purple > 0.35 and purple > 0.16 and score("bite") >= self.threshold:
            return Observation("BITE", score("bite"), scores=scores), img
        if purple > 0.16 and panel_purple < 0.25:
            cap_score = score("cap")
            if cap_score < self.threshold:
                # 鱼漂经过右端会挡住原模板，使用未被遮挡的左端复核。
                cap_score = max(cap_score, score("cap_left"))
            if cap_score >= self.threshold:
                marker, target, y, clear, ml, mr = self.timing_details(hsv)
                tl,tr=self.track_bounds(hsv)
                return Observation("TIMING", cap_score, marker, target, y, clear, scores,
                                   marker_left=ml, marker_right=mr,track_left=tl,track_right=tr), img
        if green > 0.20 and score("idle") >= self.threshold:
            return Observation("IDLE", score("idle"), scores=scores), img
        if float(np.mean(btn[:, :, 1])) < 55 and score("wait") >= self.threshold:
            return Observation("WAIT", score("wait"), scores=scores), img
        return Observation(scores=scores), img

def annotate(img, obs, prediction=None):
    out = img.copy()
    cv2.rectangle(out, (77, 376), (266, 426), (170, 170, 170), 1)
    if obs.target:
        a, b = map(round, obs.target)
        cv2.rectangle(out, (a, 389), (b, 416), (0, 255, 0), 1)
    if obs.marker is not None:
        cv2.line(out, (round(obs.marker), 376), (round(obs.marker), 426), (0, 0, 255), 1)
    if prediction is not None and 0 <= prediction < W:
        cv2.line(out, (round(prediction), 380), (round(prediction), 422), (255, 0, 255), 1)
    cv2.rectangle(out, (0, 0), (W, 20), (20, 20, 20), -1)
    label = f"{obs.state}/{obs.variant.upper()}" if obs.variant else obs.state
    cv2.putText(out, f"{label}  {obs.confidence:.2f}", (5, 15), cv2.FONT_HERSHEY_SIMPLEX, .45, (255,255,255), 1)
    return out
