@echo off
chcp 936 >nul 2>nul
rem ===========================================================================
rem  AssignmentRadar - 环境安装（只装环境，不做登录引导）
rem  正常情况你不需要直接运行它：双击项目根目录的「一键安装.bat」即可。
rem  这个文件由 radar.bat 在发现没有 .venv 时自动调用。
rem
rem  退出码：0 = 成功，1 = 失败
rem
rem  【编码】本文件必须是 GBK(cp936)：保存时选 ANSI/GBK，不要选 UTF-8。
rem          UTF-8 版在 cmd /c（双击）路径上解析错位，实测不可靠。
rem  【注意】因为开了 enabledelayedexpansion，正文里不能出现裸的感叹号（会被吞掉）。
rem ===========================================================================
setlocal enabledelayedexpansion

rem 本文件在 scripts\ 下，项目根目录是它的上一级
cd /d "%~dp0.."
set "ROOT=%CD%"
set "PY=%ROOT%\.venv\Scripts\python.exe"

echo.
echo === AssignmentRadar 环境安装 ===
echo     项目目录：%ROOT%
echo.

rem ---------------------------------------------------------------- 1. 找 Python
where python >nul 2>nul
if errorlevel 1 goto :no_python

set "PYVER="
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
if not defined PYVER goto :no_python

echo [1/3] 检测到 Python %PYVER%

rem ------------------------------------------------------- 2. 版本必须 >= 3.11
set "PYMAJ="
set "PYMIN="
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set "PYMAJ=%%a"
    set "PYMIN=%%b"
)
set "OKVER=0"
if !PYMAJ! GTR 3 set "OKVER=1"
if !PYMAJ! EQU 3 if !PYMIN! GEQ 11 set "OKVER=1"
if "!OKVER!"=="0" goto :bad_version

rem ------------------------------------------------- 3. 路径里有没有非 ASCII 字符
rem 交给 Python 判断：findstr 的正则在中文代码页下会误报，不可靠。
rem 只是提醒，不阻断——现代 Python 一般能处理中文路径。
python -c "import os,sys; sys.exit(0 if all(ord(c) < 128 for c in os.getcwd()) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [注意] 当前路径含中文或特殊字符：%ROOT%
    echo        多数情况没问题；如果下面出现看不懂的报错，
    echo        把整个文件夹移到 C:\AssignmentRadar 再试一次。
    echo.
)

rem ------------------------------------------------------- 4. 创建虚拟环境
echo [2/3] 创建虚拟环境 .venv ...
if exist "%PY%" (
    echo       已存在，跳过。
) else (
    python -m venv "%ROOT%\.venv"
    if errorlevel 1 (
        echo.
        echo [X] 创建虚拟环境失败。
        echo     常见原因：Python 安装不完整，或磁盘权限不足。
        echo     可以在 CMD 里手动跑一次看具体报错：python -m venv .venv
        exit /b 1
    )
)

if not exist "%PY%" (
    echo [X] 虚拟环境建好了但找不到 python.exe，环境可能不完整。
    echo     请删掉 .venv 文件夹后重新运行本脚本。
    exit /b 1
)

rem ------------------------------------------------------- 5. 安装依赖
echo [3/3] 安装依赖（首次约 30-60 秒）...
"%PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check >nul 2>nul

"%PY%" -m pip install -r "%ROOT%\requirements.txt" --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo [注意] 默认源安装失败，改用清华镜像重试……
    "%PY%" -m pip install -r "%ROOT%\requirements.txt" --disable-pip-version-check -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo.
        echo [X] 依赖安装失败。
        echo     如果是网络问题，可以稍后重试，或手动执行：
        echo     .venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        exit /b 1
    )
)

rem ------------------------------------------------------- 6. 自检
echo.
"%PY%" -c "import requests, bs4" >nul 2>nul
if errorlevel 1 (
    echo [注意] 自检未通过：依赖似乎没装全，但脚本已执行完。
    echo        先试试 radar.bat check，还不行就删掉 .venv 重来一次。
    exit /b 1
)

echo [OK] 环境就绪
exit /b 0

rem ================================================================ 异常分支
:no_python
echo.
echo [X] 没找到 python 命令。
echo.
echo     请先安装 Python 3.11 或更新版本：
echo       https://www.python.org/downloads/
echo.
echo     安装第一步的底部有一个勾选框，务必勾上：
echo         [x] Add python.exe to PATH
echo     不勾的话，装完这里还是找不到。
echo.
echo     装完后关掉这个窗口，重新双击「一键安装.bat」。
start "" "https://www.python.org/downloads/"
exit /b 1

:bad_version
echo.
echo [X] 你的 Python 版本是 %PYVER%，太旧了。
echo.
echo     本项目需要 Python 3.11 或更新版本（用到了 3.11 才有的标准库模块）。
echo     请到 https://www.python.org/downloads/ 装一个新版本，
echo     装完关掉这个窗口，重新双击「一键安装.bat」。
start "" "https://www.python.org/downloads/"
exit /b 1
