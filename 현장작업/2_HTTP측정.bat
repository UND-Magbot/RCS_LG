@echo off
chcp 949 > nul
title [2] HTTP 왕복 측정 - ping 이 막혀 있을 때
cd /d "%~dp0.."
call "%~dp0config.bat"

echo.
echo ============================================================
echo  [2] HTTP 왕복 측정
echo ============================================================
echo.
echo  ping 이 응답 없음이었다면 이걸 쓰세요.
echo  실제 통신과 같은 방식(HTTP)으로 잽니다.
echo.
echo  로봇: %ROBOT_IP%
echo.

if not exist "BackEnd\venv\Scripts\python.exe" (
  echo  [오류] BackEnd\venv\Scripts\python.exe 가 없습니다.
  echo         가상환경 경로를 확인하세요.
  pause
  exit /b 1
)

"BackEnd\venv\Scripts\python.exe" tools\mtu_check.py
echo.
echo  결과가 tools 폴더에 저장됩니다.
pause
