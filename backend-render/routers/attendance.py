"""Attendance routes — real GPS check + 10-min edit window."""
import math
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import (AttendanceMethod, AttendanceRecord, AttendanceStatus,
                            Session, SessionStatus, SystemSettings, User, UserRole)
from schemas.schemas import AttendanceListOut, AttendanceMarkManual, AttendanceMarkQR, AttendanceOut

router = APIRouter()


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = math.radians(float(lat2)-float(lat1))
    dl = math.radians(float(lon2)-float(lon1))
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def get_settings(db):
    s = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    return s or SystemSettings()


def check_edit_window(session: Session, db: DBSession):
    """Raises 403 if manual edit window has passed."""
    if session.status == SessionStatus.active:
        return  # still live — always editable
    if session.status == SessionStatus.closed and session.ended_at:
        settings = get_settings(db)
        window = settings.manual_edit_window if hasattr(settings, 'manual_edit_window') else 10
        deadline = session.ended_at + timedelta(minutes=window)
        if datetime.utcnow() > deadline:
            raise HTTPException(
                status_code=403,
                detail=f"Edit window closed. Attendance can only be edited up to {window} minutes after session ends."
            )


@router.post("/qr-gps-face", response_model=AttendanceOut, status_code=201)
def mark_full_flow(
    payload: AttendanceMarkQR,
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    session = db.query(Session).filter(
        Session.qr_token == payload.qr_token,
        Session.status   == SessionStatus.active
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Invalid or expired QR code.")

    # Sub-section check — if session has a sub_section set (lab batch),
    # only students whose course matches that sub_section can mark attendance
    if session.sub_section:
        student_subsec = (current_user.course or "").strip().upper()
        session_subsec = session.sub_section.strip().upper()
        if student_subsec != session_subsec:
            raise HTTPException(
                status_code=403,
                detail=f"This lab session is for batch {session.sub_section} only. Your batch is {current_user.course or 'not set'}."
            )

    # GPS check — server-side
    if session.gps_lat and session.gps_lng:
        if not payload.student_lat or not payload.student_lng:
            raise HTTPException(status_code=400, detail="GPS coordinates required.")
        try:
            dist    = haversine(session.gps_lat, session.gps_lng,
                                payload.student_lat, payload.student_lng)
            allowed = get_settings(db).gps_range or 50
            if dist > allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"You are {int(dist)}m away. Must be within {allowed}m of classroom."
                )
        except HTTPException:
            raise
        except Exception:
            pass

    existing = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == session.id,
        AttendanceRecord.student_id == current_user.id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Attendance already marked.")

    att_status = AttendanceStatus.present
    if session.started_at:
        if datetime.utcnow() > session.started_at + timedelta(minutes=session.grace_minutes):
            att_status = AttendanceStatus.late

    record = AttendanceRecord(
        session_id  = session.id,
        student_id  = current_user.id,
        method      = AttendanceMethod.qr_gps_face,
        status      = att_status,
        student_lat = payload.student_lat,
        student_lng = payload.student_lng,
    )
    db.add(record); db.commit(); db.refresh(record)
    return record


@router.post("/manual", response_model=AttendanceOut, status_code=201)
def mark_manual(
    payload: AttendanceMarkManual,
    current_user: User = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    session = db.query(Session).filter(Session.id == payload.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Check faculty owns this session
    if current_user.role == UserRole.faculty and session.faculty_id != current_user.id:
        raise HTTPException(status_code=403, detail="This session belongs to another faculty.")

    # Check 10-minute edit window
    check_edit_window(session, db)

    existing = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == payload.session_id,
        AttendanceRecord.student_id == payload.student_id
    ).first()
    if existing:
        existing.status = payload.status
        existing.method = AttendanceMethod.manual
        existing.notes  = payload.notes
        db.commit(); db.refresh(existing)
        return existing

    record = AttendanceRecord(
        session_id=payload.session_id, student_id=payload.student_id,
        method=AttendanceMethod.manual, status=payload.status, notes=payload.notes,
    )
    db.add(record); db.commit(); db.refresh(record)
    return record


@router.get("/session/{session_id}", response_model=AttendanceListOut)
def session_attendance(
    session_id: int,
    _: User = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    records = db.query(AttendanceRecord).filter(AttendanceRecord.session_id == session_id).all()
    return {"total": len(records), "records": records}


@router.get("/student/{student_id}", response_model=AttendanceListOut)
def student_history(
    student_id: int,
    course_id:  Optional[int] = None,
    skip: int = 0, limit: int = 200,
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if current_user.role == UserRole.student and current_user.id != student_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    q = db.query(AttendanceRecord).filter(AttendanceRecord.student_id == student_id)
    if course_id:
        q = q.join(Session, AttendanceRecord.session_id == Session.id).filter(Session.course_id == course_id)
    total   = q.count()
    records = q.order_by(AttendanceRecord.marked_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "records": records}
