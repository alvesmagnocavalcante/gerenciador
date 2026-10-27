from typing import Optional
from fastapi import Request, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
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
