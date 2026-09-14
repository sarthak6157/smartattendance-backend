"""Auth routes: login, register, profile, change-password."""
from datetime import datetime
from collections import defaultdict
from time import time
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from core.security import create_access_token, get_current_user, hash_password, verify_password
from routers.users import _sanitize, SELF_EDITABLE_FIELDS
from db.database import get_db
from models.models import Student, Faculty, Admin, ROLE_MODEL, UserRole, UserStatus
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
    # Students/faculty/admins now live in three separate tables — the
    # role tab the person picked on the login screen tells us which one
    # to search, instead of scanning everyone.
    model = ROLE_MODEL.get(payload.role)
    if model is None:
        raise HTTPException(status_code=400, detail="Invalid role.")
    user = db.query(model).filter(
        (model.email == credential) | (model.inst_id == credential)
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
    # inst_id — NOT the cosmetic id field — is the real identity/login key
    token = create_access_token({"sub": user.inst_id, "role": user.role.value})
    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    """Public registration — always creates a student, status=pending."""
    existing = db.query(Student).filter(
        (Student.inst_id == payload.inst_id) | (Student.email == payload.email)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this ID or email already exists.")
    dept = payload.department or payload.branch or ''
    new_user = Student(
        full_name=_sanitize(payload.full_name, 200),
        inst_id=payload.inst_id,
        email=payload.email,
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
def me(current_user = Depends(get_current_user)):
    return current_user


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # BUG FIX: this used to apply every field in UserUpdate blindly, which
    # includes branch/section/semester/course/department — i.e. a student
    # could PATCH /auth/me and move themselves into a different class,
    # section, or semester with no admin involvement at all (this would
    # then let them see/mark attendance for classes they were never
    # enrolled in). Only admin-managed academic-placement fields go
    # through users.py's admin-only endpoints now; a user editing their
    # own profile can only touch simple contact-info fields (imported from
    # routers.users so both endpoints share one definition).
    data = payload.model_dump(exclude_none=True)
    if "full_name" in data:
        data["full_name"] = _sanitize(data["full_name"], 200)
    if current_user.role != UserRole.admin:
        blocked = set(data) - SELF_EDITABLE_FIELDS
        data = {k: v for k, v in data.items() if k in SELF_EDITABLE_FIELDS}
        if blocked:
            # Not fatal — just ignore the fields the user isn't allowed to
            # change themselves, same as the pre-existing "unknown column"
            # skip behaviour below, so the request doesn't hard-fail on
            # what's usually a client sending its full local profile object.
            pass
    # Not every field applies to every role (e.g. faculty/admin have no
    # `section`) — those are stubbed as read-only properties on the model,
    # so only assign fields that are real mapped columns for this user's
    # table, and silently skip the rest instead of crashing.
    real_columns = current_user.__table__.columns.keys()
    for field, value in data.items():
        if field in real_columns:
            setattr(current_user, field, value)
    current_user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/change-password", status_code=200)
def change_password(
    payload: PasswordChangeRequest,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    current_user.hashed_password = hash_password(payload.new_password)
    current_user.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Password updated successfully."}


# ── Admin: Reset any user's password ─────────────────────────────────────────
from pydantic import BaseModel as _AuthBM
from core.security import require_roles as _req
from models.models import UserRole as _UR

class AdminResetPwd(_AuthBM):
    new_password: str

@router.post("/admin/reset-password/{user_id}", status_code=200)
def admin_reset_password(
    user_id: str,
    payload: AdminResetPwd,
    current_admin = Depends(_req(_UR.admin)),
    db: Session = Depends(get_db),
):
    """Admin resets any user's password — searches all three tables since
    the id alone doesn't say which role it belongs to."""
    user = None
    for model in (Student, Faculty, Admin):
        user = db.query(model).filter(model.inst_id == user_id).first()
        if user:
            break
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    user.hashed_password = hash_password(payload.new_password)
    user.updated_at      = datetime.utcnow()
    db.commit()
    return {"message": f"Password reset successfully for {user.full_name}."}
