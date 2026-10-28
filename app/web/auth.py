import hmac
from typing import Optional
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.core.security import verify_session
from app.models.user import User

COOKIE_NAME = "pyautomation_session"

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get(COOKIE_NAME)
    username = verify_session(token)
    if not username:
        return None
    return db.query(User).filter(User.username == username).first()

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/login"}
        )
    return user

def get_api_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Autentica requisições da API REST via cookie de sessão ou cabeçalho X-API-Key / Bearer."""
    user = get_current_user_optional(request, db)
    if user:
        return user

    # Verifica X-API-Key ou Authorization: Bearer se settings.API_KEY estiver configurado
    if settings.API_KEY:
        provided_key = request.headers.get("X-API-Key")
        if not provided_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided_key = auth_header[7:].strip()
        if provided_key and hmac.compare_digest(provided_key, settings.API_KEY):
            return db.query(User).filter(User.is_admin == True).first()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Não autenticado. Faça login ou forneça uma X-API-Key válida."
    )
