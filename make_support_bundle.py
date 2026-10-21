# SPDX-License-Identifier: GPL-3.0-only
"""生成本地诊断包；日志和图像需显式选择，不自动上传。"""
import argparse
from datetime import datetime
import json
from fisher.config import ROOT, DEFAULTS, load_config
from fisher.packaging import source_payloads, write_archive


def main():
    parser = argparse.ArgumentParser(description="打包当前源码，可选择加入诊断数据")
    parser.add_argument("--include-logs", action="store_true", help="加入最近3份日志和去除窗口框选的配置")
    parser.add_argument("--include-screenshots", action="store_true", help="加入最近10张游戏截图，可能含昵称或聊天内容")
    parser.add_argument("--include-video", action="store_true", help="加入最新录像与时间戳，可能含个人信息")
    args = parser.parse_args()
    payloads = source_payloads()
    selected = []
    if args.include_logs:
        for path in sorted((ROOT/"logs").glob("*.jsonl*"),key=lambda p:p.stat().st_mtime,reverse=True)[:3]:
            selected.append(path)
        cfg = load_config()
        clean = {key:cfg[key] for key in DEFAULTS}
        clean["profile"] = None
        payloads["diagnostics/config.redacted.json"] = (json.dumps(clean,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
    if args.include_screenshots:
        selected.extend(sorted((ROOT/"snapshots").glob("*.png"),key=lambda p:p.stat().st_mtime,reverse=True)[:10])
    if args.include_video:
        videos = sorted((ROOT/"recordings").glob("*.avi"),key=lambda p:p.stat().st_mtime,reverse=True)
        if videos:
            selected.append(videos[0])
            if videos[0].with_suffix(".csv").exists():
                selected.append(videos[0].with_suffix(".csv"))
    for path in selected:
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("诊断数据路径无效")
        payloads["diagnostics/"+path.relative_to(ROOT).as_posix()] = path.read_bytes()
    dest = ROOT/"support"/f"tt_fish_support_{datetime.now():%Y%m%d_%H%M%S_%f}.zip"
    write_archive(dest,payloads)
    print(f"诊断包已生成：{dest}")
    if selected:
        print("附加数据可能包含昵称、聊天内容或本机路径，请检查后再分享。")


if __name__ == "__main__":
    main()
