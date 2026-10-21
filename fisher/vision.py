# SPDX-License-Identifier: GPL-3.0-only
"""截图归一化、局部模板和颜色几何检测。所有坐标单位为336×598图像像素。"""
from dataclasses import dataclass, field
from pathlib import Path
import cv2
import numpy as np

W, H = 336, 598
TEMPLATE_DEFINITIONS = {
    "idle": ((134, 461, 198, 521), 7),
    "wait": ((133, 459, 200, 524), 7),
    "bite": ((114, 363, 228, 382), 9),
    "purple": ((133, 459, 201, 524), 7),
    "cap": ((248, 389, 263, 416), 6),
    "weight": ((104, 365, 122, 388), 12),
    "claim_title": ((88, 68, 248, 110), 9),
    "claim_button": ((130, 515, 166, 539), 9),
}
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
    def timing_geometry(hsv):
        x0, y0, x1, y1 = 77, 376, 266, 426
        roi = hsv[y0:y1, x0:x1]
        red = (((roi[:, :, 0] < 13) | (roi[:, :, 0] > 172)) &
               (roi[:, :, 1] > 120) & (roi[:, :, 2] > 95)).astype(np.uint8)
        n, labels, stats, centers = cv2.connectedComponentsWithStats(red)
        markers = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if 2 <= w <= 19 and 12 <= h <= 40 and area >= 20 and h > w:
                markers.append((int(area), float(centers[i][0] + x0)))
        marker = max(markers)[1] if markers else None
        # 黄色必须为横向色块，且位于窄轨道内部；紫色拉钩面板在外层优先排除。
        yellow = cv2.inRange(hsv[391:414, x0:x1], (18, 95, 160), (38, 255, 255))
        n, labels, stats, centers = cv2.connectedComponentsWithStats(yellow)
        parts = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if w >= 2 and 4 <= h <= 18 and area >= 10:
                parts.append((x+x0, x+x0+w-1, y+391, h, area))
        if not parts:
            return marker, None, None, False
        # 浮标可能遮挡黄色条；合并同一行、相隔不超过浮标宽度的两段。
        parts.sort()
        merged = []
        for p in parts:
            if merged and p[0]-merged[-1][1] <= 17 and abs(p[2]-merged[-1][2]) <= 3:
                a = merged.pop()
                merged.append((a[0], p[1], min(a[2], p[2]), max(a[3], p[3]), a[4]+p[4]))
            else:
                merged.append(p)
        left, right, y, height, area = max(merged, key=lambda a: a[4])
        clear = marker is not None and (marker < left-12 or marker > right+12)
        return marker, (float(left), float(right)), y+height/2, clear

    def detect(self, image):
        img = normalize(image)
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        scores = {name: self.match(img, name) for name in self.templates}
        btn = hsv[455:524, 131:203]
        green = float(np.mean((btn[:, :, 0] >= 35) & (btn[:, :, 0] <= 85) &
                              (btn[:, :, 1] > 95) & (btn[:, :, 2] > 115)))
        purple = float(np.mean((btn[:, :, 0] >= 120) & (btn[:, :, 0] <= 164) &
                               (btn[:, :, 1] > 90) & (btn[:, :, 2] > 110)))
        panel = hsv[356:387, 88:253]
        panel_purple = float(np.mean((panel[:, :, 0] >= 120) & (panel[:, :, 0] <= 167) & (panel[:, :, 1] > 95)))
        center = hsv[180:400, 10:326]
        # 纪录面板可为蓝、紫或金黄，仍需同时满足重量图标和暗背景条件。
        panel_hue = (((center[:, :, 0] >= 88) & (center[:, :, 0] <= 116)) |
                     ((center[:, :, 0] >= 120) & (center[:, :, 0] <= 167)) |
                     ((center[:, :, 0] >= 14) & (center[:, :, 0] <= 38)))
        result_color = float(np.mean(panel_hue & (center[:, :, 1] > 110) & (center[:, :, 2] > 80)))
        dim = float(np.mean(hsv[475:542, 125:215, 2]))
        claim = hsv[507:548, 96:242]
        claim_green = float(np.mean((claim[:, :, 0] >= 32) & (claim[:, :, 0] <= 85) &
                                    (claim[:, :, 1] > 95) & (claim[:, :, 2] > 140)))
        dark_edges = float(np.mean(hsv[130:430, :65, 2] < 80))
        scores.update(green=green, purple=purple, panel_purple=panel_purple,
                      result_color=result_color, dim=dim, claim_green=claim_green, dark_edges=dark_edges)
        claim_score = min(scores["claim_title"], scores["claim_button"])
        if claim_score >= max(.72, self.threshold) and claim_green > .5 and dark_edges > .65:
            return Observation("RESULT", claim_score, scores=scores, variant="claim"), img
        if scores["weight"] >= self.threshold and result_color > .45 and dim < 165:
            return Observation("RESULT", scores["weight"], scores=scores, variant="record"), img
        if panel_purple > 0.35 and scores["bite"] >= self.threshold and purple > 0.16:
            return Observation("BITE", scores["bite"], scores=scores), img
        marker, target, y, clear = self.timing_geometry(hsv)
        if purple > 0.16 and scores["cap"] >= self.threshold and panel_purple < 0.25:
            # 暂时遮挡黄色条或浮标时仍能认出阶段，但控制器不产生判定按键。
            return Observation("TIMING", scores["cap"], marker, target, y, clear, scores), img
        if green > 0.20 and scores["idle"] >= self.threshold:
            return Observation("IDLE", scores["idle"], scores=scores), img
        if float(np.mean(btn[:, :, 1])) < 55 and scores["wait"] >= self.threshold:
            return Observation("WAIT", scores["wait"], scores=scores), img
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
