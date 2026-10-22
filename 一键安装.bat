@echo off
chcp 936 >nul 2>nul
rem ===========================================================================
rem  AssignmentRadar 一键安装
rem
rem  双击本文件即可。它会依次完成：
rem    1. 检查 Python（是否装了、版本够不够）
rem    2. 创建虚拟环境 + 安装依赖（失败自动换国内镜像）
rem    3. 在桌面创建快捷方式
rem    4. 打开浏览器让你登录平台
rem    5. 配一个长期令牌（推荐；配了之后数据就不再依赖会话）
rem    6. 顺手把浏览器里的会话读进配置（只为作业页截图，读不到不拦）
rem    7. 跑一次自检，把课程和作业列出来
rem    8. 问你要不要现在启动、要不要开机自启
rem
rem  全程不需要敲命令。
rem
rem  【编码】本文件必须是 GBK(cp936)：保存时选 ANSI/GBK，不要选 UTF-8。
rem          UTF-8 版在 cmd /c（双击）路径上解析错位，实测不可靠。
rem          GitHub 网页上显示乱码是预期行为：下载后用记事本按 ANSI/GBK 打开来读。
rem  【注意】因为开了 enabledelayedexpansion，正文里不能出现裸的感叹号（会被吞掉），
rem          只有 !ANS! 这类变量引用是有意为之。
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "ROOT=%CD%"

echo.
echo  ============================================================
echo    AssignmentRadar - Moodle 作业雷达
echo    安装向导
echo  ============================================================
echo.
echo  接下来大约 2 分钟，中途需要你做两件小事：
echo    1. 在浏览器里登录一次平台
echo    2. 顺手建一个长期令牌（推荐，约 1 分钟）
echo.
echo  令牌可以跳过。跳过了也能用，只是每 10 小时要重新读一次会话。
echo.
echo  ------------------------------------------------------------
echo.

rem ------------------------------------------------------------ 1. 装环境
call "%~dp0scripts\setup.bat"
if errorlevel 1 (
    echo.
    echo  ------------------------------------------------------------
    echo  [X] 安装没有完成，上面写了具体原因。
    echo      按提示处理后再双击本文件一次即可。
    echo.
    pause
    exit /b 1
)

rem ------------------------------------------------------------ 2. 快捷方式（桌面→开始菜单逐级降级）
call :make_shortcut

rem ------------------------------------------------------------ 3. 登录引导
echo.
echo  ------------------------------------------------------------
echo  [下一步] 请在浏览器里登录平台
echo.
echo    现在会打开 https://moodle.scnu.edu.cn
echo    用统一身份认证登录，看到「我的课程」页面就算好了。
echo.
start "" "https://moodle.scnu.edu.cn"
echo    登录完成后，回到这个窗口按任意键继续。
echo.
pause >nul

rem ------------------------------------------------------------ 4. 长期令牌
echo.
echo  ------------------------------------------------------------
echo  [推荐] 配一个长期令牌
echo.
echo    配了它：会话过期也不影响作业数据，之后基本不用再管这个工具。
echo    不配也行：大约每 10 小时要重新读取一次会话（一条命令，10 秒）。
echo.
echo    全程不需要账号密码，令牌可以随时在同一个页面吊销。
echo    浏览器应该已经登录好了，下面这个页面打开就能直接建。
echo.
set "ANS0="
set /p "ANS0=现在配吗？(Y/n) "
if /i "!ANS0!"=="n" goto :skip_token
echo.
call "%~dp0radar.bat" set-token
if errorlevel 1 (
    echo.
    echo  [注意] 令牌没配上，不影响继续。之后随时可以再运行：
    echo          radar.bat set-token
    echo.
) else (
    set "HAVE_TOKEN=1"
)
:skip_token

rem ------------------------------------------------------------ 5. 取回会话
echo.
echo  [正在读取浏览器里的登录状态……]
echo.
call "%~dp0radar.bat" import --sso
if not errorlevel 1 goto :import_done
if defined HAVE_TOKEN goto :import_soft
echo.
echo  ------------------------------------------------------------
echo  [注意] 没能直接读到会话，也没有配令牌，现在还没有可用的数据通路。
echo.
echo      新版 Chrome 会把 cookie 加密，普通方式读不出来。
echo      请改用下面这条命令（会打开一个独立窗口让你登录）：
echo.
echo          radar.bat browser login
echo.
echo      登录一次之后就不用再管了，然后再双击本文件一次。
echo.
echo  ------------------------------------------------------------
echo.
pause
exit /b 1

:import_soft
echo.
echo  [注意] 没能读到会话。不影响作业数据（令牌通路已经在用），
echo         只是作业页整页截图用不了。想要截图就运行：
echo.
echo          radar.bat browser login
echo.
:import_done

rem ------------------------------------------------------------ 6. 自检
echo.
echo  [正在验证……]
echo.
call "%~dp0radar.bat" check
if errorlevel 1 (
    echo.
    echo  [注意] 验证没通过。请把上面的输出截图发给作者。
    echo.
    pause
    exit /b 1
)

rem ------------------------------------------------------------ 7. 完成
echo.
echo  ============================================================
echo    装好了
echo  ============================================================
echo.
echo    桌面（或开始菜单）里已经有 A-Radar 图标，以后点它就能启动。
echo    也可以直接双击项目里的 radar.bat。
echo.

set "ANS="
set /p "ANS=要现在启动吗？(Y/n) "
if /i "!ANS!"=="n" goto :skip_run
echo.
echo    正在启动……
start "" "%~dp0radar.bat"
:skip_run

echo.
set "ANS2="
set /p "ANS2=要开机自动启动吗？(y/N) "
if /i not "!ANS2!"=="y" goto :skip_auto
call "%~dp0radar.bat" install
:skip_auto

echo.
echo  ------------------------------------------------------------
echo    全部完成。这个窗口可以关掉了。
echo.
echo    常用命令（在项目目录下执行）：
echo      radar.bat list      看作业清单（秒开）
echo      radar.bat check     验证登录状态
echo      radar.bat set-token 配令牌 / 换令牌
echo      radar.bat import    会话过期后重新读取
echo.
echo    详细说明见 docs\manual.md
echo  ------------------------------------------------------------
echo.
pause
exit /b 0

rem ================================================================ 子过程
:make_shortcut
echo  [正在创建快捷方式……]
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $p=Join-Path $d 'A-Radar.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath='%ROOT%\radar.bat'; $s.WorkingDirectory='%ROOT%'; $s.WindowStyle=7; $s.Description='AssignmentRadar - Moodle 作业雷达'; $s.Save()" >nul 2>nul
if not errorlevel 1 (
    echo       完成：桌面多了 A-Radar 图标（就是 AssignmentRadar）
    goto :eof
)
rem 桌面失败：常见原因是桌面被 OneDrive 重定向、或没有写桌面的权限 → 退到开始菜单
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'; $p=Join-Path $d 'A-Radar.lnk'; $s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath='%ROOT%\radar.bat'; $s.WorkingDirectory='%ROOT%'; $s.WindowStyle=7; $s.Description='AssignmentRadar - Moodle 作业雷达'; $s.Save()" >nul 2>nul
if not errorlevel 1 (
    echo       桌面没建成，已放进开始菜单：按 Win 键搜 A-Radar 就能找到
    goto :eof
)
rem 两级都失败：不再尝试写文件，直接把三条启动路径告诉用户
echo       没能创建快捷方式（不影响使用）。三种启动方式任选：
echo         1) 双击本目录里的 radar.bat
echo         2) 在本目录执行：radar.bat run
echo         3) 想开机自动起：radar.bat install（这套走的是另一套机制，通常不会失败）
goto :eof
