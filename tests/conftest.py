import pytest
import os
import shutil
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base, get_db, SessionLocal, engine
from app.core.security import hash_password, sign_session
from app.models.user import User
from app.main import app

# Utilizar banco de teste SQLite em memória ou arquivo temporário
TEST_DB_PATH = settings.DATA_DIR / "test_app.db"
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_PATH.as_posix()}"
TEST_AUTOMATIONS_DIR = settings.DATA_DIR / "test_automations"

engine_test = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine_test)

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    orig_automations_dir = settings.AUTOMATIONS_DIR
    settings.AUTOMATIONS_DIR = TEST_AUTOMATIONS_DIR
    TEST_AUTOMATIONS_DIR.mkdir(parents=True, exist_ok=True)

    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass

    Base.metadata.create_all(bind=engine_test)
    # Configurar SessionLocal para apontar para o banco de teste em threads/serviços
    SessionLocal.configure(bind=engine_test)

    # Criar usuário admin de teste
    with TestingSessionLocal() as db:
        user = User(
            username="testadmin",
            password_hash=hash_password("testpass123"),
            is_admin=True
        )
        db.add(user)
        db.commit()

    yield

    SessionLocal.configure(bind=engine)
    settings.AUTOMATIONS_DIR = orig_automations_dir
    if TEST_AUTOMATIONS_DIR.exists():
        shutil.rmtree(TEST_AUTOMATIONS_DIR, ignore_errors=True)
    if TEST_DB_PATH.exists():
        try:
            TEST_DB_PATH.unlink()
        except Exception:
            pass

@pytest.fixture
def db_session():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

@pytest.fixture
def auth_client(client):
    # Insere o cookie de sessão assinado
    token = sign_session("testadmin")
    client.cookies.set("pyautomation_session", token)
    return client
