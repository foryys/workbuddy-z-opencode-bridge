@echo off
setlocal enabledelayedexpansion
title OpenCode Go Proxy for WorkBuddy
cd /d "%~dp0"

if "%PORT%"=="" set PORT=8787
set "UPSTREAM_BASE=https://opencode.ai/inference/go"

set "NODE="
where node >nul 2>nul
if %errorlevel%==0 set "NODE=node"

if not defined NODE (
  for /f "delims=" %%i in ('dir /b /s /o-n "%USERPROFILE%\.workbuddy\binaries\node\versions\*\node.exe" 2^>nul') do (
    if not defined NODE set "NODE=%%i"
  )
)

if not defined NODE (
  echo.
  echo [ERROR] Node.js not found.
  echo Install Node 18+ from https://nodejs.org or run WorkBuddy once so it downloads its bundled node.
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
