import json
import logging
import threading
import time
from typing import Any, Optional

import requests
from websocket import WebSocketException, create_connection

logger = logging.getLogger(__name__)


PORT = 8090
HTTP_TIMEOUT = 15
WS_TIMEOUT = 15

MAP_WS_TOPICS = [
    "/map",
    "/maps/5cm/1hz",
    "/tracked_pose",
    "/trajectory",
    "/scan_matched_points2",
]


# ── HTTP helpers ──────────────────────────────────────────────

def _request(
    ip: str,
    secret: str,
    method: str,
    path: str,
    json_data: Any = None,
    timeout: int = HTTP_TIMEOUT,
) -> dict:
    url = f"http://{ip}:{PORT}{path}"
    headers = {"Secret": secret}
    res = requests.request(
        method,
        url,
        headers=headers,
        json=json_data,
        timeout=timeout,
    )
    if not res.ok:
        error_body = ""
        try:
            error_body = res.text[:500]
        except Exception:
            pass
        logger.error(f"{method} {path} → {res.status_code}: {error_body}")
        res.raise_for_status()
    if res.status_code == 204 or not res.content:
        return {}
    return res.json()


def _get(ip: str, secret: str, path: str) -> dict:
    return _request(ip, secret, "GET", path)


def _post(ip: str, secret: str, path: str, data: Any = None) -> dict:
    return _request(ip, secret, "POST", path, json_data=data)


def _put(ip: str, secret: str, path: str, data: Any = None) -> dict:
    return _request(ip, secret, "PUT", path, json_data=data)


def _delete(ip: str, secret: str, path: str) -> dict:
    return _request(ip, secret, "DELETE", path)


def _patch(ip: str, secret: str, path: str, data: Any = None) -> dict:
    return _request(ip, secret, "PATCH", path, json_data=data)


# ── /maps ─────────────────────────────────────────────────────

def get_maps(ip: str, secret: str) -> dict:
    return _get(ip, secret, "/maps")


def get_map(ip: str, secret: str, map_name: str) -> dict:
    return _get(ip, secret, f"/maps/{map_name}")


def create_map(ip: str, secret: str, data: dict) -> dict:
    # 맵 업로드는 Base64 데이터가 크므로 타임아웃을 120초로 설정
    return _request(ip, secret, "POST", "/maps/", json_data=data, timeout=120)


def update_map(ip: str, secret: str, map_name: str, data: dict) -> dict:
    return _put(ip, secret, f"/maps/{map_name}", data)


def delete_map(ip: str, secret: str, map_name: str) -> dict:
    return _delete(ip, secret, f"/maps/{map_name}")


def patch_map(ip: str, secret: str, map_name: str, data: dict) -> dict:
    return _patch(ip, secret, f"/maps/{map_name}", data)


def get_map_by_id(ip: str, secret: str, map_id: int) -> dict:
    """숫자 ID로 로봇 맵 상세 조회 — GET /maps/{id}"""
    return _get(ip, secret, f"/maps/{map_id}")


def download_map_binary(ip: str, secret: str, map_id: int) -> bytes:
    """맵 carto_map 바이너리 다운로드 — GET /maps/{id}/download"""
    url = f"http://{ip}:{PORT}/maps/{map_id}/download"
    res = requests.get(url, headers={"Secret": secret}, timeout=30)
    res.raise_for_status()
    return res.content


def download_map_image(ip: str, secret: str, map_id: int) -> bytes:
    """맵 occupancy_grid (PNG) 다운로드 — GET /maps/{id}.png"""
    url = f"http://{ip}:{PORT}/maps/{map_id}.png"
    res = requests.get(url, headers={"Secret": secret}, timeout=30)
    res.raise_for_status()
    return res.content


def delete_map_by_id(ip: str, secret: str, map_id: int) -> dict:
    """숫자 ID로 로봇 맵 삭제 — DELETE /maps/{id}"""
    return _delete(ip, secret, f"/maps/{map_id}")


def patch_map_by_id(ip: str, secret: str, map_id: int, data: dict) -> dict:
    """숫자 ID로 로봇 맵 부분 수정 — PATCH /maps/{id}"""
    return _patch(ip, secret, f"/maps/{map_id}", data)


def update_map_by_id(ip: str, secret: str, map_id: int, data: dict) -> dict:
    """숫자 ID로 로봇 맵 전체 업데이트 — PUT /maps/{id}
    carto_map 포함 시 데이터가 크므로 타임아웃 120초."""
    return _request(ip, secret, "PUT", f"/maps/{map_id}", json_data=data, timeout=120)


# ── /mappings 데이터 다운로드 ─────────────────────────────────

def download_mapping_data(ip: str, secret: str, mapping_id: int) -> dict:
    """매핑의 전체 데이터 다운로드 — GET /mappings/{id}/download

    반환 JSON에 포함되는 주요 필드:
      - carto_map: Base64 인코딩 SLAM 바이너리 (pbstream)
      - occupancy_grid: Base64 인코딩 PNG 이미지
      - grid_origin_x, grid_origin_y, grid_resolution: 좌표 메타
      - trajectories: 이동 경로 좌표 배열
    """
    url = f"http://{ip}:{PORT}/mappings/{mapping_id}/download"
    res = requests.get(url, headers={"Secret": secret}, timeout=60)
    res.raise_for_status()
    return res.json()


def get_latest_finished_mapping(ip: str, secret: str) -> dict | None:
    """가장 최근 완료된(finished) 매핑 반환. 없으면 None."""
    mappings = _get(ip, secret, "/mappings/")
    if not isinstance(mappings, list):
        return None
    finished = [m for m in mappings if m.get("state") == "finished"]
    if not finished:
        return None
    # id가 가장 큰 것 = 가장 최근
    return max(finished, key=lambda m: m.get("id", 0))


# ── /services ─────────────────────────────────────────────────

def restart_robot_service(ip: str, secret: str) -> dict:
    """로봇 서비스 재시작 — POST /services/restart_service
    재시작 후 약 60-90초간 응답 불가."""
    return _post(ip, secret, "/services/restart_service")


# ── /chassis ──────────────────────────────────────────────────

def set_chassis_pose(ip: str, secret: str, data: dict) -> dict:
    return _post(ip, secret, "/chassis/pose", data)


def get_current_map(ip: str, secret: str) -> dict:
    """현재 지도 조회 — GET /chassis/current-map"""
    return _get(ip, secret, "/chassis/current-map")


def set_current_map(ip: str, secret: str, data: dict) -> dict:
    """현재 지도 설정 — POST /chassis/current-map {map_id: ...}"""
    return _post(ip, secret, "/chassis/current-map", data)


# ── /mappings ─────────────────────────────────────────────────

def get_mappings(ip: str, secret: str) -> dict:
    return _get(ip, secret, "/mappings/")


def get_mapping_by_id(ip: str, secret: str, mapping_id: int) -> dict:
    return _get(ip, secret, f"/mappings/{mapping_id}")


def create_mapping(ip: str, secret: str, data: dict) -> dict:
    return _post(ip, secret, "/mappings/", data)


def update_mapping(ip: str, secret: str, data: dict) -> dict:
    return _put(ip, secret, "/mappings/", data)


def delete_mapping(ip: str, secret: str) -> dict:
    return _delete(ip, secret, "/mappings/")


def patch_mapping(ip: str, secret: str, data: dict) -> dict:
    return _patch(ip, secret, "/mappings/", data)


# ── /mappings/current ─────────────────────────────────────────

def get_current_mapping(ip: str, secret: str) -> dict:
    return _get(ip, secret, "/mappings/current")


def create_current_mapping(ip: str, secret: str, data: dict) -> dict:
    return _post(ip, secret, "/mappings/current", data)


def update_current_mapping(ip: str, secret: str, data: dict) -> dict:
    return _put(ip, secret, "/mappings/current", data)


def delete_current_mapping(ip: str, secret: str) -> dict:
    return _delete(ip, secret, "/mappings/current")


def patch_current_mapping(ip: str, secret: str, data: dict) -> dict:
    return _patch(ip, secret, "/mappings/current", data)


# ── WebSocket relay (robot → queue) ──────────────────────────

class RobotWSRelay:
    """로봇 WebSocket에 연결하여 토픽 데이터를 큐로 전달하는 백그라운드 릴레이."""

    def __init__(self, ip: str, secret: str, topics: Optional[list[str]] = None):
        self.ip = ip
        self.secret = secret
        self.topics = topics or MAP_WS_TOPICS
        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._send_queue: list[str] = []
        self._send_lock = threading.Lock()

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def send_to_robot(self, message: str):
        with self._send_lock:
            self._send_queue.append(message)

    def poll(self) -> list[str]:
        with self._lock:
            items = self._queue[:]
            self._queue.clear()
        return items

    def _run(self):
        ws_url = f"ws://{self.ip}:{PORT}/ws/v2/topics"
        max_retries = 3

        for attempt in range(max_retries):
            if not self._running:
                return
            try:
                self._ws = create_connection(ws_url, timeout=WS_TIMEOUT)

                for topic in self.topics:
                    self._ws.send(json.dumps({"enable_topic": topic}))

                self._ws.settimeout(0.1)

                while self._running:
                    # 프론트엔드 → 로봇 전달
                    with self._send_lock:
                        pending = self._send_queue[:]
                        self._send_queue.clear()
                    for msg in pending:
                        try:
                            self._ws.send(msg)
                        except Exception:
                            pass

                    # 로봇 → 큐 수집
                    try:
                        raw = self._ws.recv()
                        if raw:
                            with self._lock:
                                self._queue.append(raw)
                    except Exception:
                        pass

                return  # 정상 종료

            except (WebSocketException, OSError, TimeoutError) as exc:
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
                    self._ws = None

                if attempt < max_retries - 1:
                    time.sleep(2)  # 재시도 전 대기
                    continue

                error_msg = json.dumps({"error": f"robot_ws_connection_failed: {exc}"})
                with self._lock:
                    self._queue.append(error_msg)

            finally:
                if self._ws is not None:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
