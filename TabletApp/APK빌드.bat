@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

REM ─────────────────────────────────────────────────────────────
REM  RCS LG 태블릿 APK 빌드
REM
REM  flavor 3종 — applicationId 가 서로 달라 한 기기에 동시 설치 가능
REM    console : 관리자 콘솔        /api/dispatch/console
REM    robot   : 로봇 부착 태블릿    /api/dispatch/robot-tablet/{로봇ID}
REM    poi     : 위치별 태블릿       /api/dispatch/tablet/poi/{POI ID}   ← 2026-08-24 추가
REM
REM  사용법:  APK빌드.bat            → 3종 전부
REM           APK빌드.bat poi        → poi 만
REM
REM  ⚠ JDK 17 또는 21 이 필요합니다 (2026-08-24 실측 확인)
REM     이 프로젝트는 Gradle 8.7 + AGP 8.3.2 구성입니다.
REM       · JRE 1.8   → AGP 가 Java 11+ 를 요구해 실패
REM       · JDK 25    → Gradle 8.7 이 못 읽음 (Unsupported class file major version 69)
REM     build.gradle 이 이미 Java 17 을 지정하고 있으므로 JDK 17 이 정석입니다.
REM     받는 곳: https://adoptium.net  →  Temurin 17 (LTS)
REM ─────────────────────────────────────────────────────────────

cd /d "%~dp0"

REM ── 1) 쓸 JDK 찾기 ────────────────────────────────────────────
REM 직접 지정하고 싶으면 아래에 경로를 넣으세요 (비우면 자동 탐색)
set "JDK_DIR="

if "%JDK_DIR%"=="" (
    for %%B in (
        "C:\Program Files\Eclipse Adoptium"
        "C:\Program Files\Java"
        "C:\Program Files\Microsoft"
        "C:\Program Files\Amazon Corretto"
        "%USERPROFILE%\.jdks"
    ) do (
        if exist "%%~B" (
            for /d %%D in ("%%~B\*17*") do if exist "%%~D\bin\javac.exe" set "JDK_DIR=%%~D"
            if "!JDK_DIR!"=="" for /d %%D in ("%%~B\*21*") do if exist "%%~D\bin\javac.exe" set "JDK_DIR=%%~D"
        )
    )
)

if "%JDK_DIR%"=="" (
    echo.
    echo ============================================================
    echo  [중단] JDK 17 또는 21 을 찾지 못했습니다.
    echo ============================================================
    echo.
    echo  이 프로젝트는 Gradle 8.7 + AGP 8.3.2 라서
    echo    - Java 8  : 너무 낮아 AGP 가 안 붙습니다
    echo    - Java 25 : 너무 높아 Gradle 이 못 읽습니다
    echo  둘 다 실패합니다. JDK 17 을 설치하세요.
    echo.
    echo    https://adoptium.net  ^-^>  Temurin 17 (LTS) ^-^> Windows x64 .msi
    echo.
    echo  설치 후 이 파일을 다시 실행하면 자동으로 찾습니다.
    echo  (직접 지정하려면 위쪽 JDK_DIR 변수에 경로 입력)
    echo.
    exit /b 1
)

set "JAVA_HOME=%JDK_DIR%"
echo.
echo  사용할 JDK: %JAVA_HOME%
"%JAVA_HOME%\bin\java.exe" -version

REM ── 2) 빌드 ───────────────────────────────────────────────────
set TARGET=%1
if "%TARGET%"=="" set TARGET=all

echo.
echo ============================================
echo   RCS LG 태블릿 APK 빌드  (target: %TARGET%)
echo ============================================
echo.

if /i "%TARGET%"=="all" (
    call gradlew.bat assembleRelease --console=plain
) else if /i "%TARGET%"=="poi" (
    call gradlew.bat assemblePoiRelease --console=plain
) else if /i "%TARGET%"=="console" (
    call gradlew.bat assembleConsoleRelease --console=plain
) else if /i "%TARGET%"=="robot" (
    call gradlew.bat assembleRobotRelease --console=plain
) else (
    echo [오류] 알 수 없는 대상: %TARGET%
    echo        사용 가능: all ^| console ^| robot ^| poi
    exit /b 1
)

if errorlevel 1 (
    echo.
    echo [실패] 빌드 오류. 위 메시지를 확인하세요.
    echo        "Unsupported class file major version" 이면 JDK 버전 문제입니다.
    exit /b 1
)

echo.
echo ============================================
echo   빌드 완료 — 생성된 APK
echo ============================================
for /r "app\build\outputs\apk" %%f in (*.apk) do echo   %%f
echo.
echo  설치:  adb install -r "APK경로"
echo.

endlocal
