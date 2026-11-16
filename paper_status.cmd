@echo off
rem 星财富 Paper/Replay - 状态
rem 用法：paper_status.cmd
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_status.ps1" %*
exit /b %ERRORLEVEL%
