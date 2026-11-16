@echo off
rem XingCaiFu Paper/Replay - status
rem Usage: paper_status.cmd
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_status.ps1" %*
exit /b %ERRORLEVEL%
