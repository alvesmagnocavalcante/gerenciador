from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.core.database import Base

class Execution(Base):
    __tablename__ = "executions"

    id = Column(Integer, primary_key=True, index=True)
    automation_id = Column(Integer, ForeignKey("automations.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 'manual' ou 'scheduled'
    trigger_type = Column(String(20), default="manual", nullable=False)
    
    # 'PENDING', 'RUNNING', 'SUCCESS', 'FAILED', 'TIMEOUT', 'CANCELLED'
    status = Column(String(20), default="PENDING", nullable=False, index=True)
    
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    
    exit_code = Column(Integer, nullable=True)
    logs = Column(Text, default="", nullable=False)
    error_message = Column(Text, nullable=True)
    process_pid = Column(Integer, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)

    automation = relationship("Automation", back_populates="executions")

    @property
    def started_at_local(self):
        if not self.started_at:
            return None
        from app.core.config import settings
        from zoneinfo import ZoneInfo
        dt = self.started_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(settings.TIMEZONE))

    @property
    def finished_at_local(self):
        if not self.finished_at:
            return None
        from app.core.config import settings
        from zoneinfo import ZoneInfo
        dt = self.finished_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(settings.TIMEZONE))
