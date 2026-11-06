"""인터랙티브 배차 세션 (VESA 모드)

태블릿에서 사람이 매 포지션마다 다음 위치를 누르는 운영 방식.
잭은 시작 시 1회 업 → 종료 시 1회 다운 (포지션마다 잭 사이클 없음).
"""
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class DispatchSession(Base):
    """로봇별 인터랙티브 배차 세션.

    status:
      starting       — start 직후, 워커가 standby로 가서 픽업 준비 중
      picking_up     — standby에서 align_with_rack → jack_up 진행 중
      moving         — target_poi_id 로 이동 중 (current_poi_id 비어있음)
      awaiting_next  — target_poi_id 도착, 다음 명령 대기
      returning      — 종료 명령 받고 standby 복귀 + 잭다운 + 충전소 도킹 중
      completed      — 정상 종료
      failed         — 오류로 종료
    """
    __tablename__ = "dispatch_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    robot_id = Column(Integer, ForeignKey("robots.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="starting")
    with_rack = Column(Boolean, nullable=False, default=True)  # True=렉 픽업 후 작업, False=잭 조작 없이 바로 작업
    first_poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="SET NULL"), nullable=True)
    current_poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="SET NULL"), nullable=True)
    target_poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="SET NULL"), nullable=True)
    last_error = Column(String(500), nullable=True)
    started_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    ended_at = Column(DateTime, nullable=True)

    robot = relationship("Robot")
    first_poi = relationship("MapPOI", foreign_keys=[first_poi_id])
    current_poi = relationship("MapPOI", foreign_keys=[current_poi_id])
    target_poi = relationship("MapPOI", foreign_keys=[target_poi_id])


class DispatchReservation(Base):
    """배차 예약 (가용 로봇이 없을 때 대기열).

    가용 로봇이 0대일 때 POI에서 호출하면 즉시 배차 대신 예약을 생성한다.
    이후 어떤 로봇이 작업을 종료해 가용해지면, 먼저 예약한(FIFO) POI부터
    자동으로 호출(start_session)된다.

    status:
      waiting    — 대기 중 (로봇 배정 대기)
      fulfilled  — 로봇 배정 완료 (자동 호출됨)
      cancelled  — 사용자 취소 또는 무효화
    """
    __tablename__ = "dispatch_reservations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="CASCADE"), nullable=False, index=True)
    with_rack = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="waiting", index=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    fulfilled_at = Column(DateTime, nullable=True)

    poi = relationship("MapPOI")


class DispatchWaypoint(Base):
    """배차 세션의 경유지 (콘솔에서 순서대로 등록한 작업 경로).

    호출 위치 도착 후 콘솔에서 B→C→D 순서로 등록하면 seq=0,1,2 로 저장된다.
    로봇은 seq 순서대로 이동하며, 각 위치 도착 후 로봇 부착 태블릿의 [확인]을
    눌러야 다음 seq 로 진행한다. 마지막 seq 확인 후 자동 종료.

    status:
      pending  — 아직 안 감
      current  — 현재 이동 대상 / 도착해서 확인 대기 중
      done     — 확인 완료(통과)
    """
    __tablename__ = "dispatch_waypoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("dispatch_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime, server_default=func.now(), nullable=False)

    poi = relationship("MapPOI")


class TabletSlot(Base):
    """태블릿 슬롯 → POI 매핑.

    태블릿 앱이 외우기 쉽게 1, 2, 3... 슬롯 번호로 접속하고,
    슬롯 번호는 DB에서 실제 POI ID로 변환됨.
    슬롯에 매핑이 없으면 태블릿 페이지에서 설정 모드 노출.
    """
    __tablename__ = "tablet_slots"

    slot_number = Column(Integer, primary_key=True, autoincrement=False)
    poi_id = Column(Integer, ForeignKey("map_pois.id", ondelete="CASCADE"), nullable=False)
    alias = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    poi = relationship("MapPOI")


class JobPoint(Base):
    """작업지점(J) ↔ 랙 보관 위치(R) 고정 매핑 + 랙 점유 상태.

    2026-08-24 변경 시나리오용. J1 호출이면 R1, J2 호출이면 R2 에서 랙을 가져온다.

    POI id 가 아니라 **이름**으로 저장하는 이유:
      맵을 다시 동기화하면 POI 가 삭제·재생성돼 id 가 바뀐다. 그때마다 매핑이
      끊기면 안 되므로 이름으로 묶는다. (charging_id/standby_id 를 이름으로
      백업·복원하는 crud/map.py 의 처리와 같은 이유)

    occupied:
      로봇이 J 에 랙을 내려놓으면 True. 작업자가 랙을 치우고 태블릿에서
      [확인] 을 누르면 False. True 인 상태로 호출이 오면 배차하지 않고
      "작업지역에 랙이 있어요" 알람을 띄운다.
    """
    __tablename__ = "job_points"
    __table_args__ = (UniqueConstraint("area_id", "j_poi_name", name="uq_job_point_area_j"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    area_id = Column(Integer, nullable=True, index=True)
    j_poi_name = Column(String(200), nullable=False)      # 작업지점 이름   예) J1
    r_poi_name = Column(String(200), nullable=False)      # 랙 보관 위치   예) R1
    occupied = Column(Boolean, nullable=False, default=False)   # J 에 랙이 놓여 있나
    occupied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class DispatchJobLog(Base):
    """배송 작업 기록 — 작업 1건 = R 에서 랙 적재 → 짝 J 하차 (2026-11-06).

    배차 콘솔의 [로그] 화면(작업 기록·요약)이 읽는다. 배차 동작에는 쓰지 않는다
    (기록이 실패해도 배차는 그대로 돈다 — services/job_log.py 참조).

    이름(r_name/j_name)으로 저장하는 이유는 JobPoint 와 같다 — 맵을 다시 동기화하면
    POI id 가 바뀌어도 기록이 끊기지 않게.

    result:
      None           — 진행 중
      completed      — J 하차까지 끝남
      cancelled      — 사용자 중지(작업 중지)로 끊김
      force_stopped  — 강제 종료로 끊김
      failed         — 그 밖의 실패(랙 없음·이동 실패·서버 재시작 등, note 에 사유)

    continued:
      J 하차 **직후**, 충전소 복귀를 시작하기 전에 바로 이어받은 작업이면 True.
      복귀 중(사전 접근·도킹 전후)에 이어받은 것은 False.
    """
    __tablename__ = "dispatch_job_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    area_id = Column(Integer, nullable=True, index=True)
    robot_id = Column(Integer, nullable=True)
    robot_name = Column(String(100), nullable=True)
    r_name = Column(String(200), nullable=True)            # 랙 보관 위치 이름   예) R1
    j_name = Column(String(200), nullable=True)            # 작업지점 이름      예) J1
    route_key = Column(String(100), nullable=True)         # "R1→J1"
    call_at = Column(DateTime, nullable=True)              # 호출(예약 생성) 시각
    started_at = Column(DateTime, nullable=True, index=True)   # 이 작업 출발
    unloaded_at = Column(DateTime, nullable=True)          # J 하차 완료
    ended_at = Column(DateTime, nullable=True)
    result = Column(String(20), nullable=True)
    note = Column(String(200), nullable=True)              # 실패·중단 사유
    continued = Column(Boolean, nullable=False, default=False)
    prev_job_id = Column(Integer, nullable=True)           # 연속이면 직전 작업 id
    source = Column(String(20), nullable=False, default="live")   # live / log_import
    session_id = Column(Integer, nullable=True)
    # 로그 가져오기 중복 방지 키 — live 는 NULL (MariaDB UNIQUE 는 NULL 여러 개 허용)
    dedup_key = Column(String(120), nullable=True, unique=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
