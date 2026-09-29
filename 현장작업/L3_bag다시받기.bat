@echo off
rem ============================================================
rem  L3 - fetch robot bags again
rem  ASCII only on purpose: no Korean, no chcp.
rem  (2026-09-29 the Korean version broke on the field server PC -
rem   cmd mis-read line boundaries under a different codepage.)
rem  All Korean messages are printed by scripts\field_session.py.
rem ============================================================
title L3 - fetch robot bags again
cd /d "%~dp0.."
set PY=BackEnd\venv\Scripts\python.exe
if not exist "%PY%" set PY=python
if not exist "scripts\field_session.py" goto :missing
echo.
"%PY%" scripts\field_session.py bags
echo.
"%PY%" scripts\field_session.py open
pause
exit /b 0

:missing
echo  [ERROR] scripts\field_session.py not found.
echo          git checkout FETCH_HEAD -- scripts/field_session.py
pause
exit /b 1
