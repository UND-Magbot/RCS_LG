@echo off
chcp 65001 >nul
echo.
echo  [2단계] 노트북을 서버 라우터에 유선 연결하고, WiFi 를 끈 상태에서 실행하세요.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0field_diag.ps1" -Mode Server
pause
