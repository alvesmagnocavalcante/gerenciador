from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
import logging
from fastapi import APIRouter, Request, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_password, sign_session
from app.models.user import User
from app.models.automation import Automation
from app.models.execution import Execution
from app.web.auth import get_current_user, get_current_user_optional, COOKIE_NAME
from app.services.scheduler import sync_automation_job, remove_automation_job
from app.services.executor import run_automation_job, cancel_execution, get_automation_dir, ensure_venv, rebuild_automation_venv, clean_automation_workspace
from app.services.git_service import clone_or_update_repo, GitError

logger = logging.getLogger("pyautomation.web")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

def format_datetime_filter(value, fmt: str = "%d/%m/%Y %H:%M:%S") -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return value
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local_dt = value.astimezone(ZoneInfo(settings.TIMEZONE))
    except Exception:
        local_dt = value.astimezone()
    return local_dt.strftime(fmt)

templates.env.filters["localdatetime"] = format_datetime_filter
templates.env.filters["datetime"] = format_datetime_filter
templates.env.globals["localdatetime"] = format_datetime_filter

router = APIRouter()

# ----------------- AUTENTICAÇÃO -----------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: Optional[User] = Depends(get_current_user_optional)):
    if user:
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})

@router.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(user.password_hash, password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Usuário ou senha inválidos.", "username": username},
            status_code=status.HTTP_401_UNAUTHORIZED
        )

    response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    session_token = sign_session(user.username)
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,  # 7 dias
    )
    return response

@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie(COOKIE_NAME)
    return response

# ----------------- DASHBOARD -----------------

@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    total_automations = db.query(Automation).count()
    active_automations = db.query(Automation).filter(Automation.status == "active").count()
    running_automations = db.query(Execution).filter(Execution.status.in_(["PENDING", "RUNNING"])).count()
    success_executions = db.query(Execution).filter(Execution.status == "SUCCESS").count()
    failed_executions = db.query(Execution).filter(Execution.status.in_(["FAILED", "TIMEOUT"])).count()

    upcoming_jobs = db.query(Automation).filter(
        Automation.status == "active",
        Automation.next_run_at != None
    ).order_by(Automation.next_run_at.asc()).limit(5).all()

    recent_executions = db.query(Execution).order_by(desc(Execution.id)).limit(10).all()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "total_automations": total_automations,
            "active_automations": active_automations,
            "running_automations": running_automations,
            "success_executions": success_executions,
            "failed_executions": failed_executions,
            "upcoming_jobs": upcoming_jobs,
            "recent_executions": recent_executions,
        }
    )

# ----------------- AUTOMAÇÕES -----------------

@router.get("/automations", response_class=HTMLResponse)
def automations_list(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    automations = db.query(Automation).order_by(desc(Automation.id)).all()
    return templates.TemplateResponse(
        request=request,
        name="automations/list.html",
        context={"user": user, "automations": automations}
    )

@router.get("/automations/new", response_class=HTMLResponse)
def automations_new(
    request: Request,
    user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(
        request=request,
        name="automations/create.html",
        context={"user": user, "error": None}
    )

@router.post("/automations/new", response_class=HTMLResponse)
def automations_create(
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    source_type: str = Form("local"),
    entrypoint: str = Form("main.py"),
    arguments: Optional[str] = Form(None),
    env_vars: Optional[str] = Form(None),
    timeout_seconds: int = Form(3600),
    max_retries: int = Form(0),
    retry_delay_seconds: int = Form(10),
    cleanup_mode: str = Form("none"),
    # Git
    git_url: Optional[str] = Form(None),
    git_branch: Optional[str] = Form("main"),
    git_token: Optional[str] = Form(None),
    # Local code script
    local_script_content: Optional[str] = Form(None),
    # HTTP
    http_url: Optional[str] = Form(None),
    http_method: Optional[str] = Form("GET"),
    http_headers: Optional[str] = Form(None),
    http_body: Optional[str] = Form(None),
    # Scheduling
    trigger_type: str = Form("none"),
    interval_seconds: Optional[int] = Form(None),
    cron_expression: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = Automation(
        name=name,
        description=description,
        source_type=source_type,
        status="active",
        entrypoint=entrypoint,
        arguments=arguments,
        env_vars=env_vars,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        cleanup_mode=cleanup_mode,
        git_url=git_url,
        git_branch=git_branch,
        git_token=git_token,
        http_url=http_url,
        http_method=http_method,
        http_headers=http_headers,
        http_body=http_body,
        trigger_type=trigger_type,
        interval_seconds=interval_seconds,
        cron_expression=cron_expression,
    )
    db.add(auto)
    db.commit()
    db.refresh(auto)

    # Se for código local fornecido no cadastro, gravar o arquivo
    auto_dir = get_automation_dir(auto.id)
    if source_type == "local" and local_script_content:
        entry_file = auto_dir / auto.entrypoint
        entry_file.write_text(local_script_content, encoding="utf-8")
    elif source_type == "git" and git_url:
        try:
            repo_dir = auto_dir / "repo"
            commit_sha = clone_or_update_repo(repo_dir, git_url, git_branch or "main", git_token)
            auto.current_commit = commit_sha
            db.commit()
            # Constrói a venv e instala dependências de pyproject.toml ou requirements.txt
            ensure_venv(auto.id, repo_dir)
        except GitError as e:
            # Registra aviso mas não quebra a criação
            pass
        except Exception as e:
            pass

    # Sincronizar com APScheduler
    try:
        next_run = sync_automation_job(auto)
        auto.next_run_at = next_run
        db.commit()
    except Exception:
        pass

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.get("/automations/{automation_id}", response_class=HTMLResponse)
def automations_detail(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    executions = db.query(Execution).filter(Execution.automation_id == auto.id).order_by(desc(Execution.id)).limit(30).all()

    # Ler conteúdo do script se for local
    code_content = None
    if auto.source_type == "local":
        file_path = get_automation_dir(auto.id) / auto.entrypoint
        if file_path.exists():
            try:
                code_content = file_path.read_text(encoding="utf-8")
            except Exception:
                code_content = "[Não foi possível ler o arquivo]"

    # Verificar se existe arquivo .env.example no repositório
    auto_dir = get_automation_dir(auto.id)
    working_dir = auto_dir / "repo" if auto.source_type == "git" else auto_dir
    env_example_content = None
    for example_candidate in [working_dir / ".env.example", auto_dir / ".env.example"]:
        if example_candidate.is_file():
            try:
                env_example_content = example_candidate.read_text(encoding="utf-8")
                break
            except Exception:
                pass

    return templates.TemplateResponse(
        request=request,
        name="automations/detail.html",
        context={
            "user": user,
            "automation": auto,
            "executions": executions,
            "code_content": code_content,
            "env_example_content": env_example_content,
        }
    )

@router.get("/automations/{automation_id}/edit", response_class=HTMLResponse)
def automations_edit(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    local_script_content = None
    if auto.source_type == "local":
        file_path = get_automation_dir(auto.id) / auto.entrypoint
        if file_path.exists():
            try:
                local_script_content = file_path.read_text(encoding="utf-8")
            except Exception:
                pass

    return templates.TemplateResponse(
        request=request,
        name="automations/edit.html",
        context={
            "user": user,
            "automation": auto,
            "local_script_content": local_script_content,
            "error": None,
        }
    )

@router.post("/automations/{automation_id}/edit", response_class=HTMLResponse)
def automations_update(
    automation_id: int,
    request: Request,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    source_type: str = Form("local"),
    entrypoint: str = Form("main.py"),
    arguments: Optional[str] = Form(None),
    env_vars: Optional[str] = Form(None),
    timeout_seconds: int = Form(3600),
    max_retries: int = Form(0),
    retry_delay_seconds: int = Form(10),
    cleanup_mode: str = Form("none"),
    # Git
    git_url: Optional[str] = Form(None),
    git_branch: Optional[str] = Form("main"),
    git_token: Optional[str] = Form(None),
    # Local code script
    local_script_content: Optional[str] = Form(None),
    # HTTP
    http_url: Optional[str] = Form(None),
    http_method: Optional[str] = Form("GET"),
    http_headers: Optional[str] = Form(None),
    http_body: Optional[str] = Form(None),
    # Scheduling
    trigger_type: str = Form("none"),
    interval_seconds: Optional[int] = Form(None),
    cron_expression: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    auto.name = name
    auto.description = description
    auto.source_type = source_type
    auto.entrypoint = entrypoint
    auto.arguments = arguments
    auto.env_vars = env_vars.strip() if env_vars else ""
    auto.timeout_seconds = timeout_seconds
    auto.max_retries = max_retries
    auto.retry_delay_seconds = retry_delay_seconds
    auto.cleanup_mode = cleanup_mode
    auto.git_url = git_url
    auto.git_branch = git_branch
    if git_token:
        auto.git_token = git_token
    auto.http_url = http_url
    auto.http_method = http_method
    auto.http_headers = http_headers
    auto.http_body = http_body
    auto.trigger_type = trigger_type
    auto.interval_seconds = interval_seconds
    auto.cron_expression = cron_expression

    auto_dir = get_automation_dir(auto.id)
    if source_type == "local" and local_script_content is not None:
        entry_file = auto_dir / auto.entrypoint
        entry_file.write_text(local_script_content, encoding="utf-8")
    elif source_type == "git" and auto.git_url:
        try:
            repo_dir = auto_dir / "repo"
            commit_sha = clone_or_update_repo(repo_dir, auto.git_url, auto.git_branch or "main", auto.git_token)
            auto.current_commit = commit_sha
            ensure_venv(auto.id, repo_dir)
        except Exception as e:
            logger.warning(f"Aviso ao sincronizar git na edição da automação {auto.id}: {e}")

    # Atualizar arquivo .env físico
    if auto.env_vars:
        try:
            from app.services.executor import parse_env_vars
            working_dir = auto_dir / "repo" if auto.source_type == "git" else auto_dir
            parsed = parse_env_vars(auto.env_vars)
            env_file = working_dir / ".env"
            env_lines = [f"{k}={v}" for k, v in parsed.items()]
            env_file.write_text("\n".join(env_lines) + "\n" if env_lines else "", encoding="utf-8")
        except Exception:
            pass

    # Sincronizar com APScheduler
    try:
        next_run = sync_automation_job(auto)
        auto.next_run_at = next_run
    except Exception:
        pass

    db.commit()
    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/run")
def automations_trigger_run(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    execution_id = run_automation_job(auto.id, trigger_type="manual")

    if "HX-Request" in request.headers:
        # Retorno simplificado para HTMX
        return HTMLResponse(
            content=f"""
            <div class="p-3 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-900 text-emerald-800 dark:text-emerald-300 rounded-lg text-sm flex justify-between items-center mb-4">
                <span>Disparo iniciado com sucesso! Execução #{execution_id}.</span>
                <a href="/executions/{execution_id}" class="underline font-semibold ml-2">Ver Logs</a>
            </div>
            """
        )

    return RedirectResponse(url=f"/executions/{execution_id}" if execution_id else f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/sync-git")
def automations_sync_git(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto or auto.source_type != "git" or not auto.git_url:
        raise HTTPException(status_code=400, detail="Automação não configurada para Git")

    auto_dir = get_automation_dir(auto.id)
    repo_dir = auto_dir / "repo"
    msg = ""
    is_error = False

    try:
        commit_sha = clone_or_update_repo(repo_dir, auto.git_url, auto.git_branch or "main", auto.git_token)
        auto.current_commit = commit_sha
        db.commit()
        # Atualiza/instala dependências de pyproject.toml ou requirements.txt
        ensure_venv(auto.id, repo_dir)
        msg = f"Repositório sincronizado no commit {commit_sha[:8]} e dependências instaladas com sucesso!"
    except GitError as e:
        msg = f"Erro na sincronização Git: {str(e)}"
        is_error = True
    except Exception as e:
        msg = f"Aviso nas dependências: {str(e)}"
        is_error = True

    if "HX-Request" in request.headers:
        bg_color = "bg-rose-50 dark:bg-rose-950/40 border-rose-200 dark:border-rose-900 text-rose-800 dark:text-rose-300" if is_error else "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900 text-emerald-800 dark:text-emerald-300"
        return HTMLResponse(content=f'<div class="p-3 {bg_color} border rounded-lg text-sm mb-4">{msg}</div>')

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/rebuild-venv")
def automations_rebuild_venv(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    success, message = rebuild_automation_venv(auto.id)

    if "HX-Request" in request.headers:
        bg_color = "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900 text-emerald-800 dark:text-emerald-300" if success else "bg-rose-50 dark:bg-rose-950/40 border-rose-200 dark:border-rose-900 text-rose-800 dark:text-rose-300"
        return HTMLResponse(content=f'<div class="p-3 {bg_color} border rounded-lg text-sm mb-4">{message}</div>')

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/clean-workspace")
def automations_clean_workspace(
    automation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    success, message = clean_automation_workspace(auto.id)

    if "HX-Request" in request.headers:
        bg_color = "bg-emerald-50 dark:bg-emerald-950/40 border-emerald-200 dark:border-emerald-900 text-emerald-800 dark:text-emerald-300" if success else "bg-rose-50 dark:bg-rose-950/40 border-rose-200 dark:border-rose-900 text-rose-800 dark:text-rose-300"
        return HTMLResponse(content=f'<div class="p-3 {bg_color} border rounded-lg text-sm mb-4">{message}</div>')

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/env")
def automations_save_env(
    automation_id: int,
    request: Request,
    env_vars: Optional[str] = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    auto.env_vars = env_vars.strip() if env_vars else ""
    db.commit()

    # Atualiza o arquivo .env no diretório de trabalho imediatamente
    try:
        from app.services.executor import parse_env_vars
        auto_dir = get_automation_dir(auto.id)
        working_dir = auto_dir / "repo" if auto.source_type == "git" else auto_dir
        parsed = parse_env_vars(auto.env_vars)
        env_file = working_dir / ".env"
        env_lines = [f"{k}={v}" for k, v in parsed.items()]
        env_file.write_text("\n".join(env_lines) + "\n" if env_lines else "", encoding="utf-8")
        count = len(parsed)
        msg = f"Variáveis (.env) salvas com sucesso! {count} variável(is) ativa(s)."
    except Exception as e:
        msg = f"Variáveis salvas no banco, mas erro ao escrever arquivo físico: {e}"

    if "HX-Request" in request.headers:
        return HTMLResponse(
            content=f'<div class="p-3 bg-emerald-50 dark:bg-emerald-950/40 border border-emerald-200 dark:border-emerald-900 text-emerald-800 dark:text-emerald-300 rounded-lg text-sm mb-4">{msg}</div>'
        )

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/toggle-status")
def automations_toggle_status(
    automation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if not auto:
        raise HTTPException(status_code=404, detail="Automação não encontrada")

    if auto.status == "active":
        auto.status = "paused"
    elif auto.status == "paused":
        auto.status = "active"
    elif auto.status == "disabled":
        auto.status = "active"

    next_run = sync_automation_job(auto)
    auto.next_run_at = next_run
    db.commit()

    return RedirectResponse(url=f"/automations/{auto.id}", status_code=status.HTTP_302_FOUND)

@router.post("/automations/{automation_id}/delete")
def automations_delete(
    automation_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    auto = db.query(Automation).filter(Automation.id == automation_id).first()
    if auto:
        remove_automation_job(auto.id)
        db.delete(auto)
        db.commit()
    return RedirectResponse(url="/automations", status_code=status.HTTP_302_FOUND)

# ----------------- EXECUÇÕES E LOGS -----------------

@router.get("/executions", response_class=HTMLResponse)
def executions_list(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    executions = db.query(Execution).order_by(desc(Execution.id)).limit(50).all()
    return templates.TemplateResponse(
        request=request,
        name="executions/list.html",
        context={"user": user, "executions": executions}
    )

@router.get("/executions/{execution_id}", response_class=HTMLResponse)
def executions_detail(
    execution_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    return templates.TemplateResponse(
        request=request,
        name="executions/detail.html",
        context={"user": user, "execution": execution}
    )

@router.get("/executions/{execution_id}/logs")
def executions_get_logs(
    execution_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        return HTMLResponse("<p>Execução não encontrada.</p>")

    is_running = execution.status in ["PENDING", "RUNNING"]
    poll_attr = 'hx-get="/executions/' + str(execution_id) + '/logs" hx-trigger="every 2s" hx-swap="outerHTML"' if is_running else ''

    status_badge = {
        "SUCCESS": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-emerald-100 dark:bg-emerald-950/60 text-emerald-800 dark:text-emerald-300">Sucesso</span>',
        "FAILED": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-rose-100 dark:bg-rose-950/60 text-rose-800 dark:text-rose-300">Falha</span>',
        "TIMEOUT": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-amber-100 dark:bg-amber-950/60 text-amber-800 dark:text-amber-300">Timeout</span>',
        "RUNNING": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-blue-100 dark:bg-blue-950/60 text-blue-800 dark:text-blue-300 animate-pulse">Executando</span>',
        "PENDING": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-zinc-100 dark:bg-zinc-800 text-zinc-800 dark:text-zinc-300">Pendente</span>',
        "CANCELLED": '<span class="px-2.5 py-0.5 rounded-full text-xs font-semibold bg-zinc-200 dark:bg-zinc-800 text-zinc-700 dark:text-zinc-400">Cancelado</span>',
    }.get(execution.status, execution.status)

    duration_text = f"{execution.duration_seconds}s" if execution.duration_seconds is not None else "-"
    log_content = execution.logs if execution.logs else "(Nenhum log gerado ainda)"

    html = f"""
    <div id="execution-logs-container" {poll_attr}>
        <div class="flex items-center justify-between pb-3 border-b border-zinc-200 dark:border-zinc-800 mb-3">
            <div class="flex items-center space-x-3">
                <span class="text-sm font-medium text-zinc-600 dark:text-zinc-400">Status atual:</span>
                {status_badge}
                <span class="text-xs text-zinc-500 dark:text-zinc-400">Duração: {duration_text}</span>
            </div>
            <div>
                {f'<button hx-post="/executions/{execution.id}/cancel" hx-target="#execution-logs-container" class="px-3 py-1 bg-rose-600 text-white rounded text-xs hover:bg-rose-700 transition">Cancelar Execução</button>' if is_running else ''}
            </div>
        </div>
        <pre class="bg-zinc-950 border border-zinc-800 text-zinc-100 font-mono text-xs p-4 rounded-lg overflow-x-auto whitespace-pre-wrap leading-relaxed max-h-[600px] select-text">{log_content}</pre>
    </div>
    """
    return HTMLResponse(content=html)

@router.post("/executions/{execution_id}/cancel")
def executions_cancel(
    execution_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    execution = db.query(Execution).filter(Execution.id == execution_id).first()
    if not execution:
        raise HTTPException(status_code=404, detail="Execução não encontrada")

    if execution.status in ["PENDING", "RUNNING"]:
        cancel_execution(execution_id)
        execution.status = "CANCELLED"
        execution.error_message = "Execução cancelada pelo usuário."
        execution.finished_at = datetime.now(timezone.utc)
        if execution.started_at:
            execution.duration_seconds = round((execution.finished_at - execution.started_at).total_seconds(), 2)
        execution.logs += "\n[EXECUÇÃO CANCELADA PELO USUÁRIO]"
        db.commit()

    return RedirectResponse(url=f"/executions/{execution_id}", status_code=status.HTTP_302_FOUND)
