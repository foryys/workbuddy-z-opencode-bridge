@echo off
setlocal enabledelayedexpansion
title OpenCode Go Proxy for WorkBuddy
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8787
set "UPSTREAM_BASE=https://opencode.ai/inference/go"
rem 历史厚的会话请求体能到 2-3MB，压缩后上传不容易被丢。上游不认就删掉这一行。
rem 请求发出后多久还没收到响应头就算死（毫秒）。今天成功的长请求最长 252 秒，故设 3 分钟。
set "FIRST_BYTE_TIMEOUT_MS=180000"

rem 上游不接受 content-encoding: gzip（2026-10-22 实测 400）。需要时临时打开这一行。
rem set "GZIP=1"

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo.
  echo [ERROR] Port %PORT% is already in use.
  echo Either close the program holding it, or set PORT=8788 and change the URL in WorkBuddy to match.
  echo.
  pause
  exit /b 1
)

set "NODE="
set "NODEVERS=%USERPROFILE%\.workbuddy\binaries\node\versions"

rem 1) node already on PATH
where node >nul 2>nul
if %errorlevel%==0 set "NODE=node"

rem 2) WorkBuddy bundled node, via the "current" pointer file
if not defined NODE if exist "%NODEVERS%\current" (
  set "CURV="
  set /p CURV=<"%NODEVERS%\current"
  if defined CURV if exist "%NODEVERS%\!CURV!\node.exe" set "NODE=%NODEVERS%\!CURV!\node.exe"
)

rem 3) fallback: newest version folder that actually contains node.exe
if not defined NODE if exist "%NODEVERS%" (
  for /f "delims=" %%d in ('dir /b /ad /o-n "%NODEVERS%" 2^>nul') do (
    if not defined NODE if exist "%NODEVERS%\%%d\node.exe" set "NODE=%NODEVERS%\%%d\node.exe"
  )
)

if not defined NODE (
  echo.
  echo [ERROR] Node.js not found.
  echo Looked in: %NODEVERS%
  echo Install Node 18+ from https://nodejs.org, or set NODE=C:\path\to\node.exe before running.
  echo.
  pause
  exit /b 1
)

echo Using node: %NODE%
echo Starting proxy on http://127.0.0.1:%PORT%
echo Keep this window open while using WorkBuddy.
echo.

"%NODE%" "%~dp0opencode-go-proxy.mjs"

echo.
echo Proxy stopped. Press any key to close.
pause >nul
