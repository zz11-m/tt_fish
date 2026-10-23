@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
if not exist "%~dp0runtime\pythonw.exe" goto missing
if not exist "%~dp0app\desktop.py" goto missing
start "" "%~dp0runtime\pythonw.exe" -I -B "%~dp0app\desktop.py" --data-dir "%~dp0data"
exit /b 0
:missing
echo 程序文件不完整。请先将整个压缩包解压，再运行本入口。
echo 请保留同目录的 runtime 和 app 文件夹。
pause
exit /b 1
