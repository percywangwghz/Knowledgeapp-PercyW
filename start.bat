@echo off
cd /d "%~dp0"
title 投研知识库

rem ---- 探测可用的 Python(>=3.10),自动跳过微软商店占位程序 ----
set PY=
where py >nul 2>&1 && py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && python -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python")
if not defined PY (where python3 >nul 2>&1 && python3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python3")

if not defined PY (
    echo [错误] 没有找到可用的 Python 3.10 或更高版本
    echo.
    where python >nul 2>&1 && echo 提示:电脑上检测到 python 命令,但它是微软商店的占位程序(不是真的 Python),或版本低于 3.10。
    echo.
    echo 解决办法:
    echo   1. 到 https://www.python.org/downloads/ 下载安装 Python 3.10+
    echo      安装时务必勾选 "Add python.exe to PATH"
    echo   2. 如果已经装过 Python,请打开 设置 - 应用 - 高级应用设置 - 应用执行别名,
    echo      把 python.exe / python3.exe 的"应用安装程序"别名关掉
    pause
    exit /b 1
)

if not exist .venv (
    echo [初始化] 首次运行,正在创建虚拟环境...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [错误] 虚拟环境创建失败,上方是 Python 给出的具体原因。常见解决办法:
        echo   1. 杀毒软件/安全管家拦截了 .venv 的创建,暂时关闭后重试
        echo   2. 当前文件夹没有写入权限(如放在 Program Files 或网盘同步目录),移到普通目录再运行
        echo   3. 安装的是"嵌入式绿色版" Python(不支持 venv),请换装 python.org 官网的安装版
        rmdir /s /q .venv >nul 2>&1
        pause
        exit /b 1
    )
)

echo [依赖] 安装/校验依赖(首次大约 1-3 分钟,请保持联网)...
.venv\Scripts\pip.exe install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt || (
    echo [提示] 清华镜像安装失败,改用 PyPI 官方源重试...
    .venv\Scripts\pip.exe install -q -r requirements.txt || (
        echo [错误] 依赖安装失败,请检查网络后重试
        pause
        exit /b 1
    )
)

echo [启动] 投研知识库,浏览器将自动打开 http://localhost:8501
echo 关闭本窗口即停止运行。
.venv\Scripts\streamlit.exe run app.py --browser.gatherUsageStats false
pause
