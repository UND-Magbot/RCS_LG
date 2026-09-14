/**
 * 로봇 / 랙 외형 치수 — 맵 편집기에서 R·J 지점 마커를 실제 크기로 그리기 위한 값.
 *
 * ★ BackEnd/app/constants/rack_specs.py 와 같은 값이어야 한다. 한쪽만 고치지 말 것.
 *
 * 마커가 나타내는 것은 "랙을 든 로봇이 그 자리에서 차지하는 크기"다.
 *   좌우 = max(로봇 폭,  랙 폭  + margin×2)
 *   앞뒤 = max(로봇 길이, 랙 깊이 + margin×2)
 * LG 랙은 좌우로 로봇보다 넓고, 앞뒤로는 로봇이 더 길다.
 */

export type RackSpec = { width: number; depth: number; margin: number };

/** 랙 규격 — width/depth 는 라이다가 재는 '다리 중심 간격', margin 은 실물 외형까지의 여유 */
export const RACK_SPECS: Record<string, RackSpec> = {
  S300: { width: 0.765, depth: 0.765, margin: 0.0925 },
  S600: { width: 0.83, depth: 0.87, margin: 0.1 },
  // 실물 최대 폭 870mm 실측(2026-09-04, 캐스터 최대 회전 상태) → margin 0.09
  LG: { width: 0.7, depth: 0.5, margin: 0.09 },
  LG2: { width: 0.64, depth: 0.545, margin: 0.08 },
};

/** 로봇 외형 (m) */
export const ROBOT_FOOTPRINT: Record<string, { width: number; length: number }> = {
  // 2026-09-04 /device/info 실측
  crawler_s300_op5: { width: 0.46, length: 0.748 },
  // L-150 제품 사양표: Dimensions 740 x 500 x 1240 mm → 길이 0.74 / 폭 0.50
  //   (같은 표의 Minimum Passage Width 70cm = 폭 0.50 + 여유 0.20 과 맞아떨어진다)
  //   로봇이 온라인이 되면 GET /device/info → robot.footprint 로 검산할 것.
  longjack: { width: 0.5, length: 0.74 },
};

export const DEFAULT_ROBOT_MODEL = "longjack";
export const DEFAULT_RACK_SIZE = "LG";

/** 랙을 든 상태에서 차지하는 크기 (좌우, 앞뒤) */
export function carriedFootprint(
  rackSize?: string | null,
  robotModel: string = DEFAULT_ROBOT_MODEL
): { width: number; depth: number } {
  const rack = RACK_SPECS[rackSize || DEFAULT_RACK_SIZE] ?? RACK_SPECS[DEFAULT_RACK_SIZE];
  const robot = ROBOT_FOOTPRINT[robotModel] ?? ROBOT_FOOTPRINT[DEFAULT_ROBOT_MODEL];
  return {
    width: Math.max(robot.width, rack.width + rack.margin * 2),
    depth: Math.max(robot.length, rack.depth + rack.margin * 2),
  };
}
