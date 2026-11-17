import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import (
    Settings,
    ensure_setup_token,
    load_or_create_internal_credentials,
    read_setup_token,
    settings,
)
from app.core.database import Base, get_db
from app.core.security import verify_password
from app.models.user import User
from app.web import routes


ROOT = Path(__file__).resolve().parents[1]


def test_internal_credentials_are_generated_and_persisted(artifact_tmp_path):
    first = load_or_create_internal_credentials(artifact_tmp_path)
    second = load_or_create_internal_credentials(artifact_tmp_path)

    assert first == second
    assert first["secret_key"] != first["api_key"]
    assert len(first["secret_key"]) >= 32
    assert len(first["api_key"]) >= 32
    assert (artifact_tmp_path / "internal-credentials.json").is_file()


def test_invalid_persisted_credentials_fail_closed(artifact_tmp_path):
    (artifact_tmp_path / "internal-credentials.json").write_text(
        '{"secret_key":"short"}', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="Credenciais internas"):
        load_or_create_internal_credentials(artifact_tmp_path)


def test_startup_without_env_persists_sessions_across_processes(artifact_tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"SECRET_KEY", "API_KEY", "ADMIN_PASSWORD", "SETUP_TOKEN"}
    }
    env.update(
        {
            "DATA_DIR": str(artifact_tmp_path),
            "AUTOMATIONS_DIR": str(artifact_tmp_path / "automations"),
            "DB_PATH": str(artifact_tmp_path / "app.db"),
            "RUN_SCHEDULER_IN_WEB": "false",
        }
    )
    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.core.database import init_db; init_db(); "
            "from app.core.security import sign_session; print(sign_session('restart-owner'))",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert create.returncode == 0, create.stderr
    token = create.stdout.strip()

    verify = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from app.core.security import verify_session; "
            "print(verify_session(sys.argv[1]) or '')",
            token,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert verify.returncode == 0, verify.stderr
    assert verify.stdout.strip() == "restart-owner"


def test_explicit_environment_credentials_take_precedence(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "s" * 48)
    monkeypatch.setenv("API_KEY", "a" * 48)
    configured = Settings(_env_file=None)
    assert configured.SECRET_KEY == "s" * 48
    assert configured.API_KEY == "a" * 48

    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.setenv("SETUP_TOKEN", "")
    configured = Settings(_env_file=None)
    assert configured.ADMIN_PASSWORD is None
    assert configured.SETUP_TOKEN is None


@pytest.fixture
def setup_app(artifact_tmp_path, monkeypatch):
    database_path = artifact_tmp_path / "setup.db"
    setup_engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SetupSession = sessionmaker(autocommit=False, autoflush=False, bind=setup_engine)
    Base.metadata.create_all(setup_engine)

    monkeypatch.setattr(settings, "DATA_DIR", artifact_tmp_path)
    monkeypatch.setattr(settings, "SETUP_TOKEN", None)
    routes._setup_attempts.clear()
    ensure_setup_token()

    app = FastAPI()
    app.include_router(routes.router)

    def override_db():
        with SetupSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    yield app, SetupSession, read_setup_token()
    setup_engine.dispose()


def _setup_form(client: TestClient, token: str, **overrides):
    page = client.get("/setup")
    assert page.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    data = {
        "username": "owner",
        "password": "strong-password-123",
        "password_confirmation": "strong-password-123",
        "setup_token": token,
        "csrf_token": csrf,
    }
    data.update(overrides)
    return data


def test_initial_setup_creates_admin_and_blocks_reuse(setup_app):
    app, SetupSession, token = setup_app
    with TestClient(app) as client:
        response = client.post("/setup", data=_setup_form(client, token), follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "pyautomation_session" in response.cookies
        assert client.get("/setup").status_code == 404

    with SetupSession() as db:
        user = db.query(User).one()
        assert user.username == "owner"
        assert user.is_admin is True
        assert verify_password(user.password_hash, "strong-password-123")


def test_initial_setup_rejects_invalid_password_and_csrf(setup_app):
    app, SetupSession, token = setup_app
    with TestClient(app) as client:
        invalid_password = _setup_form(
            client,
            token,
            password="short",
            password_confirmation="short",
        )
        assert client.post("/setup", data=invalid_password).status_code == 400

        invalid_csrf = _setup_form(client, token)
        invalid_csrf["csrf_token"] = "invalid"
        assert client.post("/setup", data=invalid_csrf).status_code == 403

    with SetupSession() as db:
        assert db.query(User).count() == 0


def test_initial_setup_rate_limits_invalid_authorization(setup_app):
    app, _, token = setup_app
    with TestClient(app) as client:
        for _ in range(5):
            form = _setup_form(client, token, setup_token="x" * 32)
            assert client.post("/setup", data=form).status_code == 403
        form = _setup_form(client, token, setup_token="x" * 32)
        assert client.post("/setup", data=form).status_code == 429


def test_concurrent_initial_setup_creates_only_one_admin(setup_app):
    app, SetupSession, token = setup_app

    def submit(username: str):
        with TestClient(app) as client:
            form = _setup_form(client, token, username=username)
            return client.post("/setup", data=form, follow_redirects=False).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ("owner-one", "owner-two")))

    assert statuses.count(303) == 1
    assert set(statuses) <= {303, 409}
    with SetupSession() as db:
        assert db.query(User).count() == 1
