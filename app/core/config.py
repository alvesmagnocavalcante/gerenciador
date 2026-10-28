from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PROJECT_NAME: str = "PyAutomation Manager"
    SECRET_KEY: str = "super-secret-key-change-in-production-1234567890"
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
    MAX_CONCURRENT_EXECUTIONS: int = 5
    DEFAULT_TIMEOUT_SECONDS: int = 3600
    MAX_LOG_SIZE_BYTES: int = 5 * 1024 * 1024  # 5MB

    # Autenticação Padrão Inicial
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"

    # Fuso Horário Local (ex: 'America/Sao_Paulo', 'UTC')
    TIMEZONE: str = "America/Sao_Paulo"

    # APScheduler
    RUN_SCHEDULER_IN_WEB: bool = True

settings = Settings()

# Garante que os diretórios existam
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.AUTOMATIONS_DIR.mkdir(parents=True, exist_ok=True)
