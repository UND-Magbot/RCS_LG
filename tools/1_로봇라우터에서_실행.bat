@echo off
chcp 65001 >nul
echo.
echo  [1단계] 노트북을 로봇 라우터 WiFi (TC700_5170086) 에 붙인 상태에서 실행하세요.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0field_diag.ps1" -Mode Robot
pause
