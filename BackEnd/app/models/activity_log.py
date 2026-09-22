from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func

from app.database import Base


class ActivityLog(Base):
    """운영 활동 로그 테이블
    category: task | map | convoy | robot | system
    """
    __tablename__ = "activity_logs"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(20), nullable=False, index=True)
    action = Column(String(50), nullable=False, index=True)
    message = Column(String(500), nullable=False)
    detail = Column(Text, nullable=True)
    robot_id = Column(Integer, nullable=True, index=True)
    robot_name = Column(String(100), nullable=True)
    source = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
