# Windows免安装便携版

## 普通用户

在项目的GitHub Releases中选择 `tt_fish-版本号-Windows-x64.zip`，完整解压，双击 `启动钓鱼助手.bat`。下载页是否已有对应版本，以项目实际发布为准。不要下载GitHub自动生成的Source code ZIP来代替便携包。

适用64位Windows 10／11。包中带有独立CPython、Tcl/Tk、NumPy、OpenCV、Pillow和MSS；无需安装Python、Anaconda、PyCharm或额外运行pip，首次启动也不下载组件。雷电或MuMu模拟器和游戏仍需自行准备。程序不会修改系统Python、注册表或PATH。

根目录的 `app/` 保存完整项目源码和资源，`runtime/` 保存独立运行环境，`data/` 在运行后保存本机配置、日志、截图和录像。双击入口使用随包解释器的隔离模式，不读取用户安装的Python包和PYTHONPATH。

解压到当前账户可写的位置，不要放在压缩包预览窗口、只读盘或需要提权才能写入的系统目录。同一个数据目录重复启动会提示切换到已打开的窗口。不同文件夹中的独立副本仍应避免同时控制同一模拟器。

初次使用仍需要设置空格映射、选择窗口、框选完整游戏区域，并通过开始按钮或快捷键启动钓鱼。操作步骤见 [README](../README.md)。

## 更新与排查

- 更新：先退出旧版，将新包解压到新文件夹。保留旧目录备份；需要沿用设置时，仅复制旧 `data/` 中的 `config.json`、`speed_profile.json` 到新包的 `data/`，不存在的文件无需创建。从源码版迁移时，复制项目根目录中的这两个文件。历史截图和录像可继续保留在旧目录。
- 无法打开：双击 `检查运行环境.bat`，保留显示的错误；启动错误另写入 `data/logs/startup.log`。不要公开未经检查的错误日志。
- 文件被拦截或缺失：核对是否来自项目发布页、校验值是否一致，再按系统提示处理；不要以关闭安全软件作为常规步骤。
- 权限不一致：仅当模拟器以管理员身份运行时，才考虑以相同权限运行助手。
- 分享：发布构建得到的原始ZIP，不要重新压缩自己已经使用过的便携目录。

## 维护者重新生成

仓库根目录运行：

```bash
python build_windows.py
```

构建入口只需Python 3.10以上的标准库。首次构建需要联网，从固定清单下载运行环境和Windows依赖，逐个检查SHA-256。之后可复用 `.build-cache/` 中的文件。构建不会读取本机 `config.json` 或把日志、截图、录像带入成品。

输出在 `dist/tt_fish-版本号-Windows-x64.zip`。Windows主机上构建还会用随包解释器检查Tk、ImageTk、图像模板、录像编解码和显示器接口；该检查不截图、不发送按键。在其他操作系统上可组装相同便携包，但二进制结构和文件完整性检查不等于Windows运行验证。发布前仍需在Windows上检查界面、框选、暂停与真实模拟器操作。

在GitHub上传源码后，也可进入 **Actions → Windows portable → Run workflow**。工作流在Windows机器上构建、检查，成功后提供可下载的构建产物。下载后将便携ZIP手动添加到GitHub Release；工作流不会自动发布版本。仓库须包含 `.github/workflows/windows-portable.yml`，不要漏传以点开头的目录。

`packaging/windows-runtime.lock.json` 固定依赖版本、来源和哈希。更新依赖时要同时核对Windows x64／Python 3.12兼容性及许可，验证实际下载后再更新清单；不要只改版本字符串。当前锁定包没有wheel的额外`.data`安装目录；新增此类依赖须先扩展并测试构建规则。

本项目使用独立运行环境目录实现免安装，不需要在用户电脑临时解包单文件EXE。请始终一起分发入口、app和runtime。源码由明确文件清单生成；第三方二进制保留其上游许可，版本与下载来源记录在包内 `DEPENDENCIES.json`。
