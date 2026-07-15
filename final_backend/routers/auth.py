"""Auth routes: login, register, profile, change-password."""
from datetime import datetime
from collections import defaultdict
from time import time
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from core.security import create_access_token, get_current_user, hash_password, verify_password
from db.database import get_db
from models.models import User, UserRole, UserStatus
from schemas.schemas import LoginRequest, PasswordChangeRequest, TokenResponse, UserCreate, UserOut, UserUpdate

router = APIRouter()

# ── Simple in-memory rate limiter: IP → list of attempt timestamps ──────────
_login_attempts: dict = defaultdict(list)
_MAX_ATTEMPTS   = 5
_WINDOW_SECONDS = 300  # 5 minutes

def _check_rate_limit(ip: str):
    now  = time()
    attempts = [t for t in _login_attempts[ip] if now - t < _WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) >= _MAX_ATTEMPTS:
        wait = int(_WINDOW_SECONDS - (now - attempts[0]))
        raise HTTPException(
            status_code=429,
            detail=f"Too many login attempts. Try again in {wait//60+1} minute(s)."
        )
    _login_attempts[ip].append(now)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _check_rate_limit(request.client.host)
    credential = payload.credential.strip()
    user = db.query(User).filter(
        (User.email == credential) | (User.inst_id == credential)
    ).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    if user.status == UserStatus.pending:
        raise HTTPException(status_code=403, detail="Account pending admin approval.")
    if user.status == UserStatus.inactive:
        raise HTTPException(status_code=403, detail="Account is deactivated. Contact admin.")
    user.last_login = datetime.utcnow()
    db.commit()
    # Clear rate limit on successful login
    _login_attempts.pop(request.client.host, None)
    token = create_access_token({"sub": user.id, "role": user.role.value})
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    """Public registration — always creates student, status=pending."""
    existing = db.query(User).filter(
        (User.inst_id == payload.inst_id) | (User.email == payload.email)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this ID or email already exists.")
    dept = payload.department or payload.branch or ''
    new_user = User(
        full_name=payload.full_name,
        inst_id=payload.inst_id,
        email=payload.email,
        role=UserRole.student,
        status=UserStatus.pending,
        hashed_password=hash_password(payload.password),
        department=dept,
        branch=payload.branch or payload.department or '',
        section=getattr(payload, 'section', None),
        semester=getattr(payload, 'semester', None),
        course=getattr(payload, 'course_type', None),
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(current_user, field, value)
    current_user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/change-password", status_code=200)
def change_password(
    payload: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    current_user.hashed_password = hash_password(payload.new_password)
    current_user.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Password updated successfully."}
