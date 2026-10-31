import os
import secrets
import uuid
from pathlib import Path

# Antes de importar a aplicação: engines, jobstore e settings ficam isolados.
ARTIFACTS = Path(__file__).resolve().parents[1] / ".test-artifacts"
ARTIFACTS.mkdir(exist_ok=True)
TEST_ROOT = ARTIFACTS / f"tests-{uuid.uuid4().hex}"
TEST_ROOT.mkdir()
os.environ.update({
    "DATA_DIR": str(TEST_ROOT),
    "DB_PATH": str(TEST_ROOT / "app.db"),
    "AUTOMATIONS_DIR": str(TEST_ROOT / "automations"),
    "SECRET_KEY": secrets.token_urlsafe(48),
    "ADMIN_PASSWORD": secrets.token_urlsafe(24),
    "DEBUG": "false",
    "RUN_SCHEDULER_IN_WEB": "false",
})

import pytest
from fastapi.testclient import TestClient
from app.core.config import settings
from app.core.database import Base, get_db, SessionLocal, engine, init_db
from app.core.security import hash_password, sign_session
from app.models.user import User
from app.main import app
from app.services.scheduler import scheduler, shutdown_scheduler
from app.services.executor import start_executor, stop_executor


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    # Mesmo bind para rotas, threads, inicialização e SQLAlchemyJobStore.
    SessionLocal.configure(bind=engine)
    init_db()
    with SessionLocal() as db:
        db.add(User(username="testadmin", password_hash=hash_password("testpass123"), is_admin=True))
        db.commit()
    start_executor()
    yield
    shutdown_scheduler()
    stop_executor()
    engine.dispose()
    import shutil
    shutil.rmtree(TEST_ROOT)


@pytest.fixture
def db_session():
    with SessionLocal() as db:
        yield db


@pytest.fixture
def client():
    # Cada requisição recebe sua própria sessão, como em produção.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_client(client):
    client.cookies.set("pyautomation_session", sign_session("testadmin"))
    return client
