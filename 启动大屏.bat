@echo off
setlocal
cd /d "%~dp0"
title RFM 可视化大屏

echo ============================================================
echo   RFM 客户价值分析大屏（浅色投行版）
echo ============================================================
echo.

rem ---- 1) Python 检测 ----
where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

rem ---- 2) 依赖检测（失败则自诊断 + 自动修复） ----
python -c "import flask, pandas" >nul 2>nul
if errorlevel 1 goto DEPS_CHECK

rem ---- 3) 端口占用检测 ----
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 goto PORTBUSY

echo [OK] 环境检查通过，正在启动服务...
echo.
echo   浏览器访问:  http://127.0.0.1:5000
echo   停止服务  :  关闭本窗口 或按 Ctrl+C
echo.
python dashboard_app.py

echo.
echo 服务已停止。
pause
exit /b 0

:DEPS_CHECK
echo [!] 依赖自动检测未通过，下面显示真实原因...
echo.
echo --- 1. 当前命中的 Python ---
where python
python --version 2>&1
echo.
echo --- 2. 依赖导入的真实报错 ---
python -c "import flask, pandas"
echo.
echo --- 3. 尝试自动安装依赖（需要联网，约 1-2 分钟）---
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [错误] 自动安装失败。请手动执行: pip install -r requirements.txt
  echo 并把本窗口截图发给助手分析。
  echo.
  pause
  exit /b 1
)
echo.
echo [OK] 依赖就绪，正在启动大屏...
echo   浏览器访问:  http://127.0.0.1:5000
echo.
python dashboard_app.py

echo.
echo 服务已停止。
pause
exit /b 0

:NOPYTHON
echo [错误] 未检测到 Python。
echo        请安装 Python 3，安装时务必勾选 "Add Python to PATH"。
echo        或在本目录手动执行: python dashboard_app.py
echo.
pause
exit /b 1

:PORTBUSY
echo [提示] 端口 5000 已被占用，大屏可能已经在运行。
echo        请直接用浏览器打开: http://127.0.0.1:5000
echo        如需重启，请先关闭已有的黑窗口（或结束 python 进程）。
echo.
pause
exit /b 0
