import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent.parent
_CREDENTIALS_FILE = "internal-credentials.json"
_SETUP_TOKEN_FILE = "setup-token"


def _private_file_is_safe(path: Path) -> None:
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError(f"Permissões inseguras no arquivo protegido: {path}")


def _atomic_write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
    descriptor = None
    try:
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)
        raise


def _read_credentials(path: Path) -> dict[str, str]:
    try:
        _private_file_is_safe(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        secret_key = payload["secret_key"]
        api_key = payload["api_key"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Credenciais internas inválidas ou inacessíveis em {path}") from exc
    if (
        not isinstance(secret_key, str)
        or len(secret_key) < 32
        or not isinstance(api_key, str)
        or len(api_key) < 32
        or secrets.compare_digest(secret_key, api_key)
    ):
        raise RuntimeError(f"Credenciais internas inválidas em {path}")
    return {"secret_key": secret_key, "api_key": api_key}


def load_or_create_internal_credentials(data_dir: Path) -> dict[str, str]:
    """Carrega ou cria chaves internas distintas sem expô-las em logs."""
    path = data_dir / _CREDENTIALS_FILE
    if path.exists():
        return _read_credentials(path)

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / f".{_CREDENTIALS_FILE}.lock"
    deadline = time.monotonic() + 10
    while True:
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(lock_fd)
            break
        except FileExistsError:
            if path.exists():
                return _read_credentials(path)
            if time.monotonic() >= deadline:
                raise RuntimeError("Não foi possível obter a trava das credenciais internas.")
            time.sleep(0.05)

    try:
        if path.exists():
            return _read_credentials(path)
        credentials = {
            "secret_key": secrets.token_urlsafe(48),
            "api_key": secrets.token_urlsafe(48),
        }
        _atomic_write_private(path, json.dumps(credentials, separators=(",", ":")) + "\n")
        return credentials
    finally:
        lock_path.unlink(missing_ok=True)


_BOOTSTRAP_DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
_INTERNAL_CREDENTIALS = load_or_create_internal_credentials(_BOOTSTRAP_DATA_DIR)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", extra="ignore", hide_input_in_errors=True
    )

    PROJECT_NAME: str = "Ongma"
    SECRET_KEY: str = Field(default=_INTERNAL_CREDENTIALS["secret_key"], min_length=32)
    API_KEY: str = Field(default=_INTERNAL_CREDENTIALS["api_key"], min_length=32)
    SETUP_TOKEN: Optional[str] = Field(default=None, min_length=32)
    DEBUG: bool = False

    DATA_DIR: Path = BASE_DIR / "data"
    AUTOMATIONS_DIR: Path = BASE_DIR / "data" / "automations"
    DB_PATH: Path = BASE_DIR / "data" / "app.db"

    @property
    def DATABASE_URL(self) -> str:
        return f"sqlite:///{self.DB_PATH.as_posix()}"

    MAX_CONCURRENT_EXECUTIONS: int = Field(default=5, ge=1)
    DEFAULT_TIMEOUT_SECONDS: int = 3600
    MAX_LOG_SIZE_BYTES: int = Field(default=5 * 1024 * 1024, ge=256)
    EXECUTION_LEASE_SECONDS: int = Field(default=120, ge=30)
    HEARTBEAT_INTERVAL_SECONDS: int = Field(default=5, ge=1, le=10)

    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: Optional[str] = Field(default=None, min_length=8)

    @field_validator("ADMIN_PASSWORD", "SETUP_TOKEN", mode="before")
    @classmethod
    def empty_optional_secret_is_none(cls, value):
        return None if value == "" else value

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def normalize_secret_key(cls, value):
        return _INTERNAL_CREDENTIALS["secret_key"] if value == "" or value is None else value

    @field_validator("API_KEY", mode="before")
    @classmethod
    def normalize_api_key(cls, value):
        return _INTERNAL_CREDENTIALS["api_key"] if value == "" or value is None else value

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

    TIMEZONE: str = "America/Sao_Paulo"
    RUN_SCHEDULER_IN_WEB: bool = True


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
settings.AUTOMATIONS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_setup_token() -> Path | None:
    """Cria a prova de posse do primeiro acesso, salvo quando fornecida via ambiente."""
    if settings.SETUP_TOKEN:
        return None
    path = settings.DATA_DIR / _SETUP_TOKEN_FILE
    if path.exists():
        read_setup_token()
        return path
    _atomic_write_private(path, secrets.token_urlsafe(36) + "\n")
    return path


def read_setup_token() -> str:
    if settings.SETUP_TOKEN:
        return settings.SETUP_TOKEN
    path = settings.DATA_DIR / _SETUP_TOKEN_FILE
    try:
        _private_file_is_safe(path)
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Token de configuração inicial inacessível.") from exc
    if len(token) < 32:
        raise RuntimeError("Token de configuração inicial inválido.")
    return token


def remove_setup_token() -> None:
    if not settings.SETUP_TOKEN:
        (settings.DATA_DIR / _SETUP_TOKEN_FILE).unlink(missing_ok=True)
