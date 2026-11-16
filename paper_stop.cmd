@echo off
rem 星财富 Paper/Replay - 停止
rem 用法：paper_stop.cmd [-Force]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_stop.ps1" %*
exit /b %ERRORLEVEL%
