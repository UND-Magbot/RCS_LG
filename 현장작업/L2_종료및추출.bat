@echo off
chcp 949 > nul
title [L2] 로그 기록 종료 + 추출
cd /d "%~dp0.."
set PY=BackEndenv\Scripts\python.exe
if not exist "%PY%" set PY=python
if not exist "scripts\field_session.py" (
  echo  [오류] scripts\field_session.py 가 없습니다. 0_개발분받기.bat 으로 받으세요.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  [L2] 로그 기록 종료 + 추출
echo ============================================================
echo.
"%PY%" scripts\field_session.py stop
echo.
if exist "로그추출" start "" explorer "%CD%\로그추출"
pause
