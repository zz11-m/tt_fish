@echo off
chcp 65001 >nul
setlocal DisableDelayedExpansion
if not exist "%~dp0runtime\python.exe" goto missing
if not exist "%~dp0app\desktop.py" goto missing
"%~dp0runtime\python.exe" -I -B "%~dp0app\desktop.py" --check --data-dir "%~dp0data"
if errorlevel 1 goto failed
echo.
echo 运行环境检查通过。
pause
exit /b 0
:failed
echo.
echo 运行环境检查失败，请查看上方错误和 data\logs\startup.log。
pause
exit /b 1
:missing
echo 程序文件不完整，请重新完整解压压缩包。
pause
exit /b 1
