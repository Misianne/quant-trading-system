@echo off
rem 星财富 Paper/Replay - 启动
rem 用法：paper_start.cmd [-Minutes 30] [-Replay all]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_start.ps1" %*
exit /b %ERRORLEVEL%
