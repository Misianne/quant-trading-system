@echo off
rem XingCaiFu Paper/Replay - stop
rem Usage: paper_stop.cmd [-Force]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_stop.ps1" %*
exit /b %ERRORLEVEL%
