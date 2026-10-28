import hmac
import hashlib
import secrets
import time
import re
from pathlib import Path
from typing import Optional
from app.core.config import settings

SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 dias

def hash_password(password: str) -> str:
    """Gera hash PBKDF2-HMAC-SHA256 seguro com salt aleatório."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations=100000,
    )
    return f"{salt}${key.hex()}"

def verify_password(stored_password: str, provided_password: str) -> bool:
    """Verifica se a senha fornecida confere com o hash armazenado."""
    if not stored_password or "$" not in stored_password:
        return False
    try:
        salt, expected_hash = stored_password.split("$", 1)
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256",
            provided_password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations=100000,
        ).hex()
        return hmac.compare_digest(actual_hash, expected_hash)
    except Exception:
        return False

def sign_session(username: str, timestamp: Optional[int] = None) -> str:
    """Cria uma assinatura HMAC com timestamp para a sessão do usuário."""
    ts = int(timestamp if timestamp is not None else time.time())
    payload = f"{username}:{ts}"
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"

def verify_session(token: Optional[str], max_age: int = SESSION_MAX_AGE_SECONDS) -> Optional[str]:
    """Verifica assinatura e expiração do token de sessão e extrai o username."""
    if not token or ":" not in token:
        return None
    try:
        parts = token.split(":")
        if len(parts) == 3:
            username, ts_str, signature = parts
            ts = int(ts_str)
            if max_age > 0 and (time.time() - ts) > max_age:
                return None
            payload = f"{username}:{ts_str}"
            expected = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(signature, expected):
                return username
        elif len(parts) == 2:
            username, signature = parts
            expected = hmac.new(
                settings.SECRET_KEY.encode("utf-8"),
                username.encode("utf-8"),
                hashlib.sha256
            ).hexdigest()
            if hmac.compare_digest(signature, expected):
                return username
    except Exception:
        return None
    return None

def resolve_safe_path(base_dir: Path, relative_path: str) -> Path:
    """Resolve um caminho relativo garantindo que permaneça estritamente dentro de base_dir."""
    if not relative_path or not relative_path.strip():
        raise ValueError("Caminho de arquivo vazio ou inválido.")
    base_resolved = base_dir.resolve()
    target_resolved = (base_resolved / relative_path).resolve()
    if not target_resolved.is_relative_to(base_resolved) or target_resolved == base_resolved:
        raise ValueError("Path traversal detectado: o arquivo deve estar dentro do diretório da automação.")
    return target_resolved

# Máscaras de segurança para logs
SENSITIVE_PATTERNS = [
    (re.compile(r'(ghp_[a-zA-Z0-9]{36,})'), r'[TOKEN_REDACTED]'),
    (re.compile(r'(github_pat_[a-zA-Z0-9_]{50,})'), r'[TOKEN_REDACTED]'),
    (re.compile(r'(https?://)([^:\s]+):([^@\s]+)@'), r'\1[CREDENTIALS_REDACTED]@'),
    (re.compile(r'((?:password|token|secret|api_key|auth)\s*[:=]\s*["\']?)([^"\'\s&]{4,})', re.IGNORECASE), r'\1[REDACTED]'),
    (re.compile(r'(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*', re.IGNORECASE), r'\1[TOKEN_REDACTED]'),
]

def sanitize_output(text: str) -> str:
    """Remove tokens, credenciais e chaves sensíveis do texto antes de persistir em logs."""
    if not text:
        return ""
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
