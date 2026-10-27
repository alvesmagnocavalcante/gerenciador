import hmac
import hashlib
import secrets
import re
from typing import Optional
from app.core.config import settings

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

def sign_session(username: str) -> str:
    """Cria uma assinatura HMAC para a sessão do usuário."""
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{username}:{signature}"

def verify_session(token: Optional[str]) -> Optional[str]:
    """Verifica e extrai o username da sessão se a assinatura for válida."""
    if not token or ":" not in token:
        return None
    try:
        username, signature = token.split(":", 1)
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
