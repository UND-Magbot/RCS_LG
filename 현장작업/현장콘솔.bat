@echo off
rem ============================================================
rem  Field console (web UI on port 8777) - scripts\field_console.py
rem  ASCII only on purpose: no Korean, no chcp (see L1_*.bat).
rem ============================================================
title Field console - do not close this window
cd /d "%~dp0.."
set PY=BackEnd\venv\Scripts\python.exe
if not exist "%PY%" set PY=python
if not exist "scripts\field_console.py" goto :missing
"%PY%" scripts\field_console.py
echo.
pause
exit /b 0

:missing
echo  [ERROR] scripts\field_console.py not found.
echo          git checkout FETCH_HEAD -- scripts/field_console.py
pause
exit /b 1
