# 版权与来源

Copyright (C) 2026 zz11-m and contributors.

项目源码及原创项目文档采用 GNU General Public License version 3 only（GPL-3.0-only），完整条款见 [LICENSE](LICENSE)。分发修改版时应按该许可证保留许可与版权声明、注明修改，并向接收者提供相应源码。依赖库仍适用各自许可证。

本项目由作者免费提供，用于学习和交流，没有付费授权、激活码或官方收费解锁功能。代码来源：[zz11-m/tt_fish](https://github.com/zz11-m/tt_fish)。作者公开主页：[哔哩哔哩](https://space.bilibili.com/488950689)。

免费发布不意味着GPL禁止收费再分发；GPL允许商业分发，但分发者须遵守许可义务。本项目未授权任何人冒充原作者、虚构官方收费授权或声称拥有排他权利。本说明不向GPL附加用途或收费限制。

`assets/templates/` 中的局部游戏控件、`tests/assets/` 中的判定条与重量图标局部样本和 `docs/images/crop-example.png` 中的游戏、模拟器界面属于第三方视觉素材，用于功能识别、回归验证及操作示例，其权利归各自权利人；不属于上述代码许可证的授权范围。项目不声称拥有这些素材或获得游戏、模拟器官方背书。需要独立复用或再分发相关图像时，应另行确认权利。

项目不修改游戏客户端、不读取游戏内存，也不提供避免检测或封禁的保证。使用前应确认相关软件和服务允许该操作方式。

## 便携运行环境

Windows便携包随附CPython（python-build-standalone发行）、Tcl/Tk、NumPy、OpenCV headless、Pillow及MSS。它们分别适用上游许可证；项目的GPL声明不替换第三方许可。Python许可保留在runtime/LICENSE.txt，Tcl/Tk许可在runtime/tcl中，Python依赖的许可在相应dist-info目录中，OpenCV另保留LICENSE-3RD-PARTY.txt。各组件版本、下载URL和SHA-256记录在DEPENDENCIES.json，可据此取得对应上游资料。便携包的app目录包含完整项目源码。
