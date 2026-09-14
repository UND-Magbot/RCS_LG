<#
  RCS LG - 서버 PC 쪽 MTU 조회 / 변경 / 되돌리기 (2026-08-28)

  왜 서버 PC 에서 바꾸는 게 의미가 있나
    우리가 못 받고 있는 건 "로봇 -> 서버" 방향의 큰 응답입니다.
    TCP 는 연결할 때 서로 "내가 받을 수 있는 최대 조각 크기(MSS)"를 알려주는데,
    그 값이 내 인터페이스 MTU 에서 나옵니다.
    즉 서버 PC 의 MTU 를 낮추면 로봇이 보내는 조각도 그만큼 작아집니다.
    -> 라우터 관리자 접속 없이도 "큰 패킷이 문제인가"를 바로 시험할 수 있습니다.

    다만 이건 시험용입니다. 효과가 있으면 라우터 쪽 MTU 도 같이 낮추는 것이 본선입니다.

  사용법  (변경/되돌리기는 관리자 권한 PowerShell 필요)
      .\set_mtu.ps1                                 현재 MTU 보기
      .\set_mtu.ps1 -Mtu 1400                       활성 어댑터 MTU 를 1400 으로
      .\set_mtu.ps1 -Mtu 1400 -Interface "이더넷"    특정 어댑터 지정
      .\set_mtu.ps1 -Revert                         1500 으로 되돌리기

  바꾼 뒤에는 반드시 재측정하세요.
      python mtu_check.py
  처리량(B/s)이 뛰면 큰 패킷이 원인이었던 것이고, 그대로면 회선 속도 문제입니다.
#>
param(
    [int]$Mtu = 0,
    [string]$Interface = "",
    [switch]$Revert
)

function Show-Mtu {
    Write-Host ""
    Write-Host "===== 현재 인터페이스별 MTU =====" -ForegroundColor Cyan
    Get-NetIPInterface -AddressFamily IPv4 |
        Where-Object { $_.ConnectionState -eq "Connected" -and $_.InterfaceAlias -notlike "*Loopback*" } |
        Format-Table InterfaceAlias, NlMtu, ConnectionState -AutoSize
    Write-Host "  기본값은 1500 입니다. 1400 / 1360 순으로 낮춰가며 시험합니다." -ForegroundColor DarkGray
}

function Get-ActiveAlias {
    $c = Get-NetIPInterface -AddressFamily IPv4 |
         Where-Object { $_.ConnectionState -eq "Connected" -and $_.InterfaceAlias -notlike "*Loopback*" }
    if (@($c).Count -eq 1) { return $c.InterfaceAlias }
    return $null
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

Show-Mtu

if ($Revert) { $Mtu = 1500 }
if ($Mtu -le 0) {
    Write-Host ""
    Write-Host "  바꾸려면:  .\set_mtu.ps1 -Mtu 1400      (관리자 PowerShell)" -ForegroundColor Yellow
    Write-Host "  되돌리려면: .\set_mtu.ps1 -Revert" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "엔터를 누르면 닫습니다"
    exit 0
}

if ($Mtu -lt 576 -or $Mtu -gt 1500) {
    Write-Host "  [중단] MTU 는 576 ~ 1500 사이여야 합니다." -ForegroundColor Red
    Read-Host "엔터"
    exit 1
}

if (-not (Test-Admin)) {
    Write-Host ""
    Write-Host "  [중단] 관리자 권한이 필요합니다." -ForegroundColor Red
    Write-Host "         시작 -> PowerShell 우클릭 -> '관리자 권한으로 실행' 후 다시 실행하세요." -ForegroundColor Yellow
    Read-Host "엔터"
    exit 1
}

if (-not $Interface) {
    $Interface = Get-ActiveAlias
    if (-not $Interface) {
        Write-Host ""
        Write-Host "  [중단] 활성 어댑터가 하나가 아닙니다. -Interface 로 지정하세요." -ForegroundColor Red
        Write-Host '         예)  .\set_mtu.ps1 -Mtu 1400 -Interface "이더넷"' -ForegroundColor Yellow
        Read-Host "엔터"
        exit 1
    }
}

$before = (Get-NetIPInterface -AddressFamily IPv4 -InterfaceAlias $Interface -ErrorAction SilentlyContinue).NlMtu
Write-Host ""
Write-Host ("  '{0}' 의 MTU 를 {1} -> {2} 로 변경합니다." -f $Interface, $before, $Mtu) -ForegroundColor Cyan

try {
    netsh interface ipv4 set subinterface "$Interface" mtu=$Mtu store=persistent | Out-Null
} catch {
    Write-Host ("  [실패] " + $_.Exception.Message) -ForegroundColor Red
    Read-Host "엔터"
    exit 1
}

Start-Sleep -Seconds 1
$after = (Get-NetIPInterface -AddressFamily IPv4 -InterfaceAlias $Interface -ErrorAction SilentlyContinue).NlMtu
if ($after -eq $Mtu) {
    Write-Host ("  [OK] 적용됨 - 현재 MTU = {0}" -f $after) -ForegroundColor Green
} else {
    Write-Host ("  [주의] 적용 후 값이 {0} 입니다. 어댑터를 껐다 켜거나 재부팅이 필요할 수 있습니다." -f $after) -ForegroundColor Yellow
}

Show-Mtu
Write-Host ""
Write-Host "  다음: 같은 폴더에서 재측정하고 처리량(B/s)을 비교하세요." -ForegroundColor Cyan
Write-Host "        python mtu_check.py" -ForegroundColor Cyan
Write-Host "  되돌리기: .\set_mtu.ps1 -Revert" -ForegroundColor DarkGray
Write-Host ""
Read-Host "엔터를 누르면 닫습니다"
