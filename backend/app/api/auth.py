from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.deps import get_current_user, require_db_ready
from app.core.security import create_access_token, verify_password
from app.database import get_db
from app.models import User
from app.schemas import AdminSetupRequest, LoginRequest, SetupStatusResponse, Token, UserResponse
from app.seeds.seed_data import ensure_admin_user, seed_database
from app.services.admin_setup import is_admin_setup_needed, persist_admin_env, reload_settings

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/setup-status", response_model=SetupStatusResponse)
def setup_status(_: None = Depends(require_db_ready), db: Session = Depends(get_db)):
    return SetupStatusResponse(needs_setup=is_admin_setup_needed(db))


@router.post("/setup")
def complete_setup(data: AdminSetupRequest, _: None = Depends(require_db_ready), db: Session = Depends(get_db)):
    if not is_admin_setup_needed(db):
        raise HTTPException(status_code=403, detail="Admin already configured")
    email = str(data.email).strip().lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(status_code=400, detail="Email already in use")
    persist_admin_env(email, data.password)
    reload_settings()
    seed_database(db, email, data.password)
    return {"message": "Admin configured successfully"}


@router.post("/login", response_model=Token)
def login(data: LoginRequest, _: None = Depends(require_db_ready), db: Session = Depends(get_db)):
    settings = get_settings()
    email = str(data.email).strip().lower()
    password = str(data.password or "")
    user = db.query(User).filter(func.lower(User.email) == email).first()

    env_email = (settings.ADMIN_EMAIL or "").strip().lower()
    env_password = settings.ADMIN_PASSWORD or ""
    env_ok = bool(env_email and env_password and email == env_email and password == env_password)

    if user and verify_password(password, user.hashed_password):
        pass
    elif env_ok:
        # This backend's env credentials always win (local .env vs Render env)
        user = ensure_admin_user(db, env_email, env_password)
    else:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({"sub": user.email, "role": user.role.value})
    return Token(access_token=token)


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user
