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


# ── Bulk Import ────────────────────────────────────────────────────────────────
import csv, io
from fastapi import UploadFile, File

@router.post("/bulk-import", status_code=200)
async def bulk_import_students(
    file: UploadFile = File(...),
    _: User = Depends(AdminOnly),
    db: Session = Depends(get_db),
):
    """
    Import students from a CSV file.
    Required columns: full_name, inst_id, email, password
    Optional columns: branch, section, semester, course_type
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported.")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # utf-8-sig handles Excel BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))

    # Validate required columns
    required = {"full_name", "inst_id", "email", "password"}
    if not reader.fieldnames or not required.issubset(set(f.strip() for f in reader.fieldnames)):
        raise HTTPException(
            status_code=400,
            detail=f"CSV must have columns: {', '.join(required)}. Found: {reader.fieldnames}"
        )

    created, skipped, errors = [], [], []

    for i, row in enumerate(reader, start=2):  # row 1 = header
        row = {k.strip(): v.strip() for k, v in row.items() if k}
        inst_id = row.get("inst_id", "").strip()
        email   = row.get("email", "").strip()
        name    = row.get("full_name", "").strip()
        pwd     = row.get("password", "").strip()

        if not all([inst_id, email, name, pwd]):
            errors.append({"row": i, "reason": "Missing required field", "data": inst_id or email})
            continue

        # Check duplicate
        exists = db.query(User).filter(
            (User.inst_id == inst_id) | (User.email == email)
        ).first()
        if exists:
            skipped.append({"row": i, "inst_id": inst_id, "reason": "Already exists"})
            continue

        branch   = row.get("branch", "")
        section  = row.get("section", "")
        semester = row.get("semester", "")
        course   = row.get("course_type", "")

        try:
            new_user = User(
                full_name       = name,
                inst_id         = inst_id,
                email           = email,
                role            = UserRole.student,
                status          = UserStatus.active,
                hashed_password = hash_password(pwd),
                department      = branch,
                branch          = branch,
                section         = section,
                semester        = semester,
                course          = course,
            )
            db.add(new_user)
            db.flush()  # get ID without committing
            created.append({"row": i, "inst_id": inst_id, "name": name})
        except Exception as e:
            errors.append({"row": i, "reason": str(e), "data": inst_id})

    db.commit()

    return {
        "summary": {
            "total_rows": len(created) + len(skipped) + len(errors),
            "created":    len(created),
            "skipped":    len(skipped),
            "errors":     len(errors),
        },
        "created": created,
        "skipped": skipped,
        "errors":  errors,
    }


@router.get("/bulk-import/template")
def download_template(_: User = Depends(AdminOnly)):
    """Download a sample CSV template for bulk import."""
    sample = (
        "full_name,inst_id,email,password,branch,section,semester,course_type\n"
        "John Doe,2300123456,john@tmu.ac.in,Pass@1234,B.Tech AI,A,2nd,B.Tech\n"
        "Jane Smith,2300123457,jane@tmu.ac.in,Pass@1234,B.Tech AI,A,2nd,B.Tech\n"
        "Rahul Kumar,2300123458,rahul@tmu.ac.in,Pass@1234,B.Tech AI,B,2nd,B.Tech\n"
    )
    from fastapi.responses import Response
    return Response(
        content=sample,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=student_import_template.csv"}
    )
