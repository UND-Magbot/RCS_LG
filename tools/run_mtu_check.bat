@echo off
rem RCS LG - link diagnosis launcher. ASCII only on purpose:
rem cmd.exe mis-parses batch files that contain multibyte text.
setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\BackEnd\venv\Scripts\python.exe"
if exist "%PY%" goto :run
set "PY=python"
:run
"%PY%" "%HERE%mtu_check.py" %*
