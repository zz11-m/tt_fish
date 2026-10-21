# SPDX-License-Identifier: GPL-3.0-only
"""生成可公开发布的源码包。"""
from fisher import __version__
from fisher.config import ROOT
from fisher.packaging import source_payloads, write_archive


def main():
    path = write_archive(ROOT/"dist"/f"tt_fish-{__version__}.zip", source_payloads())
    print(f"发布包已生成：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
