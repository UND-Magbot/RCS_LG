"use client";

import { useRef, useEffect, useMemo } from "react";
import * as THREE from "three";
import { Edges } from "@react-three/drei";
import type { OccupancyResult } from "./useOccupancyGrid";

type Props = {
  data: OccupancyResult;
};

export function OccupancyWalls({ data }: Props) {
  const meshRef = useRef<THREE.InstancedMesh>(null);

  const geometry = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const material = useMemo(
    () =>
      new THREE.MeshBasicMaterial({
        color: "#0a2a4a",
        transparent: true,
        opacity: 0.25,
      }),
    []
  );

  const { boxes } = data;
  const count = boxes.length;

  useEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;

    const dummy = new THREE.Object3D();

    for (let i = 0; i < count; i++) {
      const b = boxes[i];
      dummy.position.set(b.x, b.y, b.z);
      dummy.scale.set(b.w, b.h, b.d);
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
    }

    mesh.instanceMatrix.needsUpdate = true;
  }, [boxes, count]);

  if (count === 0) return null;

  return (
    <>
      <instancedMesh
        ref={meshRef}
        args={[geometry, material, count]}
        frustumCulled={false}
      />
      {/* Wireframe edges overlay for the cyber look */}
      <WallEdges data={data} />
    </>
  );
}

/** Render a separate edge-line pass over every merged wall block */
function WallEdges({ data }: Props) {
  const { boxes } = data;

  // Only render edges for a reasonable number of boxes to avoid too many draw calls
  const maxEdgeBoxes = 500;
  const subset = boxes.length > maxEdgeBoxes ? boxes.slice(0, maxEdgeBoxes) : boxes;

  return (
    <>
      {subset.map((b, i) => (
        <mesh key={i} position={[b.x, b.y, b.z]} scale={[b.w, b.h, b.d]}>
          <boxGeometry args={[1, 1, 1]} />
          <meshBasicMaterial visible={false} />
          <Edges color="#00e5ff" threshold={15} />
        </mesh>
      ))}
    </>
  );
}
