# 통계 페이지 — 보류 (2026-08-13)

집계가 맞지 않아 **2026-08-17 LG 설치 대상에서 제외**했습니다. 차후 개발 재개 예정.

이 폴더는 **빌드에 포함되지 않습니다.** `frontend/tsconfig.json` 의 `include` 가
`**/*.tsx` 라서 `frontend/` 안에 두면 타입 검사에 걸리므로, 프로젝트 루트로 빼두었습니다.

## 무엇을 뺐나

| 항목 | 조치 |
|---|---|
| `frontend/app/stats/page.tsx` | 이 폴더의 `stats/page.tsx` 로 **이동** (삭제 아님) |
| `frontend/app/components/shell/SideNav.tsx` | `defaultNavItems` 에서 "통계" 메뉴 줄 제거 |

**건드리지 않은 것** (되돌릴 때 그대로 쓸 수 있음)
- 백엔드 `/api/tasks/stats/*` 엔드포인트 — 그대로 살아 있음
- `frontend/app/globals.css` 의 `.stats-page` / `.stats-card` 스타일 — 그대로 둠
- `package.json` 의 `recharts` 의존성 — 그대로 둠 (쓰는 곳이 없어 번들에는 안 들어감)
- `frontend/public/icon/stats.svg` — 그대로 둠

## 되돌리는 법

```powershell
cd C:\Users\Administrator\Desktop\RCS_LGIT
Move-Item 보류_통계페이지\stats frontend\app\stats
```

그리고 `SideNav.tsx` 의 `defaultNavItems` 에 아래 줄을 "로봇관리"와 "로그관리" 사이에 다시 넣습니다.

```ts
{ label: "통계", href: "/stats", match: "/stats", icon: "/icon/stats.svg" },
```

되돌린 뒤에는 재빌드 + 배포가 필요합니다.

```powershell
cd frontend
$env:BUILD_STATIC = "1"; npm run build
# out\ 을 BackEnd\web\ 으로 복사
```
