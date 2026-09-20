
"""Session routes — faculty ends live sessions only."""
import secrets, time
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles, build_qr_payload, current_qr_bucket
from db.database import get_db
from models.models import Session, SessionStatus, SystemSettings, UserRole
from schemas.schemas import QRLiveOut, SessionListOut, SessionOut

router = APIRouter()
FacultyOrAdmin = require_roles(UserRole.faculty, UserRole.admin)


def _mask_qr_for(session_or_list, current_user):
    """BUG FIX (pre-existing, not introduced by the QR-rotation feature):
    SessionOut.qr_token was returned to EVERY caller, including students —
    meaning a student could read the classroom's live QR value straight
    out of GET /sessions/active's JSON response, no scanning required at
    all. That defeats "you must be scanning the projected code" as a
    control entirely, independent of whether the token itself rotates.
    Only faculty (their own sessions) and admins get to see it.
    Builds a SessionOut copy rather than mutating the ORM object in
    place — mutating s.qr_token directly would mark it dirty on the
    SQLAlchemy session and risk a stray db.commit() elsewhere in the
    same request silently wiping out the real token in the database."""
    items = session_or_list if isinstance(session_or_list, list) else [session_or_list]
    out = []
    for s in items:
        so = SessionOut.model_validate(s)
        is_owner_faculty = current_user.role == UserRole.faculty and getattr(s, "faculty_id", None) == current_user.inst_id
        if not (current_user.role == UserRole.admin or is_owner_faculty):
            so.qr_token = None
        out.append(so)
    return out if isinstance(session_or_list, list) else out[0]


@router.get("", response_model=SessionListOut)
def list_sessions(
    course_id:  Optional[str] = None,
    faculty_id: Optional[str] = None,
    status_:    Optional[str] = Query(None, alias="status"),
    branch:     Optional[str] = None,
    section:    Optional[str] = None,
    skip: int = 0, limit: int = 100,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    q = db.query(Session)
    if current_user.role == UserRole.faculty:
        q = q.filter(Session.faculty_id == current_user.inst_id)
    elif faculty_id:
        q = q.filter(Session.faculty_id == faculty_id)
    if course_id: q = q.filter(Session.course_id == course_id)
    if status_:   q = q.filter(Session.status == status_)
    if branch:
        import re as _re
        b_raw  = branch.strip()
        b_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b_raw).strip()
        q = q.filter(or_(
            Session.branch == None, Session.branch == '',
            Session.branch.ilike(b_raw),
            Session.branch.ilike(f'%{b_core}%'),
            Session.branch.ilike(f'%{b_raw}%'),
        ))
    if section:
        q = q.filter(or_(
            Session.section == None, Session.section == '',
            func.upper(Session.section) == section.strip().upper(),
        ))
    total    = q.count()
    sessions = q.order_by(Session.scheduled_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "sessions": _mask_qr_for(sessions, current_user)}


@router.get("/active", response_model=list[SessionOut])
def get_active(
    branch:  Optional[str] = None,
    section: Optional[str] = None,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    from sqlalchemy import func, or_
    import re
    q = db.query(Session).filter(Session.status == SessionStatus.active)

    if branch:
        # Flexible matching: handle "CSE(AI-ML-DL)" vs "B.Tech CSE (AI-ML-DL)"
        b = branch.strip().lower()
        b_core = re.sub(r'^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b, flags=re.IGNORECASE).strip()
        q = q.filter(or_(
            Session.branch == None,
            Session.branch == '',
            func.lower(Session.branch) == b,
            Session.branch.ilike(f'%{b_core}%'),
            Session.branch.ilike(f'%{b}%'),
        ))

    if section:
        sec = section.strip().upper()
        q = q.filter(or_(
            Session.section == None,
            Session.section == '',
            func.upper(Session.section) == sec,
        ))

    # If a student is asking, also narrow to their lab batch — a session tied
    # to a specific sub_section (e.g. a parallel lab batch) shouldn't show as
    # "live" for students in a different batch. Sessions with no sub_section
    # set are for everyone and always pass through.
    if current_user.role == UserRole.student:
        student_subsec = (current_user.sub_section or "").strip().upper()
        q = q.filter(or_(
            Session.sub_section == None,
            Session.sub_section == '',
            func.upper(Session.sub_section) == student_subsec,
        ))

    # Also only return sessions that are for this specific section
    # (not sessions from other classes accidentally leaking through)
    results = q.all()
    return _mask_qr_for(results, current_user)


@router.get("/active/mine", response_model=list[SessionOut])
def get_my_active_sessions(
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """Get active sessions specifically for the logged-in student's class."""
    from sqlalchemy import func, or_
    import re
    q = db.query(Session).filter(Session.status == SessionStatus.active)
    branch  = current_user.branch or current_user.department or ''
    section = current_user.section or ''
    if branch:
        b = branch.strip().lower()
        b_core = re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b).strip()
        q = q.filter(or_(
            Session.branch == None, Session.branch == '',
            func.lower(Session.branch) == b,
            Session.branch.ilike(f'%{b_core}%'),
        ))
    if section:
        sec = section.strip().upper()
        q = q.filter(or_(
            Session.section == None, Session.section == '',
            func.upper(Session.section) == sec,
        ))
    student_subsec = (current_user.sub_section or "").strip().upper()
    q = q.filter(or_(
        Session.sub_section == None, Session.sub_section == '',
        func.upper(Session.sub_section) == student_subsec,
    ))
    return _mask_qr_for(q.all(), current_user)


@router.get("/{session_id}", response_model=SessionOut)
def get_session(session_id: int, current_user = Depends(get_current_user), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404, detail="Session not found.")
    return _mask_qr_for(s, current_user)


@router.post("/{session_id}/end", response_model=SessionOut)
def end_session(session_id: int, current_user = Depends(FacultyOrAdmin), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404)
    if current_user.role != UserRole.admin and s.faculty_id != current_user.inst_id:
        raise HTTPException(status_code=403)
    if s.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session is not active.")
    s.status   = SessionStatus.closed
    s.ended_at = datetime.utcnow()
    s.qr_token = None
    db.commit(); db.refresh(s)
    return s


@router.post("/{session_id}/refresh-qr", response_model=SessionOut)
def refresh_qr(session_id: int, current_user = Depends(FacultyOrAdmin), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s or s.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session not active.")
    if current_user.role != UserRole.admin and s.faculty_id != current_user.inst_id:
        raise HTTPException(status_code=403)
    s.qr_token = secrets.token_urlsafe(16)
    db.commit(); db.refresh(s)
    return s


# ── NEW: rotating QR — faculty's projector page polls this ──────────────
# Returns what should actually go INTO the QR code image right now, and
# how many seconds until it changes. s.qr_token itself never changes on
# this call — it's the SEED the rotating payload is derived from; only
# /refresh-qr rotates the seed (e.g. if faculty suspects it's been shared).
@router.get("/{session_id}/qr-live", response_model=QRLiveOut)
def qr_live(session_id: int, current_user = Depends(FacultyOrAdmin), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s or s.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session not active.")
    if current_user.role != UserRole.admin and s.faculty_id != current_user.inst_id:
        raise HTTPException(status_code=403)
    if not s.qr_token:
        raise HTTPException(status_code=400, detail="No QR seed set for this session.")
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    rotate_every = (settings.qr_expiry if settings and settings.qr_expiry else 45)
    bucket = current_qr_bucket(rotate_every)
    seconds_into_bucket = int(time.time()) % max(rotate_every, 5)
    return QRLiveOut(
        session_id=s.id,
        qr_payload=build_qr_payload(s.qr_token, s.id, rotate_every),
        rotates_every=rotate_every,
        seconds_remaining=max(rotate_every, 5) - seconds_into_bucket,
    )


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: int, _ = Depends(require_roles(UserRole.admin)), db: DBSession = Depends(get_db)):
    s = db.query(Session).filter(Session.id == session_id).first()
    if not s: raise HTTPException(status_code=404)
    db.delete(s); db.commit()

# ── Extra / One-Time Class ──────────────────────────────────────────────────
from pydantic import BaseModel

class ExtraClassRequest(BaseModel):
    course_id:    str
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
    current_user = Depends(FacultyOrAdmin),
    db: DBSession = Depends(get_db),
):
    """
    Faculty creates a one-time extra class (not in timetable).
    Session is immediately ACTIVE with a QR code.
    Not permanently added to timetable.
    """
    # BUG FIX: course_id was never checked before insert — a bad/typo'd
    # course_id hit Session's foreign key at commit time and surfaced as
    # an unhandled 500 instead of a clear error.
    from models.models import Course
    if not db.query(Course).filter(Course.id == payload.course_id).first():
        raise HTTPException(status_code=404, detail=f"Course '{payload.course_id}' not found.")
    now = datetime.utcnow()
    s = Session(
        course_id     = payload.course_id,
        faculty_id    = current_user.inst_id,
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

    # NEW FEATURE: same auto-notify as timetable.py's go_live() — an
    # ad-hoc extra class is just as "live now, mark attendance" as a
    # scheduled one, so it gets the same treatment.
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not settings or settings.auto_notify_on_go_live is not False:  # None == default-on
        try:
            from routers.notifications import notify_students_session_live
            notify_students_session_live(db, s)
        except Exception:
            pass

    return s


# ── Admin Session Management ───────────────────────────────────────────────────
@router.post("/admin/end-all-stuck")
def end_all_stuck_sessions(
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """End all sessions that have been live for more than 4 hours (stuck sessions)."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=4)
    stuck = db.query(Session).filter(
        Session.status     == SessionStatus.active,
        Session.started_at <= cutoff,
    ).all()
    for s in stuck:
        s.status   = SessionStatus.closed
        s.ended_at = datetime.utcnow()
        s.qr_token = None
    db.commit()
    return {"ended": len(stuck), "message": f"Ended {len(stuck)} stuck sessions."}


@router.get("/admin/stats")
def admin_session_stats(
    branch:  Optional[str] = None,
    section: Optional[str] = None,
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Admin overview of sessions — total, active, closed."""
    from models.models import AttendanceRecord
    q = db.query(Session)
    if branch:  q = q.filter(Session.branch.ilike(f'%{branch}%'))
    if section: q = q.filter(Session.section == section)
    total  = q.count()
    active = q.filter(Session.status == SessionStatus.active).count()
    closed = q.filter(Session.status == SessionStatus.closed).count()
    total_att = db.query(AttendanceRecord).count()
    return {
        "total_sessions":    total,
        "active_sessions":   active,
        "closed_sessions":   closed,
        "total_attendance":  total_att,
    }
