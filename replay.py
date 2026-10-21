# SPDX-License-Identifier: GPL-3.0-only
"""纯离线回放，不导入Windows输入模块，不会发送按键。"""
import argparse
import csv
from dataclasses import asdict
import json
from pathlib import Path
import cv2
from fisher.config import ROOT, load_config
from fisher.vision import Detector, read_image, write_image, annotate
from fisher.controller import Controller

def main():
    parser = argparse.ArgumentParser(description="分析截图或游戏录像，输出识别和拟议动作，不发送任何按键")
    parser.add_argument("--input", type=Path, help="截图或录像文件；省略时分析文档中的框选示例")
    parser.add_argument("--output", type=Path, default=ROOT/"replay_output")
    parser.add_argument("--crop", type=int, nargs=4, metavar=("X","Y","W","H"), help="外部录像中的游戏范围")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    detector = Detector(ROOT/"assets/templates",cfg["template_threshold"])
    if not args.input or args.input.suffix.lower() in (".png",".jpg",".jpeg",".bmp"):
        paths = [args.input] if args.input else [ROOT/"docs/images/crop-example.png"]
        for path in paths:
            img = read_image(path)
            crop = args.crop if args.input else (23,58,366,652)
            if crop:
                x,y,w,h = crop
                img = img[y:y+h,x:x+w]
            obs,frame = detector.detect(img)
            write_image(args.output/f"{path.stem}_annotated.png", annotate(frame,obs))
            print(f"{path.name}: {obs.state}, confidence={obs.confidence:.3f}, marker={obs.marker}, target={obs.target}")
        return
    cap = cv2.VideoCapture(str(args.input))
    if not cap.isOpened(): raise SystemExit(f"无法打开录像：{args.input}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0: raise SystemExit("录像帧率无效")
    times = None
    sidecar = args.input.with_suffix(".csv")
    if sidecar.exists():
        with sidecar.open(encoding="utf-8-sig") as f:
            times = [float(row["elapsed_seconds"]) for row in csv.DictReader(f)]
    controller = Controller(cfg)
    controller.reset(0)
    n = 0
    out = (args.output/"analysis.jsonl").open("w",encoding="utf-8")
    try:
        while True:
            ok,img = cap.read()
            if not ok: break
            if times is not None and n >= len(times):
                raise ValueError("录像帧数超过时间戳CSV，不能继续假定真实时间")
            t = times[n] if times is not None else n/fps
            if args.crop:
                x,y,w,h = args.crop
                img = img[y:y+h,x:x+w]
            obs,frame = detector.detect(img)
            decision = controller.step(obs,t)
            out.write(json.dumps({"frame":n,"t":t,"state":obs.state,"marker":obs.marker,"target":obs.target,
                                  **asdict(decision)}, ensure_ascii=False)+"\n")
            if decision.action:
                # 仅模拟锁存，用于观察决策顺序；录像不会因拟议按键而改变。
                controller.commit(decision.action,t)
                write_image(args.output/f"action_{n:06}.png", annotate(frame,obs,decision.predicted_x))
            n += 1
    finally:
        cap.release()
        out.close()
    print(f"已分析{n}帧，输出：{args.output}。拟议动作不是实际游戏执行或命中记录。")

if __name__ == "__main__": main()
