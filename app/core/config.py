from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", extra="ignore", hide_input_in_errors=True)

    PROJECT_NAME: str = "PyAutomation Manager"
    SECRET_KEY: str = Field(min_length=32)
    API_KEY: Optional[str] = None
    DEBUG: bool = False

    # Caminhos
    DATA_DIR: Path = BASE_DIR / "data"
    AUTOMATIONS_DIR: Path = BASE_DIR / "data" / "automations"
    DB_PATH: Path = BASE_DIR / "data" / "app.db"
    
    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite:///{self.DB_PATH.as_posix()}"

    # Limites de Execução
    MAX_CONCURRENT_EXECUTIONS: int = Field(default=5, ge=1)
    DEFAULT_TIMEOUT_SECONDS: int = 3600
    MAX_LOG_SIZE_BYTES: int = Field(default=5 * 1024 * 1024, ge=256)
    EXECUTION_LEASE_SECONDS: int = Field(default=120, ge=30)
    HEARTBEAT_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=10)

    # Autenticação Padrão Inicial
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: Optional[str] = Field(default=None, min_length=12)

    @field_validator("SECRET_KEY")
    @classmethod
    def reject_default_secret(cls, value: str) -> str:
        if value == "super-secret-key-change-in-production-1234567890":
            raise ValueError("Configure uma SECRET_KEY aleatória, exclusiva desta instalação.")
        return value

    @field_validator("DEBUG", mode="before")
    @classmethod
    def parse_debug_mode(cls, value):
        if isinstance(value, str) and value.lower() in {"release", "debug"}:
            return value.lower() == "debug"
        return value

    # Fuso Horário Local (ex: 'America/Sao_Paulo', 'UTC')
    TIMEZONE: str = "America/Sao_Paulo"

    # APScheduler
    RUN_SCHEDULER_IN_WEB: bool = True

settings = Settings()

# Garante que os diretórios existam
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.AUTOMATIONS_DIR.mkdir(parents=True, exist_ok=True)
