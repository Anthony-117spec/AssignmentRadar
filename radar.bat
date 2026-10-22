@echo off
chcp 936 >nul 2>nul
rem ===========================================================================
rem  AssignmentRadar 命令行入口
rem  用法：radar.bat <命令>        例：radar.bat check
rem        radar.bat              不带命令 = 启动（托盘 + 轮询 + 面板）
rem
rem  没装环境时会自动先装一次，不需要你手动建虚拟环境。
rem
rem  【编码】本文件必须是 GBK(cp936)：编辑器保存时请选 ANSI/GBK，不要选 UTF-8。
rem          实测：UTF-8 版在资源管理器双击（走 cmd /c）这条路径上解析错位，
rem          报「系统找不到指定的文件」根本不执行；改成 chcp 65001 也不解决。
rem ===========================================================================
setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo [注意] 还没装运行环境，先自动安装一次……
    call "%~dp0scripts\setup.bat"
    if errorlevel 1 (
        echo.
        echo [X] 环境安装失败，上面有具体原因。
        echo     修好后重新运行本文件即可。
        echo.
        pause
        exit /b 1
    )
)

"%PY%" "%~dp0radar.py" %*
exit /b %errorlevel%
