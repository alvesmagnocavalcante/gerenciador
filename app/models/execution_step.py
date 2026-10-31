from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class ExecutionStep(Base):
    __tablename__ = "execution_steps"

    id = Column(Integer, primary_key=True)
    execution_id = Column(Integer, ForeignKey("executions.id", ondelete="CASCADE"), nullable=False, index=True)
    event_id = Column(String(36), nullable=False)
    name = Column(String(200), nullable=False)
    status = Column(String(20), nullable=False, default="RUNNING")
    attempt = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Float, nullable=True)
    error_message = Column(Text, nullable=True)

    execution = relationship("Execution", back_populates="steps")

    __table_args__ = (
        UniqueConstraint("execution_id", "event_id", name="uq_execution_step_event"),
        Index("ix_execution_steps_execution_started", "execution_id", "started_at"),
    )
