@echo off
rem RCS LG - dev launcher: backend (8002) + frontend next dev (3000),
rem each in its own window so logs stay visible and Ctrl+C stops it.
rem
rem Keep this file ASCII-only. cmd.exe mis-seeks in batch files that
rem contain multibyte text. All messages live in start_dev.ps1 instead.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_dev.ps1" %*
exit /b %errorlevel%
