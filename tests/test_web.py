from app.core.config import settings
from app.core.security import sign_session, verify_session
from app.models.automation import Automation
from app.models.execution import Execution
from app.services.executor import get_automation_dir

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

def test_session_token_expiration():
    valid_token = sign_session("testadmin")
    assert verify_session(valid_token) == "testadmin"

    # Token emitido há 10 dias deve expirar (max_age padrão de 7 dias)
    import time
    expired_token = sign_session("testadmin", timestamp=int(time.time()) - (86400 * 10))
    assert verify_session(expired_token) is None

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

def test_path_traversal_on_create_and_delete_cleanup_web(auth_client, db_session):
    bad_form = {
        "name": "Malicious Automation",
        "source_type": "local",
        "entrypoint": "../../evil.py",
        "local_script_content": "print('evil')",
        "trigger_type": "none",
    }
    resp = auth_client.post("/automations/new", data=bad_form, follow_redirects=False)
    assert resp.status_code == 400
    assert "Path traversal detectado" in resp.text

    # Testar que exclusão via web remove o diretório físico da automação
    auto = Automation(
        name="Auto To Delete",
        source_type="local",
        status="active",
        entrypoint="main.py",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    auto_dir = get_automation_dir(auto.id)
    (auto_dir / "main.py").write_text("print('del')", encoding="utf-8")
    assert auto_dir.exists()

    del_resp = auth_client.post(f"/automations/{auto.id}/delete", follow_redirects=False)
    assert del_resp.status_code == 302
    assert not auto_dir.exists()

def test_executions_page_and_xss_protection(client, auth_client, db_session):
    resp = auth_client.get("/executions")
    assert resp.status_code == 200
    assert "Histórico de Execuções" in resp.text

    auto = Automation(
        name="XSS Log Test",
        source_type="local",
        status="active",
        entrypoint="main.py",
    )
    db_session.add(auto)
    db_session.commit()
    db_session.refresh(auto)

    ex = Execution(
        automation_id=auto.id,
        trigger_type="manual",
        status="SUCCESS",
        logs="<script>alert('xss')</script>",
    )
    db_session.add(ex)
    db_session.commit()
    db_session.refresh(ex)

    # Sem autenticação, endpoint de logs deve exigir login (307)
    client.cookies.clear()
    unauth_resp = client.get(f"/executions/{ex.id}/logs", follow_redirects=False)
    assert unauth_resp.status_code == 307

    # Com autenticação, HTML malicioso nos logs deve ser escapado
    token = sign_session("testadmin")
    auth_client.cookies.set("pyautomation_session", token)
    logs_resp = auth_client.get(f"/executions/{ex.id}/logs")
    assert logs_resp.status_code == 200
    assert "<script>alert('xss')</script>" not in logs_resp.text
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in logs_resp.text

def test_api_endpoints(client, auth_client, db_session):
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

    # Sem autenticação deve retornar 401
    client.cookies.clear()
    unauth_resp = client.get("/api/automations")
    assert unauth_resp.status_code == 401

    # Autenticado via X-API-Key
    orig_api_key = settings.API_KEY
    settings.API_KEY = "test-secret-api-key"
    try:
        key_resp = client.get("/api/automations", headers={"X-API-Key": "test-secret-api-key"})
        assert key_resp.status_code == 200
    finally:
        settings.API_KEY = orig_api_key

    # Autenticado via cookie de sessão
    token = sign_session("testadmin")
    auth_client.cookies.set("pyautomation_session", token)
    resp = auth_client.get("/api/automations")
    assert resp.status_code == 200
    data = resp.json()
    assert any(a["id"] == auto.id for a in data)

    # Disparo via API
    resp = auth_client.post(f"/api/automations/{auto.id}/run")
    assert resp.status_code == 200
    assert "execution_id" in resp.json()

def test_remote_worker_flow(client, db_session):
    orig_api_key = settings.API_KEY
    settings.API_KEY = "worker-secret-key"
    headers = {"X-API-Key": "worker-secret-key"}

    try:
        auto = Automation(
            name="Robô Máquina Financeiro",
            source_type="local",
            status="active",
            entrypoint="nfe.py",
            target_worker="pc-financeiro",
            env_vars="CERT_PASS=minhasenha123\nMODO=producao",
        )
        db_session.add(auto)
        db_session.commit()
        db_session.refresh(auto)

        auto_dir = get_automation_dir(auto.id)
        (auto_dir / "nfe.py").write_text("print('EMITINDO_NFE')\n", encoding="utf-8")

        # 1. Disparar execução -> deve ficar PENDING aguardando o agente 'pc-financeiro'
        run_resp = client.post(f"/api/automations/{auto.id}/run", headers=headers)
        assert run_resp.status_code == 200
        exec_id = run_resp.json()["execution_id"]

        exec_rec = db_session.query(Execution).filter(Execution.id == exec_id).first()
        assert exec_rec.status == "PENDING"
        assert "pc-financeiro" in exec_rec.logs

        # 2. Outro worker ('pc-rh') faz poll -> não deve receber a tarefa do 'pc-financeiro'
        other_poll = client.post("/api/worker/poll?worker_name=pc-rh", headers=headers)
        assert other_poll.status_code == 200
        assert other_poll.json()["job"] is None

        # 3. Worker correto ('pc-financeiro') faz poll -> recebe a tarefa e muda para RUNNING
        poll_resp = client.post("/api/worker/poll?worker_name=pc-financeiro", headers=headers)
        assert poll_resp.status_code == 200
        job_data = poll_resp.json()["job"]
        assert job_data is not None
        assert job_data["execution_id"] == exec_id
        assert job_data["local_script_content"] == "print('EMITINDO_NFE')\n"
        assert job_data["env_vars"]["MODO"] == "producao"

        # 4. Worker envia heartbeat com logs em tempo real
        hb_resp = client.post(
            f"/api/worker/executions/{exec_id}/heartbeat",
            json={"logs": "Iniciando emissão...\npassword=segredo", "process_pid": 4321, "retry_count": 0},
            headers=headers,
        )
        assert hb_resp.status_code == 200
        assert hb_resp.json()["cancel_requested"] is False

        # 5. Worker reporta conclusão (SUCCESS)
        comp_resp = client.post(
            f"/api/worker/executions/{exec_id}/complete",
            json={
                "status": "SUCCESS",
                "exit_code": 0,
                "logs": "Iniciando emissão...\nEMITINDO_NFE\nConcluído!",
                "retry_count": 0,
            },
            headers=headers,
        )
        assert comp_resp.status_code == 200

        db_session.refresh(exec_rec)
        assert exec_rec.status == "SUCCESS"
        assert exec_rec.exit_code == 0
        assert "EMITINDO_NFE" in exec_rec.logs
    finally:
        settings.API_KEY = orig_api_key

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
        "target_worker": "vm-rpa-01",
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
    assert auto.target_worker == "vm-rpa-01"

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
