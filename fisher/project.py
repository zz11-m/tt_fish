# SPDX-License-Identifier: GPL-3.0-only
"""项目公开信息与主页入口。"""
import webbrowser

PROJECT_URL = "https://github.com/zz11-m/tt_fish"
BILIBILI_URL = "https://space.bilibili.com/488950689"
FREE_NOTICE = "免费提供，用于学习交流；无需购买授权或激活码。"


def open_page(url):
    if url not in (PROJECT_URL, BILIBILI_URL):
        raise ValueError("不支持的链接")
    if not webbrowser.open_new_tab(url):
        raise OSError("无法打开默认浏览器，请复制链接后手动打开：" + url)
