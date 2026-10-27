import subprocess
import os
import shutil
from pathlib import Path
from typing import Optional, Dict, Tuple
from app.core.security import sanitize_output
from app.services.executor import safe_rmtree

class GitError(Exception):
    pass

def build_authenticated_url(url: str, token: Optional[str]) -> str:
    """Insere o token na URL https:// caso fornecido."""
    if not token or not url.startswith("https://"):
        return url
    # Ex: https://github.com/user/repo -> https://token@github.com/user/repo
    return url.replace("https://", f"https://{token}@")

def normalize_git_url(url: Optional[str]) -> str:
    """Normaliza URLs git para comparação (remove token, .git no final, protocolo, etc.)."""
    if not url:
        return ""
    u = url.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    import re
    u = re.sub(r"^https?://([^@]+@)?", "", u)
    return u.lower()

def run_git_command(args: list[str], cwd: Optional[Path] = None, timeout: int = 60) -> Tuple[int, str, str]:
    """Executa um comando git sem shell=True de forma segura."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # Evita travar pedindo credenciais no terminal
        )
        stdout = sanitize_output(proc.stdout)
        stderr = sanitize_output(proc.stderr)
        return proc.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        raise GitError("Tempo limite excedido ao executar comando Git.")
    except Exception as e:
        raise GitError(f"Erro ao executar git: {str(e)}")

def clone_or_update_repo(repo_dir: Path, git_url: str, branch: str = "main", token: Optional[str] = None) -> str:
    """Clona ou atualiza um repositório git no diretório de destino e retorna o commit sha."""
    auth_url = build_authenticated_url(git_url, token)

    is_same_repo = False
    if (repo_dir / ".git").is_dir():
        code, current_remote, _ = run_git_command(["remote", "get-url", "origin"], cwd=repo_dir)
        if code == 0 and normalize_git_url(current_remote) == normalize_git_url(git_url):
            is_same_repo = True

    if is_same_repo:
        # Repositório já é o correto: atualizar remote auth_url e sincronizar
        run_git_command(["remote", "set-url", "origin", auth_url], cwd=repo_dir)
        code, _, err = run_git_command(["fetch", "origin", branch], cwd=repo_dir)
        if code != 0:
            safe_rmtree(repo_dir)
            is_same_repo = False
        else:
            run_git_command(["checkout", branch], cwd=repo_dir)
            code, _, err = run_git_command(["pull", "origin", branch], cwd=repo_dir)
            if code != 0:
                safe_rmtree(repo_dir)
                is_same_repo = False
            else:
                code, stdout, _ = run_git_command(["rev-parse", "HEAD"], cwd=repo_dir)
                if code == 0:
                    return stdout.strip()

    if not is_same_repo:
        # Repositório novo, alterado ou divergente: clonar do zero
        if repo_dir.exists():
            safe_rmtree(repo_dir)
        repo_dir.mkdir(parents=True, exist_ok=True)

        code, _, err = run_git_command(["clone", "--branch", branch, "--depth", "1", auth_url, str(repo_dir)])
        if code != 0:
            safe_rmtree(repo_dir)
            raise GitError(f"Falha ao clonar repositório: {err}")

    # Obter o hash do commit atual
    code, stdout, err = run_git_command(["rev-parse", "HEAD"], cwd=repo_dir)
    if code != 0:
        raise GitError(f"Falha ao obter commit SHA: {err}")

    return stdout.strip()
