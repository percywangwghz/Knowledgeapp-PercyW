@echo off
cd /d "%~dp0"
title 投研知识库

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY (where py >nul 2>&1 && set PY=py -3)
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.10 或更高版本：
    echo https://www.python.org/downloads/
    echo 安装时务必勾选 "Add python.exe to PATH"
    pause
    exit /b 1
)

if not exist .venv (
    echo [初始化] 首次运行，正在创建虚拟环境...
    %PY% -m venv .venv || (echo [错误] 虚拟环境创建失败 & pause & exit /b 1)
)

echo [检查] 安装/校验依赖（首次约 1-3 分钟，需联网）...
.venv\Scripts\pip.exe install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt || (echo [错误] 依赖安装失败，请检查网络后重试 & pause & exit /b 1)

echo [启动] 投研知识库，浏览器将自动打开 http://localhost:8501
echo （关闭本窗口即停止运行）
.venv\Scripts\streamlit.exe run app.py --browser.gatherUsageStats false
pause
