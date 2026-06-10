@echo off
chcp 65001 >nul
echo Copy channel URL to clipboard, then press any key...
pause >nul
echo.

powershell -ExecutionPolicy Bypass -File "download_channel_subs.ps1"

echo.
pause
