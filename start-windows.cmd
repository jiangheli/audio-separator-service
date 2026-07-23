@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-now.ps1" %*
exit /b %ERRORLEVEL%
