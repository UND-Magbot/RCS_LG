@echo off
rem RCS LG - start the local control server on port 8002 (unified mode).
rem
rem Keep this file ASCII-only. cmd.exe mis-seeks in batch files that
rem contain multibyte text, which breaks goto/if blocks. All messages
rem live in start_local.ps1 instead.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_local.ps1"
exit /b %errorlevel%
