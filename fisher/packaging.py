# SPDX-License-Identifier: GPL-3.0-only
"""按明确的源文件清单生成发布包，排除本机运行数据。"""
from hashlib import sha256
import json
from pathlib import Path
import zipfile
from .config import DEFAULTS, ROOT, validate_config
from .vision import TEMPLATE_DEFINITIONS

PUBLIC_FILES = (
    "README.md", "LICENSE", "NOTICE.md", "CHANGELOG.md", "requirements.txt",
    ".gitignore", ".gitattributes", "config.example.json",
    "main.py", "replay.py", "selfcheck.py", "build_release.py", "make_support_bundle.py",
    "fisher/__init__.py", "fisher/config.py", "fisher/controller.py", "fisher/runtime.py",
    "fisher/scheduling.py", "fisher/vision.py", "fisher/windows.py", "fisher/project.py", "fisher/packaging.py",
    "tests/fixtures.py", "tests/test_behavior.py", "tests/test_options.py", "tests/test_startup.py", "tests/test_release.py",
    "docs/DEVELOPMENT.md", "docs/PRIVACY.md", "docs/images/crop-example.png",
    ".github/ISSUE_TEMPLATE/bug_report.md",
) + tuple(f"assets/templates/{name}.png" for name in TEMPLATE_DEFINITIONS)


def source_payloads(root=ROOT):
    root = Path(root)
    payloads = {}
    for name in PUBLIC_FILES:
        path = root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"发布文件不能是符号链接或越界路径：{name}")
        if not path.is_file():
            raise FileNotFoundError(f"缺少发布文件：{name}")
        payloads[name] = path.read_bytes()
    example = json.loads(payloads["config.example.json"].decode("utf-8"))
    validate_config(example)
    if example.get("profile") is not None or set(example)-set(DEFAULTS):
        raise ValueError("示例配置不得包含个人框选或额外字段")
    return payloads


def write_archive(path, payloads):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for name in payloads:
        if Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name or name == "SHA256SUMS":
            raise ValueError(f"无效的压缩包路径：{name}")
    manifest = "\n".join(f"{sha256(data).hexdigest()}  {name}" for name,data in sorted(payloads.items()))+"\n"
    temp = path.with_suffix(path.suffix+".tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for name,data in sorted(payloads.items()):
                archive.writestr(name,data)
            archive.writestr("SHA256SUMS",manifest)
        with zipfile.ZipFile(temp) as archive:
            if archive.testzip() is not None:
                raise OSError("压缩包完整性校验失败")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path
