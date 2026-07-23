@echo off
chcp 65001 >nul
title StemFlow Python Installer

net session >nul 2>&1
if not "%errorlevel%"=="0" (
    echo 请右键本文件，选择“以管理员身份运行”。
    echo.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-python-windows.ps1"
set "INSTALL_EXIT=%errorlevel%"
echo.
if not "%INSTALL_EXIT%"=="0" (
    echo Python 安装失败，请查看上面的错误信息。
) else (
    echo Python 已准备完成。
)
pause
exit /b %INSTALL_EXIT%
