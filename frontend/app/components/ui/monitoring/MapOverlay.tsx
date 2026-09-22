import type { CSSProperties } from "react";
import type {
  PoiMarkerData,
  WaypointMarkerData,
  RobotMarkerData,
  VirtualWallData,
} from "@/lib/types/map-markers";
import { PoiMarker } from "./PoiMarker";
import { WaypointMarker } from "./WaypointMarker";
import { RobotMarker } from "./RobotMarker";
import "./MapOverlay.css";

type Props = {
  pois: PoiMarkerData[];
  waypoints: WaypointMarkerData[];
  routeWaypoints?: WaypointMarkerData[];
  robots: RobotMarkerData[];
  showPois: boolean;
  showRoutes: boolean;
  showWaypoints: boolean;
  showDirectionArrows: boolean;
  showVirtualWalls: boolean;
  virtualWalls: VirtualWallData[];
  imageWidth: number;
  imageHeight: number;
  mapScale: number;
};

// 시작점과 끝점이 가까우면 루프(양방향) 경로로 판단
function isLoopRoute(points: WaypointMarkerData[]): boolean {
  if (points.length < 2) return false;
  const first = points[0].position;
  const last = points[points.length - 1].position;
  const dist = Math.sqrt((first.x - last.x) ** 2 + (first.y - last.y) ** 2);
  return dist < 5;
}

function SegmentArrows({
  points,
  color,
  keyPrefix,
}: {
  points: WaypointMarkerData[];
  color: string;
  keyPrefix: string;
}) {
  const bidirectional = isLoopRoute(points);

  return (
    <>
      {points.slice(0, -1).map((wp, i) => {
        const a = wp.position;
        const b = points[i + 1].position;
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        const angleDeg = Math.atan2(b.y - a.y, b.x - a.x) * (180 / Math.PI);

        if (bidirectional) {
          // 양방향: → (앞) + ← (뒤) 를 중간점 기준 좌우 오프셋으로 배치
          return (
            <g key={`${keyPrefix}-${i}`} aria-hidden="true">
              <path
                d="M -5,0 L 3,0 M 3,-4 L 8,0 L 3,4"
                stroke={color}
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                fill="none"
                transform={`translate(${mx + 7},${my}) rotate(${angleDeg})`}
              />
              <path
                d="M -5,0 L 3,0 M 3,-4 L 8,0 L 3,4"
                stroke={color}
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                fill="none"
                transform={`translate(${mx - 7},${my}) rotate(${angleDeg + 180})`}
              />
            </g>
          );
        }

        // 단방향: → 하나만
        return (
          <path
            key={`${keyPrefix}-${i}`}
            d="M -5,0 L 3,0 M 3,-4 L 8,0 L 3,4"
            stroke={color}
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
            transform={`translate(${mx},${my}) rotate(${angleDeg})`}
            aria-hidden="true"
          />
        );
      })}
    </>
  );
}

export function MapOverlay({
  pois,
  waypoints,
  routeWaypoints,
  robots,
  showPois,
  showRoutes,
  showWaypoints,
  showDirectionArrows,
  showVirtualWalls,
  virtualWalls,
  imageWidth,
  imageHeight,
  mapScale,
}: Props) {
  const toLeftPercent = (x: number) => (x / imageWidth) * 100;
  const toTopPercent = (y: number) => (y / imageHeight) * 100;
  const toSvgPoints = (points: WaypointMarkerData[]) =>
    points.map((point) => `${point.position.x},${point.position.y}`).join(" ");

  const routeSource = routeWaypoints ?? waypoints;
  const staticWaypoints = routeSource.filter((point) => !point.id.startsWith("alloc-"));
  const allocatedWaypoints = routeSource.filter((point) => point.id.startsWith("alloc-"));
  const allocatedRouteByRobot = allocatedWaypoints.reduce<Record<string, WaypointMarkerData[]>>(
    (acc, waypoint) => {
      const key = waypoint.id.split("-")[1] ?? "robot";
      if (!acc[key]) {
        acc[key] = [];
      }
      acc[key].push(waypoint);
      return acc;
    },
    {}
  );

  const style: CSSProperties = {
    "--marker-counter-scale": 1 / mapScale,
  } as CSSProperties;

  const showSvgLayer = showRoutes || showVirtualWalls;

  return (
    <div className="map-overlay" style={style}>
      {showSvgLayer && (
        <svg
          className="map-overlay__routes"
          viewBox={`0 0 ${imageWidth} ${imageHeight}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          {showRoutes && staticWaypoints.length > 1 && (
            <polyline
              className="map-route map-route--main"
              points={toSvgPoints(staticWaypoints)}
            />
          )}
          {showRoutes && showDirectionArrows && staticWaypoints.length > 1 && (
            <SegmentArrows
              points={staticWaypoints}
              color="rgba(25, 188, 126, 0.9)"
              keyPrefix="main"
            />
          )}

          {showRoutes &&
            Object.entries(allocatedRouteByRobot).map(([robotId, robotRoute]) =>
              robotRoute.length > 1 ? (
                <polyline
                  key={robotId}
                  className="map-route map-route--allocated"
                  points={toSvgPoints(robotRoute)}
                />
              ) : null
            )}
          {showRoutes && showDirectionArrows &&
            Object.entries(allocatedRouteByRobot).map(([robotId, robotRoute]) =>
              robotRoute.length > 1 ? (
                <SegmentArrows
                  key={`arrows-${robotId}`}
                  points={robotRoute}
                  color="rgba(33, 226, 149, 0.75)"
                  keyPrefix={`alloc-${robotId}`}
                />
              ) : null
            )}

          {showVirtualWalls &&
            virtualWalls.map((wall) => (
              <line
                key={wall.id}
                className="map-route map-route--virtual-wall"
                x1={wall.start.x}
                y1={wall.start.y}
                x2={wall.end.x}
                y2={wall.end.y}
              />
            ))}
        </svg>
      )}

      {showWaypoints &&
        waypoints.map((wp) => (
          <WaypointMarker
            key={wp.id}
            data={wp}
            leftPercent={toLeftPercent(wp.position.x)}
            topPercent={toTopPercent(wp.position.y)}
          />
        ))}

      {showPois &&
        pois.map((poi) => (
          <PoiMarker
            key={poi.id}
            data={poi}
            leftPercent={toLeftPercent(poi.position.x)}
            topPercent={toTopPercent(poi.position.y)}
          />
        ))}

      {robots.map((robot) => (
        <RobotMarker
          key={robot.robotId}
          data={robot}
          leftPercent={toLeftPercent(robot.position.x)}
          topPercent={toTopPercent(robot.position.y)}
        />
      ))}
    </div>
  );
}
