/** WebSocket 주소의 앞부분(scheme + host)을 만든다.
 *
 * 포트 통합 모드에서는 `NEXT_PUBLIC_API_URL` 이 빈 문자열이라, 그대로 쓰면
 * `new WebSocket("/api/map/ws/...")` 처럼 상대경로가 된다. 상대경로 WebSocket 은
 * 비교적 최근 브라우저에서만 해석되므로, 화면을 준 서버(`location.host`)를
 * 명시적으로 붙여 어느 브라우저에서도 동작하게 한다.
 *
 * 서버 IP 를 코드에 박지 않으므로 **IP 가 바뀌어도 재빌드가 필요 없다.**
 * (분리 모드에서 `NEXT_PUBLIC_API_URL` 이 설정돼 있으면 그 값을 그대로 쓴다)
 */
export function wsOrigin(): string {
  const api = process.env.NEXT_PUBLIC_API_URL;
  if (api) return api.replace(/^http/, "ws");
  // 정적 생성(빌드) 시점에는 window 가 없다. 실제 연결은 클라이언트에서만 일어난다.
  if (typeof window === "undefined") return "";
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}`;
}
