@echo off
chcp 949 > nul
title [6] 로그 모으기 - 분석용 zip
cd /d "%~dp0.."

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
set ZIP=%CD%\로그수집_%TS%.zip

echo.
echo ============================================================
echo  [6] 로그 모으기
echo ============================================================
echo.
echo  모을 것:
echo    _logs\               주행 기록 - 사이클 폴더
echo    BackEnd\_logs\       백엔드 로그
echo.
echo  만들 파일: %ZIP%
echo.

powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $t=Join-Path $env:TEMP ('logpack_'+(Get-Date -Format yyyyMMddHHmmss)); New-Item -ItemType Directory -Path $t -Force | Out-Null; if (Test-Path '_logs') { Copy-Item '_logs' -Destination (Join-Path $t '_logs') -Recurse -Force }; if (Test-Path 'BackEnd\_logs') { Copy-Item 'BackEnd\_logs' -Destination (Join-Path $t 'backend_logs') -Recurse -Force }; Compress-Archive -Path (Join-Path $t '*') -DestinationPath '%ZIP%' -Force; Remove-Item $t -Recurse -Force; Write-Host ''; Write-Host ('만들어졌습니다: %ZIP%')"

echo.
if exist "%ZIP%" (
  powershell -NoProfile -Command "$f=Get-Item '%ZIP%'; Write-Host ('크기: ' + [math]::Round($f.Length/1MB,1) + ' MB')"
) else (
  echo  [오류] zip 이 안 만들어졌습니다. _logs 폴더가 있는지 확인하세요.
)
echo.
pause
