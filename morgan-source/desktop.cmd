@echo off
rem Standalone desktop launcher for Morgan (pywebview + WebView2)
setlocal
set "ROOT=%~dp0"
set "PYTHONW=%ROOT%src\hermes-lite\.venv\Scripts\pythonw.exe"

rem If pythonw exists and no console requested, launch detached without console window
set "USE_CONSOLE=0"
for %%a in (%*) do (
    if "%%a"=="--console" set "USE_CONSOLE=1"
)

if "%USE_CONSOLE%"=="0" if exist "%PYTHONW%" (
    cd /d "%ROOT%src\hermes-lite"
    start "" "%PYTHONW%" -m hermes_lite.chatdesktop %*
    exit /b 0
)

powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%ROOT%desktop.ps1" %*
exit /b %ERRORLEVEL%
