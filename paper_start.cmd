@echo off
rem XingCaiFu Paper/Replay - start
rem Usage: paper_start.cmd [-Minutes 30] [-Replay all]
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\paper_start.ps1" %*
exit /b %ERRORLEVEL%
