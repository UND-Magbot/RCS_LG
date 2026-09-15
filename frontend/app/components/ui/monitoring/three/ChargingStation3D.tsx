"use client";

import { memo } from "react";
import { RoundedBox } from "@react-three/drei";

/* ── 본체 (흰색 박스) ── */
const BODY_W = 12;
const BODY_D = 5;
const BODY_H = 14;
const BODY_Y = BODY_H / 2;

/* ── 전면 캐비티 (오목부) ── */
const CAVITY_W = 8;
const CAVITY_H = 6;
const CAVITY_DEPTH = 2;
const CAVITY_Y = BODY_Y - 1;

/* ── 충전 접점 바 ── */
const STRIP_W = 6;
const STRIP_H = 0.5;
const STRIP_D = 0.3;
const STRIP_GAP = 1.8;


function ChargingStation3DInner() {
  const cavityZ = BODY_D / 2 - CAVITY_DEPTH / 2 + 0.1;
  const stripZ = BODY_D / 2 - CAVITY_DEPTH + 0.4;

  return (
    <group rotation={[0, Math.PI / 2, 0]}>
      {/* ========== 본체 ========== */}
      <RoundedBox
        args={[BODY_W, BODY_H, BODY_D]}
        radius={0.6}
        smoothness={4}
        position={[0, BODY_Y, 0]}
      >
        <meshStandardMaterial color="#f0f2f5" />
      </RoundedBox>

      {/* ========== 전면 캐비티 (오목부) ========== */}
      <RoundedBox
        args={[CAVITY_W, CAVITY_H, CAVITY_DEPTH]}
        radius={0.3}
        smoothness={2}
        position={[0, CAVITY_Y, cavityZ]}
      >
        <meshStandardMaterial color="#2a3040" />
      </RoundedBox>

      {/* 캐비티 내부 뒷벽 */}
      <mesh position={[0, CAVITY_Y, BODY_D / 2 - CAVITY_DEPTH + 0.2]}>
        <boxGeometry args={[CAVITY_W - 1, CAVITY_H - 1, 0.1]} />
        <meshStandardMaterial color="#3a4555" />
      </mesh>

      {/* 캐비티 상단 경사면 */}
      <mesh
        position={[
          0,
          CAVITY_Y + CAVITY_H / 2 - 0.3,
          cavityZ,
        ]}
        rotation={[0.3, 0, 0]}
      >
        <boxGeometry args={[CAVITY_W - 0.2, 0.15, CAVITY_DEPTH - 0.2]} />
        <meshStandardMaterial color="#3a4050" />
      </mesh>

      {/* 캐비티 하단 경사면 */}
      <mesh
        position={[
          0,
          CAVITY_Y - CAVITY_H / 2 + 0.3,
          cavityZ,
        ]}
        rotation={[-0.3, 0, 0]}
      >
        <boxGeometry args={[CAVITY_W - 0.2, 0.15, CAVITY_DEPTH - 0.2]} />
        <meshStandardMaterial color="#3a4050" />
      </mesh>

      {/* ========== 충전 접점 바 (상) ========== */}
      <RoundedBox
        args={[STRIP_W, STRIP_H, STRIP_D]}
        radius={0.1}
        smoothness={2}
        position={[
          0,
          CAVITY_Y - CAVITY_H / 4 + STRIP_GAP / 2,
          stripZ,
        ]}
      >
        <meshStandardMaterial
          color="#c0c0c0"
          metalness={0.8}
          roughness={0.2}
        />
      </RoundedBox>

      {/* ========== 충전 접점 바 (하) ========== */}
      <RoundedBox
        args={[STRIP_W, STRIP_H, STRIP_D]}
        radius={0.1}
        smoothness={2}
        position={[
          0,
          CAVITY_Y - CAVITY_H / 4 - STRIP_GAP / 2,
          stripZ,
        ]}
      >
        <meshStandardMaterial
          color="#c0c0c0"
          metalness={0.8}
          roughness={0.2}
        />
      </RoundedBox>

      {/* ========== 후면 케이블 아웃렛 ========== */}
      <RoundedBox
        args={[2, 1.5, 1]}
        radius={0.2}
        smoothness={2}
        position={[0, BODY_Y - BODY_H / 4, -BODY_D / 2 - 0.5]}
      >
        <meshStandardMaterial color="#444" />
      </RoundedBox>

      {/* 후면 녹색 LED */}
      <mesh position={[0, BODY_Y - BODY_H / 4 + 1, -BODY_D / 2 - 0.3]}>
        <sphereGeometry args={[0.4, 6, 6]} />
        <meshStandardMaterial
          color="#22cc66"
          emissive="#22cc66"
          emissiveIntensity={0.6}
          toneMapped={false}
        />
      </mesh>
    </group>
  );
}

export const ChargingStation3D = memo(ChargingStation3DInner);
