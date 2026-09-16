@echo off
rem RCS LG server launcher (backend 8002 + frontend 3000)
rem Keep this file ASCII-only: cmd.exe mis-seeks in batch files that
rem contain multibyte text, which breaks goto/if blocks. All messages
rem live in start_server.ps1 instead.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_server.ps1"
exit /b %errorlevel%
