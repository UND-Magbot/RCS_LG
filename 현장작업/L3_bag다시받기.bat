@echo off
chcp 949 > nul
title [L3] 로봇 bag 다시 받기
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
echo  [L3] 마지막 세션의 로봇 bag 다시 받기
echo ============================================================
echo.
echo  L2 때 로봇이 마지막 bag 을 녹화 중이었거나 연결이 안 됐을 때.
echo  받은 것은 건너뛰고 빠진 것만 받은 뒤 zip 을 갱신합니다.
echo.
"%PY%" scripts\field_session.py bags
echo.
if exist "로그추출" start "" explorer "%CD%\로그추출"
pause
