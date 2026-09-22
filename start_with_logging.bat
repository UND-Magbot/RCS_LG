@echo off
rem RCS LG - start the control server AND the drive logger together.
rem
rem Keep this file ASCII-only. cmd.exe mis-seeks in batch files that
rem contain multibyte text, which breaks goto/if blocks. All messages
rem live in start_with_logging.ps1 instead.
rem
rem If anything goes wrong here, just use the old starter again -
rem this file does not modify it in any way.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_with_logging.ps1"
exit /b %errorlevel%
