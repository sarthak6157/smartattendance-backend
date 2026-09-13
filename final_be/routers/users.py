"""Users routes — with branch, section, semester, face registration.

Users now live in three separate tables (students / faculty / admins)
instead of one shared `users` table. Endpoints that already took a `role`
(list, create) use it to pick the right table. Endpoints that only get an
id (get/update/delete/status/etc.) search all three tables in turn — the
id alone doesn't say which table it's in.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from core.security import get_current_user, hash_password, require_roles
from db.database import get_db
from models.models import Student, Faculty, Admin, ROLE_MODEL, UserRole, UserStatus
from schemas.schemas import FaceRegisterRequest, UserCreate, UserListOut, UserOut, UserStatusUpdate, UserUpdate

router = APIRouter()

def _sanitize(value: str, max_len: int = 200) -> str:
    """Strip dangerous characters and truncate input."""
    if not value:
        return value
    # Remove null bytes and control characters
    import re
    value = re.sub(r'[--]', '', value)
    # Truncate
    return value.strip()[:max_len]

AdminOnly      = require_roles(UserRole.admin)
AdminOrFaculty = require_roles(UserRole.admin, UserRole.faculty)

ALL_MODELS = (Student, Faculty, Admin)


def _find_user_anywhere(db: Session, user_id: str):
    """Search all three tables for a matching inst_id. Returns the row or None."""
    for model in ALL_MODELS:
        user = db.query(model).filter(model.inst_id == user_id).first()
        if user:
            return user
    return None


def _id_taken_anywhere(db: Session, inst_id: str, email: str) -> bool:
    """inst_id/email must be unique ACROSS all three tables, not just within
    one — otherwise two different roles could collide on the same id."""
    for model in ALL_MODELS:
        if db.query(model).filter((model.inst_id == inst_id) | (model.email == email)).first():
            return True
    return False


@router.get("", response_model=UserListOut)
def list_users(
    role: Optional[str]     = Query(None),
    status_: Optional[str]  = Query(None, alias="status"),
    search: Optional[str]   = Query(None),
    branch: Optional[str]   = Query(None),
    section: Optional[str]  = Query(None),
    sub_section: Optional[str] = Query(None),
    semester: Optional[str] = Query(None),
    course: Optional[str]   = Query(None),        # e.g. "B.Tech", "MCA"
    face_registered: Optional[bool] = Query(None),
    sort_by: Optional[str]  = Query("created_at", pattern="^(created_at|full_name|inst_id|branch|section|semester)$"),
    sort_dir: Optional[str] = Query("desc", pattern="^(asc|desc)$"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _=Depends(AdminOrFaculty),
    db: Session = Depends(get_db),
):
    """
    List users with comprehensive filters.
    Accessible by Admin and Faculty (faculty sees students only).
    """
    # Role tells us which table to query — faculty is locked to students
    # regardless of what they pass, same as before.
    caller = _
    if caller.role == UserRole.faculty:
        effective_role = UserRole.student
    elif role:
        try:
            effective_role = UserRole(role)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid role.")
    else:
        effective_role = UserRole.student  # default tab

    Model = ROLE_MODEL[effective_role]
    q = db.query(Model)

    if status_:          q = q.filter(Model.status == status_)
    if branch and "branch" in Model.__table__.columns.keys():
        # Flexible match: "CSE(AI-ML-DL)" matches "B.Tech - CSE (AI-ML-DL)" and vice versa
        import re as _re
        b_raw  = branch.strip()
        b_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc|b\.c\.a)[\s\-]+', '', b_raw).strip()
        from sqlalchemy import or_ as _or
        q = q.filter(_or(
            Model.branch.ilike(b_raw),
            Model.branch.ilike(f'%{b_core}%'),
            Model.branch.ilike(f'%{b_raw}%'),
            Model.department.ilike(f'%{b_core}%'),
        ))
    if section and "section" in Model.__table__.columns.keys():
        q = q.filter(Model.section == section)
    if semester and "semester" in Model.__table__.columns.keys():
        q = q.filter(Model.semester == semester)
    if course and "course" in Model.__table__.columns.keys():
        q = q.filter(Model.course.ilike(course))
    if face_registered is not None and "face_registered" in Model.__table__.columns.keys():
        q = q.filter(Model.face_registered == face_registered)

    if search:
        like = f"%{search}%"
        q = q.filter(
            Model.full_name.ilike(like) |
            Model.email.ilike(like)     |
            Model.inst_id.ilike(like)
        )

    # Sorting — only apply if this table actually has the column as a real,
    # sortable mapped column. getattr(Model, sort_by) would otherwise return
    # the stub `property` object for fields Admin/Faculty don't really have
    # (e.g. Admin.branch), and calling .asc()/.desc() on that crashes.
    if sort_by in Model.__table__.columns.keys():
        sort_col = getattr(Model, sort_by)
    else:
        sort_col = Model.created_at
    q = q.order_by(sort_col.asc() if sort_dir == "asc" else sort_col.desc())

    total = q.count()
    users = q.offset(skip).limit(limit).all()
    return {"total": total, "users": users}


@router.get("/filter-options")
def get_filter_options(
    _=Depends(AdminOrFaculty),
    db: Session = Depends(get_db),
):
    """
    Returns distinct values for branch, section, semester, course
    so the frontend can populate filter dropdowns dynamically.
    """
    students = db.query(Student).all()

    def distinct(field):
        vals = sorted({getattr(u, field) for u in students if getattr(u, field)})
        return vals

    return {
        "branches":  distinct("branch"),
        "sections":  distinct("section"),
        "semesters": distinct("semester"),
        "courses":   distinct("course"),
    }


@router.post("", response_model=UserOut, status_code=201)
def admin_create_user(payload: UserCreate, _=Depends(AdminOnly), db: Session = Depends(get_db)):
    if _id_taken_anywhere(db, payload.inst_id, payload.email):
        raise HTTPException(status_code=409, detail="User with this ID or email already exists.")
    dept_val = payload.department or payload.branch or ''
    Model = ROLE_MODEL[payload.role]
    kwargs = dict(
        full_name=payload.full_name, inst_id=payload.inst_id, email=payload.email,
        status=UserStatus.active,
        hashed_password=hash_password(payload.password),
    )
    # Only pass fields the target table actually has — Admin doesn't have
    # department/branch/section/semester/course at all.
    if "department" in Model.__table__.columns.keys(): kwargs["department"] = dept_val
    if "branch" in Model.__table__.columns.keys():     kwargs["branch"]      = payload.branch or payload.department or ''
    if "section" in Model.__table__.columns.keys():    kwargs["section"]     = payload.section
    if "semester" in Model.__table__.columns.keys():   kwargs["semester"]    = payload.semester
    if "course" in Model.__table__.columns.keys():     kwargs["course"]      = getattr(payload, 'course_type', None)
    new_user = Model(**kwargs)
    db.add(new_user); db.commit(); db.refresh(new_user)
    return new_user


@router.get("/{user_id}", response_model=UserOut)
def get_user(user_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != UserRole.admin and current_user.inst_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    user = _find_user_anywhere(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: UserUpdate, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != UserRole.admin and current_user.inst_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    user = _find_user_anywhere(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    data = payload.model_dump(exclude_none=True)
    real_columns = user.__table__.columns.keys()
    for field, value in data.items():
        if field in real_columns:
            setattr(user, field, value)
    # Keep department in sync with branch
    if hasattr(user, "department"):
        if 'branch' in data and 'branch' in real_columns:
            user.department = data['branch']
        elif 'department' in data and 'department' in real_columns:
            user.branch = data['department']
    user.updated_at = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.patch("/{user_id}/status", response_model=UserOut)
def update_status(user_id: str, payload: UserStatusUpdate, _=Depends(AdminOnly), db: Session = Depends(get_db)):
    user = _find_user_anywhere(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    user.status = payload.status; user.updated_at = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.post("/{user_id}/register-face", response_model=UserOut)
def register_face(
    user_id: str, payload: FaceRegisterRequest,
    current_user=Depends(get_current_user), db: Session = Depends(get_db),
):
    """Store face image + descriptor for a student. Student can register their own face."""
    if current_user.role != UserRole.admin and current_user.inst_id != user_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    # Only students have face fields — no point searching faculty/admins.
    user = db.query(Student).filter(Student.inst_id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    user.face_image_b64  = None  # Not stored in DB to save space - only embedding needed
    user.face_embedding  = payload.face_descriptor  # store 128-float JSON array
    user.face_registered = True
    user.updated_at      = datetime.utcnow()
    db.commit(); db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: str, current_admin=Depends(AdminOnly), db: Session = Depends(get_db)):
    if user_id == current_admin.inst_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")
    user = _find_user_anywhere(db, user_id)
    if not user: raise HTTPException(status_code=404, detail="User not found.")
    db.delete(user); db.commit()


# ── Bulk Import ────────────────────────────────────────────────────────────────
import csv, io
from fastapi import UploadFile, File

@router.post("/bulk-import", status_code=200)
async def bulk_import_students(
    file: UploadFile = File(...),
    _=Depends(AdminOnly),
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

        # Check duplicate — across all three tables, not just students
        if _id_taken_anywhere(db, inst_id, email):
            skipped.append({"row": i, "inst_id": inst_id, "reason": "Already exists"})
            continue

        branch   = row.get("branch", "")
        section  = row.get("section", "")
        semester = row.get("semester", "")
        course   = row.get("course_type", "")

        try:
            new_user = Student(
                full_name       = name,
                inst_id         = inst_id,
                email           = email,
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
def download_template(_=Depends(AdminOnly)):
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


# ── Bulk Promote / Semester Change ─────────────────────────────────────────────
from pydantic import BaseModel as _BM

class PromoteRequest(_BM):
    from_branch:   str
    from_section:  str
    from_semester: str
    to_semester:   str
    to_section:    Optional[str] = None   # optional: change section too
    to_branch:     Optional[str] = None   # optional: change branch too

class PromoteOneRequest(_BM):
    semester:    Optional[str] = None
    section:     Optional[str] = None
    branch:      Optional[str] = None
    sub_section: Optional[str] = None
    course:      Optional[str] = None

@router.post("/promote/bulk")
def bulk_promote(
    payload: PromoteRequest,
    _=Depends(AdminOnly),
    db: Session = Depends(get_db),
):
    """
    Bulk promote students to next semester.
    Example: move all Section C 2nd sem → 3rd sem
    """
    from sqlalchemy import func as _func
    q = db.query(Student).filter(
        Student.status  == UserStatus.active,
        _func.lower(Student.section)  == payload.from_section.strip().lower(),
        _func.lower(Student.semester) == payload.from_semester.strip().lower(),
    )
    # Flexible branch match
    import re as _re
    b_raw  = payload.from_branch.strip()
    b_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b_raw).strip()
    from sqlalchemy import or_ as _or
    q = q.filter(_or(
        Student.branch.ilike(b_raw),
        Student.branch.ilike(f'%{b_core}%'),
    ))
    students = q.all()
    if not students:
        return {"promoted": 0, "message": "No students found matching the criteria."}

    for stu in students:
        stu.semester = payload.to_semester
        if payload.to_section: stu.section = payload.to_section
        if payload.to_branch:  stu.branch  = payload.to_branch; stu.department = payload.to_branch
        stu.updated_at = datetime.utcnow()

    db.commit()
    return {
        "promoted":  len(students),
        "message":   f"Successfully promoted {len(students)} students to {payload.to_semester}",
        "students":  [{"id": s.inst_id, "name": s.full_name, "inst_id": s.inst_id} for s in students],
    }


@router.post("/{user_id}/promote")
def promote_one_student(
    user_id: str,
    payload: PromoteOneRequest,
    _=Depends(AdminOnly),
    db: Session = Depends(get_db),
):
    """Promote or update a single student's academic details."""
    user = db.query(Student).filter(Student.inst_id == user_id).first()
    if not user: raise HTTPException(status_code=404, detail="Student not found.")

    if payload.semester:    user.semester    = payload.semester
    if payload.section:     user.section     = payload.section
    if payload.branch:      user.branch      = payload.branch; user.department = payload.branch
    if payload.sub_section: user.sub_section = payload.sub_section
    if payload.course:      user.course      = payload.course
    user.updated_at = datetime.utcnow()
    db.commit(); db.refresh(user)
    return {"message": f"Student {user.full_name} updated successfully.", "user": {
        "id": user.inst_id, "name": user.full_name, "semester": user.semester,
        "section": user.section, "branch": user.branch,
    }}


@router.get("/promote/options")
def get_promote_options(
    _=Depends(AdminOnly),
    db: Session = Depends(get_db),
):
    """Get all unique branch/section/semester combos for the promote form."""
    students = db.query(Student).filter(Student.status == UserStatus.active).all()
    branches  = sorted({s.branch   or '' for s in students if s.branch})
    sections  = sorted({s.section  or '' for s in students if s.section})
    semesters = sorted({s.semester or '' for s in students if s.semester})
    return {"branches": branches, "sections": sections, "semesters": semesters}
