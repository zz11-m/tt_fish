# SPDX-License-Identifier: GPL-3.0-only
"""按固定下载清单构建含运行环境的Windows便携包。构建本身只需Python标准库。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from fisher import __version__
from fisher.config import ROOT
from fisher.packaging import source_payloads


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name or "\0" in name:
        raise ValueError(f"无效的归档路径：{name}")
    return path


def fetch(item, cache):
    if Path(item["filename"]).name != item["filename"]:
        raise ValueError("下载文件名无效")
    path = Path(cache) / item["filename"]
    if path.exists():
        if path.stat().st_size == item["size"] and file_hash(path) == item["sha256"]:
            return path
        raise ValueError(f"缓存校验失败，请删除后重试：{path.name}")
    if not item["url"].startswith("https://"):
        raise ValueError("下载地址必须使用HTTPS")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    try:
        request = urllib.request.Request(item["url"], headers={"User-Agent": "tt_fish-build"})
        with urllib.request.urlopen(request, timeout=45) as src, part.open("wb") as dst:
            total = 0
            while block := src.read(1024 * 1024):
                total += len(block)
                if total > item["size"]:
                    raise ValueError("下载长度超过清单")
                dst.write(block)
        if part.stat().st_size != item["size"] or file_hash(part) != item["sha256"]:
            raise ValueError(f"下载校验失败：{path.name}")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def unpack_runtime(archive, destination):
    destination = Path(destination)
    with tarfile.open(archive, "r:gz") as src:
        for member in src:
            name = safe_name(member.name)
            if name.parts[0] != "python":
                raise ValueError("Python归档根目录无效")
            if not member.isdir() and not member.isfile():
                raise ValueError("Python归档包含链接或特殊文件")
            if member.isdir() or "__pycache__" in name.parts or name.suffix == ".pyc":
                continue
            path = destination.joinpath(*name.parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            with src.extractfile(member) as stream, path.open("wb") as out:
                shutil.copyfileobj(stream, out)


def unpack_wheel(archive, destination):
    destination = Path(destination)
    with zipfile.ZipFile(archive) as src:
        for item in src.infolist():
            name = safe_name(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("依赖归档包含符号链接")
            # 固定清单中的依赖无.data目录；新依赖须单独核对安装布局。
            if name.parts[0].endswith(".data"):
                raise ValueError("依赖包含额外安装目录，需先更新安装规则")
            if item.is_dir():
                continue
            path = destination.joinpath(*name.parts)
            if path.exists():
                raise ValueError(f"依赖文件冲突：{item.filename}")
            path.parent.mkdir(parents=True, exist_ok=True)
            with src.open(item) as stream, path.open("wb") as out:
                shutil.copyfileobj(stream, out)


def validate_runtime(runtime):
    runtime = Path(runtime)
    required = ("python.exe", "pythonw.exe", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll", "tcl/tcl8.6/init.tcl",
                "tcl/tk8.6/tk.tcl", "Lib/tkinter/__init__.py", "LICENSE.txt",
                "Lib/site-packages/numpy/__init__.py", "Lib/site-packages/cv2/__init__.py",
                "Lib/site-packages/PIL/ImageTk.py", "Lib/site-packages/mss/__init__.py")
    for name in required:
        if not (runtime / name).is_file():
            raise ValueError(f"运行组件缺失：{name}")
    for path in runtime.rglob("*"):
        if path.suffix.lower() not in (".dll", ".pyd") and path not in (runtime / "python.exe", runtime / "pythonw.exe"):
            continue
        with path.open("rb") as src:
            if src.read(2) != b"MZ":
                raise ValueError(f"不是Windows程序：{path.name}")
            src.seek(0x3c)
            offset = struct.unpack("<I", src.read(4))[0]
            src.seek(offset)
            if src.read(6) != b"PE\0\0\x64\x86":
                raise ValueError(f"不是x64运行组件：{path.name}")


def write_bundle(folder, destination):
    folder, destination = Path(folder), Path(destination)
    entries = sorted(p for p in folder.rglob("*") if p.is_file())
    manifest = "".join(f"{file_hash(p)}  {p.relative_to(folder).as_posix()}\n" for p in entries)
    (folder / "SHA256SUMS").write_text(manifest, encoding="utf-8")
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    archive.write(path, (Path(folder.name) / path.relative_to(folder)).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise OSError("便携包完整性校验失败")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def build(cache, output):
    lock = json.loads((ROOT / "packaging/windows-runtime.lock.json").read_text(encoding="utf-8"))
    if lock["target"] != "windows-x86_64" or lock["schema_version"] != 1:
        raise ValueError("运行环境清单不受支持")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    items = [lock["python"], *lock["wheels"]]
    downloads = []
    for item in items:
        print(f"校验运行组件：{item['filename']}", flush=True)
        downloads.append(fetch(item, cache))
    with tempfile.TemporaryDirectory(prefix="tt_fish-build-", dir=output) as tmp:
        bundle = Path(tmp) / f"tt_fish-{__version__}-Windows-x64"
        bundle.mkdir()
        runtime = bundle / "runtime"
        unpack_runtime(downloads[0], runtime)
        for wheel in downloads[1:]:
            unpack_wheel(wheel, runtime / "Lib/site-packages")
        validate_runtime(runtime)
        for name, content in source_payloads().items():
            path = bundle / "app" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for source, name in (("start.bat", "启动钓鱼助手.bat"), ("check.bat", "检查运行环境.bat"),
                             ("portable-readme.txt", "使用说明.txt")):
            shutil.copyfile(ROOT / "packaging" / source, bundle / name)
        shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE")
        shutil.copyfile(ROOT / "NOTICE.md", bundle / "NOTICE.md")
        (bundle / "DEPENDENCIES.json").write_text(json.dumps(lock, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        if sys.platform == "win32":
            with tempfile.TemporaryDirectory(prefix="tt_fish-check-") as data:
                subprocess.run([str(runtime / "python.exe"), "-I", "-B", str(bundle / "app/desktop.py"),
                                "--check", "--data-dir", data], check=True, timeout=90)
        destination = output / f"tt_fish-{__version__}-Windows-x64.zip"
        write_bundle(bundle, destination)
    print(f"便携包已生成：{destination}")
    return destination


def main():
    parser = argparse.ArgumentParser(description="构建Windows x64免安装便携包")
    parser.add_argument("--cache", type=Path, default=ROOT / ".build-cache", help="已校验下载的缓存目录")
    parser.add_argument("--output", type=Path, default=ROOT / "dist", help="输出目录")
    args = parser.parse_args()
    build(args.cache, args.output)


if __name__ == "__main__":
    main()
