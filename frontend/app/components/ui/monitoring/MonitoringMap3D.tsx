"use client";

import { Suspense, useMemo, useState } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import type { MonitoringMapProps } from "@/lib/types/map-markers";
import { useOccupancyGrid } from "./three/useOccupancyGrid";
import { GridFloor } from "./three/GridFloor";
import { OccupancyWalls } from "./three/OccupancyWalls";
import { MapFloor } from "./three/MapFloor";
import { RobotMarker3D } from "./three/RobotMarker3D";
import { PoiMarker3D } from "./three/PoiMarker3D";
import { WaypointMarker3D } from "./three/WaypointMarker3D";
import { RouteLines3D } from "./three/RouteLines3D";
import { FirewallWall3D } from "./three/FirewallWall3D";
import "./MonitoringMap3D.css";

export function MonitoringMap3D(props: MonitoringMapProps) {
  const {
    mapSrc,
    robots,
    pois,
    waypoints,
    routeWaypoints,
    routeSegments,
  } = props;

  const [contextLost, setContextLost] = useState(false);
  const occupancy = useOccupancyGrid(mapSrc);

  const cameraPosition = useMemo<[number, number, number]>(() => {
    if (!occupancy) return [500, 400, 500];
    const dist = Math.max(occupancy.imageWidth, occupancy.imageHeight) * 1.0;
    return [dist * 0.4, dist * 0.5, dist * 0.4];
  }, [occupancy]);

  if (contextLost) {
    return (
      <div className="monitoring-map3d-wrap" style={{ display: "flex", alignItems: "center", justifyContent: "center", color: "#4a90b8", fontSize: "0.875rem" }}>
        3D 렌더링을 사용할 수 없습니다. (WebGL 컨텍스트 손실)
      </div>
    );
  }

  return (
    <div className="monitoring-map3d-wrap">
      <Canvas
        camera={{ position: cameraPosition, fov: 50, near: 1, far: 10000 }}
        gl={{ antialias: false, alpha: false, powerPreference: "high-performance" }}
        dpr={[1, 1.5]}
        style={{ background: "#080e1a" }}
        onCreated={({ gl }) => {
          gl.domElement.addEventListener("webglcontextlost", (e) => {
            e.preventDefault();
            setContextLost(true);
          });
          gl.domElement.addEventListener("webglcontextrestored", () => {
            setContextLost(false);
          });
        }}
      >
        <ambientLight intensity={1.0} />
        <directionalLight position={[200, 500, 300]} intensity={1.0} />

        <Suspense fallback={null}>
          <GridFloor />

          {/* Map floor texture */}
          {props.showMapBackground && occupancy && (
            <MapFloor
              mapSrc={mapSrc}
              width={occupancy.imageWidth}
              height={occupancy.imageHeight}
            />
          )}

          {/* Extruded occupancy walls */}
          {props.showVirtualWalls && occupancy && (
            <OccupancyWalls data={occupancy} />
          )}

          {/* Navigation routes */}
          {props.showNavigationLine && occupancy && (
            <RouteLines3D
              segments={routeSegments ?? []}
              routeWaypoints={routeWaypoints}
              waypoints={waypoints}
              imgW={occupancy.imageWidth}
              imgH={occupancy.imageHeight}
              showArrows={props.showDirectionArrows}
            />
          )}

          {/* Firewall walls (3D orange wall) */}
          {occupancy && routeSegments && (
            <FirewallWall3D
              segments={routeSegments}
              imgW={occupancy.imageWidth}
              imgH={occupancy.imageHeight}
            />
          )}


          {/* Waypoint markers */}
          {props.showNavigationNodes &&
            occupancy &&
            waypoints.map((wp) => (
              <WaypointMarker3D
                key={wp.id}
                waypoint={wp}
                imgW={occupancy.imageWidth}
                imgH={occupancy.imageHeight}
              />
            ))}

          {/* POI markers */}
          {props.showPoiMarkers &&
            occupancy &&
            pois.map((poi) => (
              <PoiMarker3D
                key={poi.id}
                poi={poi}
                imgW={occupancy.imageWidth}
                imgH={occupancy.imageHeight}
              />
            ))}

          {/* Robot markers (always shown) */}
          {occupancy &&
            robots.map((r) => (
              <RobotMarker3D
                key={r.robotId}
                robot={r}
                imgW={occupancy.imageWidth}
                imgH={occupancy.imageHeight}
              />
            ))}
        </Suspense>

        <OrbitControls
          enableDamping
          dampingFactor={0.1}
          minDistance={100}
          maxDistance={3000}
          maxPolarAngle={Math.PI / 2.1}
          minPolarAngle={0.1}
        />
      </Canvas>
    </div>
  );
}
