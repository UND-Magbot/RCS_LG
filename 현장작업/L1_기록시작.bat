@echo off
chcp 949 > nul
title [L1] 로그 기록 시작
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
echo  [L1] 로그 기록 시작
echo ============================================================
echo.
"%PY%" scripts\field_session.py start
echo.
echo ------------------------------------------------------------
echo  이 창은 닫아도 됩니다. 기록은 계속됩니다.
echo  새로 열린 검은 창(주행 기록기)은 닫지 마세요.
echo ------------------------------------------------------------
pause
