@echo off
chcp 65001 >nul
echo.
echo  [3단계] 백엔드와 관제 화면을 끄고 실행하세요.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp03_MTU_링크품질_판별.ps1" %*
pause
