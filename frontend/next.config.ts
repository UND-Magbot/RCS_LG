import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // ── 배포용 정적 빌드 ──────────────────────────────────────
  // BUILD_STATIC=1 로 빌드하면 순수 HTML/JS 만 나온다(`out/`). 그걸 백엔드가 서빙하면
  // 서버 PC 에 Node.js 없이 8002 하나로 운영된다.
  // 개발(`npm run dev`)에는 이 설정이 붙지 않으므로 지금 방식 그대로 쓸 수 있다.
  //
  // 전제: 이 프로젝트는 API route / middleware / 서버 컴포넌트 / 동적 라우트가 0개다.
  //       (전부 브라우저에서 돌며 FastAPI 를 호출하는 구조)
  ...(process.env.BUILD_STATIC === "1"
    ? { output: "export" as const, trailingSlash: true }
    : {}),

  // 정적 export 에는 이미지 최적화 서버가 없다.
  // 개발/배포 동작을 어긋나게 두지 않으려고 항상 켠다(아이콘 2곳뿐이라 영향 없음).
  images: { unoptimized: true },
};

export default nextConfig;
