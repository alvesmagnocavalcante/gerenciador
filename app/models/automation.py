from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime
from sqlalchemy.orm import relationship
from app.core.database import Base

class Automation(Base):
    __tablename__ = "automations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    description = Column(Text, nullable=True)
    
    # Origem: 'local', 'git', 'http'
    source_type = Column(String(20), default="local", nullable=False)
    
    # Status: 'active', 'paused', 'disabled'
    status = Column(String(20), default="active", nullable=False)

    # Git
    git_url = Column(String(255), nullable=True)
    git_branch = Column(String(100), default="main", nullable=True)
    current_commit = Column(String(40), nullable=True)
    git_token = Column(String(255), nullable=True)

    # Execução
    entrypoint = Column(String(255), default="main.py", nullable=False)
    arguments = Column(String(500), nullable=True)
    env_vars = Column(Text, nullable=True)  # Formato .env ou JSON com variáveis
    timeout_seconds = Column(Integer, default=3600, nullable=False)
    max_retries = Column(Integer, default=0, nullable=False)
    retry_delay_seconds = Column(Integer, default=10, nullable=False)
    cleanup_mode = Column(String(30), default="none", nullable=False)  # 'none', 'delete_git', 'delete_repo'
    target_worker = Column(String(100), default="local", nullable=False)  # 'local' ou nome da máquina remota

    # HTTP Trigger
    http_url = Column(String(500), nullable=True)
    http_method = Column(String(10), default="GET", nullable=True)
    http_headers = Column(Text, nullable=True)  # JSON format
    http_body = Column(Text, nullable=True)

    # Agendamento: 'none', 'interval', 'cron'
    trigger_type = Column(String(20), default="none", nullable=False)
    cron_expression = Column(String(100), nullable=True)
    interval_seconds = Column(Integer, nullable=True)
    next_run_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    executions = relationship("Execution", back_populates="automation", cascade="all, delete-orphan", order_by="desc(Execution.id)")

    @property
    def next_run_at_local(self):
        if not self.next_run_at:
            return None
        from app.core.config import settings
        from zoneinfo import ZoneInfo
        dt = self.next_run_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(settings.TIMEZONE))

    @property
    def is_running(self) -> bool:
        # Verifica se a última execução ainda está em andamento
        if self.executions and self.executions[0].status in ["PENDING", "RUNNING"]:
            return True
        return False
