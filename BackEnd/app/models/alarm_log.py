from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text
from sqlalchemy.sql import func

from app.database import Base


class AlarmLog(Base):
    """알람 로그 테이블
    error_type: auth | task | robot | map | net | data
    severity: info | warning | error
    """
    __tablename__ = "alarm_logs"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    error_code = Column(String(20), nullable=False, index=True)
    error_type = Column(String(20), nullable=False, index=True)
    severity = Column(String(20), nullable=False, default="error")
    message = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    source = Column(String(100), nullable=True)
    robot_sn = Column(String(100), nullable=True, index=True)
    is_read = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
