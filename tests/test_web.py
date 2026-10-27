from app.models.automation import Automation
from app.models.execution import Execution

def test_login_flow(client):
    # Rota protegida sem autenticação redireciona
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/login"

    # Login com senha incorreta
    resp = client.post("/login", data={"username": "testadmin", "password": "wrongpassword"})
    assert resp.status_code == 401
    assert "Usuário ou senha inválidos" in resp.text

    # Login correto
    resp = client.post("/login", data={"username": "testadmin", "password": "testpass123"}, follow_redirects=False)
    assert resp.status_code == 302
    assert "pyautomation_session" in resp.cookies

def test_dashboard_authenticated(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200
    assert "Visão Geral" in resp.text
    assert "Total Automações" in resp.text

def test_create_automation_web(auth_client, db_session):
    form_data = {
        "name": "Automação Web Teste",
        "description": "Criada pelo teste web",
        "source_type": "local",
        "entrypoint": "main.py",
        "local_script_content": "print('Web Created Script!')",
        "trigger_type": "none",
        "timeout_seconds": 60,
        "max_retries": 0,
        "retry_delay_seconds": 10,
    }
    resp = auth_client.post("/automations/new", data=form_data, follow_redirects=False)
    assert resp.status_code == 302

    auto = db_session.query(Automation).filter(Automation.name == "Automação Web Teste").first()
    assert auto is not None
    assert auto.status == "active"

    # Acessar a página de detalhes
    resp = auth_client.get(f"/automations/{auto.id}")
    assert resp.status_code == 200
    assert "Automação Web Teste" in resp.text
    assert "Web Created Script!" in resp.text

def test_executions_page(auth_client):
    resp = auth_client.get("/executions")
    assert resp.status_code == 200
    assert "Histórico de Execuções" in resp.text

def test_api_endpoints(client, db_session):
    auto = Automation(
        name="API Test Automation",
        source_type="local",
        status="active",
        entrypoint="main.py",
        trigger_type="none",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    # Listar automações via API
    resp = client.get("/api/automations")
    assert resp.status_code == 200
    data = resp.json()
    assert any(a["id"] == auto.id for a in data)

    # Disparo via API
    resp = client.post(f"/api/automations/{auto.id}/run")
    assert resp.status_code == 200
    assert "execution_id" in resp.json()

def test_save_automation_env_web(auth_client, db_session):
    auto = Automation(
        name="Automation Env Test",
        source_type="local",
        status="active",
        entrypoint="main.py",
        trigger_type="none",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    env_content = "BOOKING_USER=teste@hotel.com\nBOOKING_PASS=123456\nHEADLESS=true"
    resp = auth_client.post(
        f"/automations/{auto.id}/env",
        data={"env_vars": env_content},
        headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert "Variáveis (.env) salvas com sucesso" in resp.text

    db_session.refresh(auto)
    assert "BOOKING_USER=teste@hotel.com" in auto.env_vars

def test_edit_automation_web(auth_client, db_session):
    auto = Automation(
        name="Original Name",
        description="Original Desc",
        source_type="local",
        status="active",
        entrypoint="main.py",
        timeout_seconds=1200,
        trigger_type="none",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    # 1. Carregar página de edição GET
    resp = auth_client.get(f"/automations/{auto.id}/edit")
    assert resp.status_code == 200
    assert "Editar Automação: Original Name" in resp.text
    assert 'value="Original Name"' in resp.text

    # 2. Salvar alterações POST
    update_data = {
        "name": "Updated Name",
        "description": "Updated Desc",
        "source_type": "local",
        "entrypoint": "main.py",
        "timeout_seconds": 1800,
        "max_retries": 2,
        "retry_delay_seconds": 15,
        "trigger_type": "interval",
        "interval_seconds": 300,
        "env_vars": "DEBUG=true",
    }
    resp = auth_client.post(f"/automations/{auto.id}/edit", data=update_data, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == f"/automations/{auto.id}"

    db_session.refresh(auto)
    assert auto.name == "Updated Name"
    assert auto.description == "Updated Desc"
    assert auto.timeout_seconds == 1800
    assert auto.max_retries == 2
    assert auto.interval_seconds == 300
    assert auto.env_vars == "DEBUG=true"

def test_format_datetime_filter():
    from app.web.routes import format_datetime_filter
    from datetime import datetime, timezone

    # 17:24 UTC deve virar 14:24 no fuso America/Sao_Paulo (UTC-3)
    utc_dt = datetime(2026, 10, 27, 17, 24, 0, tzinfo=timezone.utc)
    formatted = format_datetime_filter(utc_dt, "%d/%m/%Y %H:%M")
    assert formatted == "27/10/2026 14:24"

    # Datetime ingênuo (naive) também é tratado como UTC
    naive_dt = datetime(2026, 10, 27, 17, 24, 0)
    formatted_naive = format_datetime_filter(naive_dt, "%d/%m/%Y %H:%M")
    assert formatted_naive == "27/10/2026 14:24"




