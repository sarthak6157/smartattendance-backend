"""Session routes — faculty ends live sessions only."""
import secrets
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import Session, SessionStatus, User, UserRole
from schemas.schemas import SessionListOut, SessionOut

router = APIRouter()
FacultyOrAdmin = require_roles(UserRole.faculty, UserRole.admin)


@router.get("", response_model=SessionListOut)
def list_sessions(
    course_id:  Optional[int] = None,
    faculty_id: Optional[int] = None,
    status_:    Optional[str] = Query(None, alias="status"),
    branch:     Optional[str] = None,
    section:    Optional[str] = None,
    skip: int = 0, limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    q = db.query(Session)
    if current_user.role == UserRole.faculty:
        q = q.filter(Session.faculty_id == current_user.id)
    elif faculty_id:
        q = q.filter(Session.faculty_id == faculty_id)
    if course_id: q = q.filter(Session.course_id == course_id)
    if status_:   q = q.filter(Session.status == status_)
    if branch:    q = q.filter(Session.branch == branch)
    if section:   q = q.filter(Session.section == section)
    total    = q.count()
    sessions = q.order_by(Session.scheduled_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "sessions": sessions}


@router.get("/active", response_model=list[SessionOut])
def get_active(
    branch:  Optional[str] = None,
    section: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    q = db.query(Session).filter(Session.status == SessionStatus.active)
    if branch:  q = q.filter(Session.branch  == branch)
    if section: q = q.filter(Session.section == section)
    return q.all()


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: int, _: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404, detail="Session not found.")
    return s


@router.post("/{session_id}/end", response_model=SessionOut)
def end_session(session_id: int, current_user: User = Depends(FacultyOrAdmin), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404)
    if current_user.role != UserRole.admin and s.faculty_id != current_user.id:
        raise HTTPException(status_code=403)
    if s.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session is not active.")
    s.status   = SessionStatus.closed
    s.ended_at = datetime.utcnow()
    s.qr_token = None
    db.commit(); db.refresh(s)
    return s


@router.post("/{session_id}/refresh-qr", response_model=SessionOut)
def refresh_qr(session_id: int, current_user: User = Depends(FacultyOrAdmin), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s or s.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session not active.")
    if current_user.role != UserRole.admin and s.faculty_id != current_user.id:
        raise HTTPException(status_code=403)
    s.qr_token = secrets.token_urlsafe(16)
    db.commit(); db.refresh(s)
    return s


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: int, _: User = Depends(require_roles(UserRole.admin)), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404)
    db.delete(s); db.commit()

# ── Extra / One-Time Class ──────────────────────────────────────────────────
from pydantic import BaseModel

class ExtraClassRequest(BaseModel):
    course_id:    int
    title:        str
    location:     str = ""
    branch:       str = ""
    section:      str = ""
    grace_minutes: int = 15
    gps_lat:      str = ""
    gps_lng:      str = ""

@router.post("/extra", response_model=SessionOut, status_code=201)
def create_extra_class(
    payload: ExtraClassRequest,
    current_user: User = Depends(FacultyOrAdmin),
    db: DBSession = Depends(get_db),
):
    """
    Faculty creates a one-time extra class (not in timetable).
    Session is immediately ACTIVE with a QR code.
    Not permanently added to timetable.
    """
    now = datetime.utcnow()
    s = Session(
        course_id     = payload.course_id,
        faculty_id    = current_user.id,
        timetable_id  = None,           # ← no timetable link = one-time only
        title         = payload.title or "Extra Class",
        location      = payload.location or "",
        branch        = payload.branch or "",
        section       = payload.section or "",
        gps_lat       = payload.gps_lat or None,
        gps_lng       = payload.gps_lng or None,
        status        = SessionStatus.active,
        scheduled_at  = now,
        started_at    = now,
        grace_minutes = payload.grace_minutes,
        qr_token      = secrets.token_urlsafe(16),
    )
    db.add(s); db.commit(); db.refresh(s)
    return s
