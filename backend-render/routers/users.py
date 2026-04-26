"""Users routes — with branch, section, semester, face registration."""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.security import get_current_user, hash_password, require_roles
from db.database import get_db
from models.models import User, UserRole, UserStatus
from schemas.schemas import FaceRegisterRequest, UserCreate, UserListOut, UserOut, UserStatusUpdate, UserUpdate

router = APIRouter()
AdminOnly      = require_roles(UserRole.admin)
AdminOrFaculty = require_roles(UserRole.admin, UserRole.faculty)


@router.get("", response_model=UserListOut)
def list_users(
    role: Optional[str] = Query(None), status_: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = Query(None), branch: Optional[str] = Query(None),
    section: Optional[str] = Query(None),
    skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
    _: User = Depends(AdminOrFaculty), db: Session = Depends(get_db),
):
    q = db.query(User)
    if role:    q = q.filter(User.role == role)
    if status_: q = q.filter(User.status == status_)
    if branch:  q = q.filter(User.branch == branch)
    if section: q = q.filter(User.section == section)
    if search:
        like = f"%{search}%"
        q = q.filter(User.full_name.ilike(like) | User.email.ilike(like) | User.inst_id.ilike(like))
    total = q.count()
    users = q.order_by(User.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "users": users}


@router.post("", response_model=UserOut, status_code=201)
def admin_create_user(payload: UserCreate, _: User = Depends(AdminOnly), db: Session = Depends(get_db)):
    existing = db.query(User).filter(
        (User.inst_id == payload.inst_id) | (User.email == payload.email)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="User with this ID or email already exists.")
    dept_val = payload.department or payload.branch or ''
    new_user = User(
        full_name=payload.full_name, inst_id=payload.inst_id, email=payload.email,
        role=payload.role, status=UserStatus.active,
        hashed_password=hash_password(payload.password),
        department=dept_val,
        branch=payload.branch or payload.department or '',
        section=payload.section, semester=payload.semester,
        course=getattr(payload, 'course_type', None),
    )
    db.add(new_user); db.commit(); db.refresh(new_user)
    return new_user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    data = payload.model_dump(exclude_none=True)
    for field, value in data.items():
        if hasattr(user, field):
            setattr(user, field, value)
    # Keep department in sync with branch
    if 'branch' in data:
        user.department = data['branch']
    elif 'department' in data:
        user.branch = data['department']
    user.updated_at = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.patch("/{user_id}/status", response_model=UserOut)
def update_status(user_id: int, payload: UserStatusUpdate, _: User = Depends(AdminOnly), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    user.status = payload.status; user.updated_at = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.post("/{user_id}/register-face", response_model=UserOut)
def register_face(
    user_id: int, payload: FaceRegisterRequest,
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Store face image + descriptor for a student. Student can register their own face."""
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    user.face_image_b64  = payload.image_b64
    user.face_embedding  = payload.face_descriptor  # store 128-float JSON array
    user.face_registered = True
    user.updated_at      = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, current_admin: User = Depends(AdminOnly), db: Session = Depends(get_db)):
    if user_id == current_admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    db.delete(user); db.commit()
