@echo off
rem ===========================================================================
rem  OpenCode Go 桥接代理 · 后台常驻入口
rem  用法：
rem    service.cmd            确认在跑；没跑就最小化拉起，然后打印一屏状态（桌面按钮走这个）
rem    service.cmd ensure     只在没跑时拉起，不打印、不等待（开机自启走这个）
rem    service.cmd stop       停掉占用端口的那个实例
rem    service.cmd tui        打开终端控制台（需要 python；平时双击托盘图标即可）
rem    service.cmd status     只查一屏完整状态，不动进程（10 秒自动关）
rem ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8787

set "MODE=%~1"
if "%MODE%"=="" set "MODE=auto"

rem ---- 找 node：先 PATH，再 WorkBuddy 自带的 ----
set "NODE="
where node >nul 2>nul
if %errorlevel%==0 set "NODE=node"
if not defined NODE (
  set "NODEVERS=%USERPROFILE%\.workbuddy\binaries\node\versions"
  if exist "%NODEVERS%\current" (
    set "CURV="
    set /p CURV=<"%NODEVERS%\current"
    if defined CURV if exist "%NODEVERS%\!CURV!\node.exe" set "NODE=%NODEVERS%\!CURV!\node.exe"
  )
  if not defined NODE if exist "%NODEVERS%" (
    for /f "delims=" %%d in ('dir /b /ad /o-n "%NODEVERS%" 2^>nul') do (
      if not defined NODE if exist "%NODEVERS%\%%d\node.exe" set "NODE=%NODEVERS%\%%d\node.exe"
    )
  )
)
if not defined NODE (
  echo.
  echo [错误] 找不到 Node.js。装个 Node 18+，或先运行一次 WorkBuddy 让它下载自带的 node。
  echo 找过的位置：%USERPROFILE%\.workbuddy\binaries\node\versions
  echo.
  if /i not "%MODE%"=="ensure" pause
  exit /b 1
)

rem ---- 找 pythonw：起代理要它来解密钥，node 解不开 DPAPI ----
set "PYW="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not defined PYW (
  for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%p"
)

rem ---- 探活：/healthz 有应答就算在跑 ----
set "ALIVE=0"
curl -s --noproxy "*" --max-time 2 "http://127.0.0.1:%PORT%/healthz" >nul 2>nul
if not errorlevel 1 set "ALIVE=1"

if /i "%MODE%"=="status" goto :show
if /i "%MODE%"=="tui" goto :tui
if /i "%MODE%"=="stop" goto :stop

if "%ALIVE%"=="1" (
  if /i "%MODE%"=="ensure" exit /b 0
  goto :show
)

rem ---- 没在跑：拉起来。pythonw 起法没有窗口，node 退路会留一个最小化窗口 ----
rem TUI=0：这个窗口是新控制台（isTTY 为真），不关掉就会跑成按键驱动的 TUI，
rem 手滑按个 q 就把常驻代理按死了。后台实例只要滚动日志，按键一律无效。
set "TUI=0"
if defined PYW (
  rem 走 Python 起：只有它能解出加密的密钥和本地令牌，再通过环境变量交给 node。
  rem pythonw 没有控制台窗口，start 不需要 /min，也不会闪一下黑框。
  start "" "%PYW%" "%~dp0ocg_config.py" start
) else (
  rem 没有 Python 时的退路：代理照跑，但读不到密钥，只能透传客户端自带的钥匙。
  start "OpenCode Go 启动器" /min "%NODE%" "%~dp0opencode-go-proxy.mjs"
)
set "JUST_STARTED=1"
if /i "%MODE%"=="ensure" exit /b 0

rem 等它开始监听（最多 10 秒）
set /a _n=0
:wait
set /a _n+=1
curl -s --noproxy "*" --max-time 1 "http://127.0.0.1:%PORT%/healthz" >nul 2>nul
if not errorlevel 1 goto :show
if %_n% geq 10 goto :failed
ping -n 2 127.0.0.1 >nul
goto :wait

:failed
echo.
echo [错误] 拉起后 10 秒内没监听 %PORT%。看同目录 proxy.log 最后几行。
echo.
pause
exit /b 1

:show
rem 双击只负责"起来"，不再顺手打一屏状态——否则一次双击两个窗口。
if /i "%MODE%"=="auto" (
  title OpenCode Go 已就绪
  if "%JUST_STARTED%"=="1" (
    echo 代理已拉起，正在后台运行。
  ) else (
    echo 代理已经在后台运行。
  )
  echo 看实时状态：系统托盘里点 OpenCode Go 图标，或右键选「打开控制台」。
  echo 本窗口 2 秒后自动关闭；关掉它不影响代理。
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)
title OpenCode Go 状态快照
"%NODE%" "%~dp0opencode-go-proxy.mjs" status
echo.
echo  以上是快照，不会刷新。本窗口 10 秒后自动关闭。
ping -n 11 127.0.0.1 >nul
exit /b 0

:stop
if "%ALIVE%"=="0" (
  echo 端口 %PORT% 上没有实例在跑。
  exit /b 0
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do (
  echo 结束 PID %%p ...
  taskkill /PID %%p /F >nul 2>nul
)
echo 已停。
exit /b 0

:tui
set "PY="
for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY (
  echo.
  echo [错误] 找不到 python.exe，打不开控制台。
  echo.
  pause
  exit /b 1
)
start "" "%PY%" "%~dp0tui.py"
exit /b 0
