@echo off
rem Wrapper: the machine's PowerShell execution policy is Restricted, so this
rem launches the matching .ps1 with a per-process bypass. Real logic is in the .ps1.
setlocal
powershell.exe -NoProfile -NoLogo -ExecutionPolicy Bypass -File "%~dp0status.ps1" %*
exit /b %ERRORLEVEL%
