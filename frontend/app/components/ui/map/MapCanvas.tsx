"use client";

import {
  useRef,
  useState,
  useEffect,
  useCallback,
  type MouseEvent,
  type WheelEvent,
} from "react";
import type { MapCanvasProps } from "@/lib/types/map";
import {
  offsetPolyline, corridorWalls, snapAngle,
  openWallsAtTargets, WORK_POINT_MAX_DIST_M,
} from "@/lib/geometry";
import { carriedFootprint } from "@/lib/constants/footprint";

export function MapCanvas({
  pois,
  lines,
  polygons,
  activeTool,
  selectedPOI,
  lineStartPOI,
  zoom,
  offset,
  rotation,
  mapImageUrl,
  robotPose,
  mapMeta,
  onCanvasClick,
  onPOIClick,
  onLineClick,
  onPolygonClick,
  onZoomChange,
  onOffsetChange,
  onImageLoad,
  vwTempPoints = [],
  vwOffsetPx = 0,
  vwSide = "left",
  onCanvasDoubleClick,
  vwMode = "corridor",
  vwBase = "wall",
  vwAngleSnap = true,
  vwWidthPx = 0,
  vwAutoGap = false,
  vwGapPx = 0,
}: MapCanvasProps) {
  const svgRef = useRef<SVGSVGElement>(null);
  const isPanningRef = useRef(false);
  const panStartRef = useRef<{ x: number; y: number } | null>(null);
  const offsetStartRef = useRef({ x: 0, y: 0 });
  const mousePosRef = useRef<{ x: number; y: number } | null>(null);
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null);
  const [vwMousePos, setVwMousePos] = useState<{ x: number; y: number } | null>(null);

  // lineStartPOI 해제 시 mousePos 초기화
  useEffect(() => {
    if (!lineStartPOI) setMousePos(null);
  }, [lineStartPOI]);
  const [processedImg, setProcessedImg] = useState<{
    url: string; w: number; h: number;
  } | null>(null);

  // Load image, remove gray outer area, produce transparent PNG data URL
  useEffect(() => {
    if (!mapImageUrl) { setProcessedImg(null); return; }
    const img = new window.Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      const c = document.createElement("canvas");
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      const ctx = c.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      const imageData = ctx.getImageData(0, 0, c.width, c.height);
      const d = imageData.data;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i + 1], b = d[i + 2];
        // Gray pixels: R≈G≈B and in mid-gray range (100~180)
        const avg = (r + g + b) / 3;
        const spread = Math.max(Math.abs(r - avg), Math.abs(g - avg), Math.abs(b - avg));
        if (spread < 15 && avg > 100 && avg < 180) {
          d[i + 3] = 0; // make transparent
        }
      }
      ctx.putImageData(imageData, 0, 0);
      setProcessedImg({
        url: c.toDataURL("image/png"),
        w: img.naturalWidth,
        h: img.naturalHeight,
      });
      onImageLoad?.(img.naturalWidth, img.naturalHeight);
    };
    img.onerror = () => {
      console.error("[맵 이미지 로드 실패]", mapImageUrl);
      setProcessedImg(null);
    };
    img.src = mapImageUrl;
  }, [mapImageUrl]);

  const clamp = (val: number, min: number, max: number) =>
    Math.min(max, Math.max(min, val));

  const screenToCanvas = useCallback(
    (clientX: number, clientY: number) => {
      const svg = svgRef.current;
      if (!svg) return { x: 0, y: 0 };
      const rect = svg.getBoundingClientRect();
      return {
        x: (clientX - rect.left - offset.x) / zoom,
        y: (clientY - rect.top - offset.y) / zoom,
      };
    },
    [zoom, offset]
  );

  const handleWheel = (e: WheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    if (e.deltaY === 0) return;

    const svg = svgRef.current;
    if (!svg) return;

    const rect = svg.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    const nextZoom = clamp(zoom * factor, 0.2, 6);
    const ratio = nextZoom / zoom;

    const nextOffsetX = mouseX - (mouseX - offset.x) * ratio;
    const nextOffsetY = mouseY - (mouseY - offset.y) * ratio;

    onZoomChange(nextZoom);
    onOffsetChange({ x: nextOffsetX, y: nextOffsetY });
  };

  const handleMouseDown = (e: MouseEvent<SVGSVGElement>) => {
    if (e.button === 1 || (e.button === 0 && activeTool === "select")) {
      isPanningRef.current = true;
      panStartRef.current = { x: e.clientX, y: e.clientY };
      offsetStartRef.current = { ...offset };
      e.preventDefault();
    }
  };

  const handleMouseMove = (e: MouseEvent<SVGSVGElement>) => {
    const pos = screenToCanvas(e.clientX, e.clientY);
    mousePosRef.current = pos;

    // 가상벽 점 찍는 중이면 마우스 위치 갱신 (프리뷰용)
    if (activeTool === "virtualwall" && vwTempPoints.length > 0) {
      setVwMousePos(pos);
    }

    // 라인 그리기 중이면 마우스 위치를 state에 갱신 (임시 라인 렌더용)
    if (lineStartPOI && (activeTool === "line" || activeTool === "curveLine")) {
      setMousePos(pos);
    }

    if (isPanningRef.current && panStartRef.current) {
      // 좌클릭(1) 또는 휠클릭(4)이 눌린 상태에서만 패닝
      if (!(e.buttons & 1) && !(e.buttons & 4)) {
        isPanningRef.current = false;
        panStartRef.current = null;
        return;
      }
      const dx = e.clientX - panStartRef.current.x;
      const dy = e.clientY - panStartRef.current.y;
      onOffsetChange({
        x: offsetStartRef.current.x + dx,
        y: offsetStartRef.current.y + dy,
      });
      e.preventDefault();
    }
  };

  const handleMouseUp = (e: MouseEvent<SVGSVGElement>) => {
    if (isPanningRef.current) {
      const wasDragging =
        panStartRef.current &&
        (Math.abs(e.clientX - panStartRef.current.x) > 3 ||
          Math.abs(e.clientY - panStartRef.current.y) > 3);

      isPanningRef.current = false;
      panStartRef.current = null;

      if (wasDragging) return;
    }

    // POI나 라인 위에서 mouseUp → onClick 핸들러가 처리하므로 캔버스 클릭 무시
    const target = e.target as SVGElement;
    if (target.closest?.(".map-poi") || target.closest?.(".map-line")) return;

    if (
      activeTool === "point" ||
      activeTool === "jackPoint" ||
      activeTool === "line" ||
      activeTool === "curveLine" ||
      activeTool === "polygon" ||
      activeTool === "firewall" ||
      activeTool === "virtualwall"
    ) {
      const pos = screenToCanvas(e.clientX, e.clientY);
      onCanvasClick(pos.x, pos.y);
    }
  };

  const cursorClass = (() => {
    if (isPanningRef.current) return "map-canvas-area__svg--panning";
    switch (activeTool) {
      case "point":
      case "jackPoint":
        return "map-canvas-area__svg--point";
      case "line":
      case "curveLine":
        return "map-canvas-area__svg--line";
      case "del":
        return "map-canvas-area__svg--del";
      case "select":
        return "map-canvas-area__svg--pan";
      default:
        return "";
    }
  })();

  const renderArrow = (
    fromX: number,
    fromY: number,
    toX: number,
    toY: number,
    lineId: string,
    suffix: string,
    isSelected: boolean
  ) => {
    const dx = toX - fromX;
    const dy = toY - fromY;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len === 0) return null;
    const nx = dx / len;
    const ny = dy / len;
    const midX = (fromX + toX) / 2;
    const midY = (fromY + toY) / 2;
    const arrowSize = 4;
    const p1x = midX + nx * arrowSize;
    const p1y = midY + ny * arrowSize;
    const p2x = midX - nx * arrowSize * 0.4 - ny * arrowSize * 0.5;
    const p2y = midY - ny * arrowSize * 0.4 + nx * arrowSize * 0.5;
    const p3x = midX - nx * arrowSize * 0.4 + ny * arrowSize * 0.5;
    const p3y = midY - ny * arrowSize * 0.4 - nx * arrowSize * 0.5;

    return (
      <polygon
        key={`arrow-${lineId}-${suffix}`}
        points={`${p1x},${p1y} ${p2x},${p2y} ${p3x},${p3y}`}
        className={isSelected ? "map-line__arrow map-line__arrow--selected" : "map-line__arrow"}
      />
    );
  };

  return (
    <div className="map-canvas-area">
      <svg
        ref={svgRef}
        className={`map-canvas-area__svg ${cursorClass}`}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={handleMouseUp}
        onDoubleClick={() => onCanvasDoubleClick?.()}
        onContextMenu={(e) => e.preventDefault()}
      >
        <g transform={`translate(${offset.x}, ${offset.y}) scale(${zoom}) rotate(${rotation})`}>
          {/* Background map image (gray removed) */}
          {processedImg && (
            <image
              href={processedImg.url}
              x={-processedImg.w / 2}
              y={-processedImg.h / 2}
              width={processedImg.w}
              height={processedImg.h}
              className="map-canvas-area__bg-image"
            />
          )}

          {/* Polygons (virtual walls) */}
          {polygons.map((poly) => (
            <polygon
              key={poly.id}
              points={poly.points.map((p) => `${p.x},${p.y}`).join(" ")}
              className={poly.shapeType === "firewall" ? "map-polygon__firewall" : "map-polygon__shape"}
              onClick={(e) => {
                e.stopPropagation();
                onPolygonClick(poly.id);
              }}
            />
          ))}

          {/* 가상벽 그리기 프리뷰 — 클릭한 선(회색 점선)과 실제 생성될 선(빨강)을 같이 보여준다 */}
          {vwTempPoints.length > 0 && (() => {
            const mp = vwMousePos
              ? (vwAngleSnap ? snapAngle(vwTempPoints, vwMousePos) : vwMousePos)
              : null;
            const clicked = mp ? [...vwTempPoints, mp] : vwTempPoints;
            const walls =
              vwMode === "corridor" && clicked.length >= 2
                ? corridorWalls(clicked, vwOffsetPx, vwWidthPx, vwSide, vwBase)
                : [offsetPolyline(clicked, vwSide === "left" ? vwOffsetPx : -vwOffsetPx)];
            const result = walls[0];
            // 작업지점 출입구까지 반영해서 보여준다 — 프리뷰가 실제 저장될 모양과 같아야 한다
            const resM = mapMeta?.grid_resolution || 0.05;
            const shownWalls =
              vwAutoGap && vwGapPx > 0
                ? openWallsAtTargets(
                    walls,
                    pois
                      .filter((p) => p.type === "jack" || p.type === "standby")
                      .map((p) => ({ x: p.x, y: p.y })),
                    vwGapPx,
                    WORK_POINT_MAX_DIST_M / resM
                  )
                : walls;
            return (
              <g>
                {/* 클릭한 경로 — 오프셋이 있을 때만 따로 표시 */}
                {Math.abs(vwOffsetPx) > 0.01 && (
                  <polyline
                    points={clicked.map((p) => `${p.x},${p.y}`).join(" ")}
                    fill="none" stroke="#9aa4b2" strokeWidth={1 / zoom}
                    strokeDasharray={`${3 / zoom},${3 / zoom}`}
                  />
                )}
                {/* 실제로 만들어질 선(들) — 출입구로 끊긴 조각까지 그대로 */}
                {shownWalls.map((wl, wi) => (
                  <polyline key={`w${wi}`}
                    points={wl.map((p) => `${p.x},${p.y}`).join(" ")}
                    className="map-polygon__firewall--preview"
                    fill="none"
                  />
                ))}
                {/* 찍은 점 표시 */}
                {vwTempPoints.map((p, i) => (
                  <circle key={i} cx={p.x} cy={p.y} r={4 / zoom}
                    fill="#ff3c3c" stroke="#fff" strokeWidth={1 / zoom} />
                ))}

                {/* 치수 — 구간별 길이(가운데)와 진행 중 구간/합계(마우스 옆) */}
                {(() => {
                  const res = mapMeta?.grid_resolution || 0.05;
                  const fs = 11 / zoom;
                  const segs = [];
                  for (let i = 0; i < result.length - 1; i++) {
                    const a = result[i], b = result[i + 1];
                    const len = Math.hypot(b.x - a.x, b.y - a.y) * res;
                    if (len < 0.25) continue;            // 너무 짧으면 글자가 겹친다
                    segs.push(
                      <text key={`seg${i}`}
                        x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 4 / zoom}
                        fontSize={fs} fill="#ffd36d" stroke="#000"
                        strokeWidth={2.5 / zoom} paintOrder="stroke"
                        textAnchor="middle" style={{ pointerEvents: "none" }}>
                        {len.toFixed(2)} m
                      </text>
                    );
                  }
                  let total = 0;
                  for (let i = 0; i < result.length - 1; i++) {
                    total += Math.hypot(result[i + 1].x - result[i].x,
                                        result[i + 1].y - result[i].y) * res;
                  }
                  return (
                    <g>
                      {segs}
                      {mp && (
                        <text x={mp.x + 10 / zoom} y={mp.y - 10 / zoom}
                          fontSize={fs} fill="#fff" stroke="#000"
                          strokeWidth={2.5 / zoom} paintOrder="stroke"
                          style={{ pointerEvents: "none" }}>
                          {`합계 ${total.toFixed(2)} m`}
                          {vwMode === "corridor" ? `  · 통로 ${(vwWidthPx * res).toFixed(2)} m` : ""}
                          {vwBase === "wall" && Math.abs(vwOffsetPx) > 0.01
                            ? `  · 벽에서 ${(Math.abs(vwOffsetPx) * res).toFixed(2)} m` : ""}
                        </text>
                      )}
                    </g>
                  );
                })()}
              </g>
            );
          })()}

          {/* Lines */}
          {lines.map((line) => {
            const from = pois.find((p) => p.id === line.fromId);
            const to = pois.find((p) => p.id === line.toId);
            if (!from || !to) return null;

            const isSelected = false;

            return (
              <g
                key={line.id}
                className="map-line"
                onClick={(e) => {
                  e.stopPropagation();
                  onLineClick(line.id);
                }}
              >
                <line
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  className={
                    line.lineType === "firewall"
                      ? "map-line__path--firewall"
                      : isSelected
                      ? "map-line__path map-line__path--selected"
                      : "map-line__path"
                  }
                />
                {/* Click target (wider invisible line) */}
                <line
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  stroke="transparent"
                  strokeWidth={12}
                />
                {/* Direction arrows — 방화벽 라인은 화살표 없음 */}
                {line.lineType !== "firewall" && (line.direction === "forward" ||
                  line.direction === "bidirectional") &&
                  renderArrow(from.x, from.y, to.x, to.y, line.id, "fwd", isSelected)}
                {line.lineType !== "firewall" && (line.direction === "backward" ||
                  line.direction === "bidirectional") &&
                  renderArrow(to.x, to.y, from.x, from.y, line.id, "bwd", isSelected)}
              </g>
            );
          })}

          {/* Temporary line while drawing */}
          {lineStartPOI && (activeTool === "line" || activeTool === "curveLine" || activeTool === "firewall") && mousePos && (() => {
            const startPoi = pois.find((p) => p.id === lineStartPOI);
            if (!startPoi) return null;
            return (
              <>
                <line
                  x1={startPoi.x}
                  y1={startPoi.y}
                  x2={mousePos.x}
                  y2={mousePos.y}
                  className="map-temp-line"
                />
                {/* Snap indicator at endpoint */}
                <circle
                  cx={mousePos.x}
                  cy={mousePos.y}
                  r={3}
                  className="map-snap-indicator"
                />
              </>
            );
          })()}

          {/* POI Markers */}
          {pois.map((poi) => {
            const isSelected = selectedPOI === poi.id;
            const isLineStart = lineStartPOI === poi.id;
            const circleClass = [
              "map-poi__circle",
              `map-poi__circle--${poi.type}`,
              isSelected || isLineStart ? "map-poi__circle--selected" : "",
            ]
              .filter(Boolean)
              .join(" ");

            return (
              <g
                key={poi.id}
                className="map-poi"
                onClick={(e) => {
                  e.stopPropagation();
                  onPOIClick(poi.id);
                }}
              >
                {poi.type === "firewall" ? (
                  <rect
                    x={poi.x - 3}
                    y={poi.y - 3}
                    width={6}
                    height={6}
                    transform={`rotate(45, ${poi.x}, ${poi.y})`}
                    className={circleClass}
                    strokeWidth={1}
                  />
                ) : poi.type === "jack" || poi.type === "standby" ? (
                  (() => {
                    const res = mapMeta?.grid_resolution || 0.05;
                    // 랙을 든 로봇이 그 자리에서 차지하는 크기 (POI 의 rackSize 기준)
                    const fpm = carriedFootprint(poi.rackSize);
                    const rackW = fpm.width / res;
                    const rackD = fpm.depth / res;
                    const isStandby = poi.type === "standby";
                    const angle = poi.angle != null ? -poi.angle * (180 / Math.PI) + 90 : 0;
                    return (
                      <g transform={`translate(${poi.x}, ${poi.y}) rotate(${angle})`}>
                        <rect
                          x={-rackW / 2}
                          y={-rackD / 2}
                          width={rackW}
                          height={rackD}
                          fill={isStandby ? "rgba(46, 204, 113, 0.22)" : "rgba(155, 89, 182, 0.25)"}
                          stroke={isSelected || isLineStart ? "#fff" : (isStandby ? "#2ecc71" : "#9b59b6")}
                          strokeWidth={isSelected || isLineStart ? 1 : 0.6}
                          rx={0.5}
                        />
                        {/* V자 쉐브론 방향 표시 */}
                        <polyline
                          points={`${-rackW * 0.3},${-rackD * 0.05} 0,${-rackD * 0.25} ${rackW * 0.3},${-rackD * 0.05}`}
                          fill="none"
                          stroke="rgba(255,255,255,0.6)"
                          strokeWidth={0.5}
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                        <polyline
                          points={`${-rackW * 0.3},${rackD * 0.15} 0,${-rackD * 0.05} ${rackW * 0.3},${rackD * 0.15}`}
                          fill="none"
                          stroke="rgba(255,255,255,0.6)"
                          strokeWidth={0.5}
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </g>
                    );
                  })()
                ) : (
                  <circle
                    cx={poi.x}
                    cy={poi.y}
                    r={3}
                    className={circleClass}
                    strokeWidth={1}
                  />
                )}
                <text
                  x={poi.x}
                  y={poi.y - (poi.type === "jack" || poi.type === "standby"
                    ? carriedFootprint(poi.rackSize).depth / (mapMeta?.grid_resolution || 0.05) / 2 + 3
                    : 6)}
                  className="map-poi__label"
                >
                  {poi.name}
                </text>
              </g>
            );
          })}

          {/* Robot position indicator (red arrow) */}
          {robotPose && mapMeta && processedImg && mapMeta.grid_resolution > 0 && (() => {
            const imgW = processedImg.w;
            const imgH = processedImg.h;
            const ipx = (robotPose.pos[0] - mapMeta.grid_origin_x) / mapMeta.grid_resolution;
            const ipy = imgH - (robotPose.pos[1] - mapMeta.grid_origin_y) / mapMeta.grid_resolution;
            const cx = ipx - imgW / 2;
            const cy = ipy - imgH / 2;
            const ori = -robotPose.ori; // canvas Y is flipped


            const sz = 3;

            return (
              <g
                className="robot-indicator"
                transform={`translate(${cx}, ${cy}) rotate(${(ori * 180) / Math.PI})`}
              >
                <polygon
                  points={`${sz * 1.5},0 ${-sz * 0.8},${-sz * 0.8} ${-sz * 0.8},${sz * 0.8}`}
                  className="robot-indicator__arrow"
                />
              </g>
            );
          })()}
        </g>
      </svg>
    </div>
  );
}
