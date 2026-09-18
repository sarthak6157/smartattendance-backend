"""Attendance routes — real GPS check + 10-min edit window."""
import math
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.exc import IntegrityError

from core.security import (get_current_user, require_roles, verify_qr_payload,
                            build_qr_payload, face_descriptor_matches)
from db.database import get_db
from models.models import (AttendanceAuditLog, AttendanceFlag, AttendanceMethod,
                            AttendanceRecord, AttendanceStatus, DeviceCheckin,
                            LeaveRequest, Session, SessionStatus, SystemSettings,
                            Student, UserRole)
from schemas.schemas import (AttendanceFlagOut, AttendanceListOut, AttendanceMarkManual,
                              AttendanceMarkQR, AttendanceOut, DefaulterOut, FlagResolveRequest)

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


def _flag(db, session_id, student_id, reason, severity="warning"):
    db.add(AttendanceFlag(session_id=session_id, student_id=student_id, reason=reason, severity=severity))


def _check_proxy_signals(db: DBSession, session: Session, student_id: str, device_id: Optional[str],
                          lat: Optional[str], lng: Optional[str]):
    """NEW FEATURE: lightweight proxy-attendance heuristics. Never blocks
    the check-in — these raise a review flag for faculty/admin, since any
    single signal (shared wifi, a borrowed phone) can be innocent. Two
    checks:
      1. Same device_id marking multiple DIFFERENT students in the same
         session within a short window — the classic "pass the phone
         around" pattern.
      2. The same student checking in from two GPS points far enough
         apart that they couldn't plausibly have moved between them,
         across recent sessions today — a spoofed/replayed location.
    """
    now = datetime.utcnow()
    if device_id:
        recent = db.query(DeviceCheckin).filter(
            DeviceCheckin.session_id == session.id,
            DeviceCheckin.device_id == device_id,
            DeviceCheckin.created_at >= now - timedelta(minutes=10),
        ).all()
        other_students = {c.student_id for c in recent if c.student_id != student_id}
        if other_students:
            _flag(db, session.id, student_id,
                  f"Same device used to check in for {len(other_students)} other student(s) in this session within 10 minutes.",
                  severity="high")
    if lat and lng:
        try:
            flat, flng = float(lat), float(lng)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            prior = db.query(DeviceCheckin).filter(
                DeviceCheckin.student_id == student_id,
                DeviceCheckin.created_at >= today_start,
                DeviceCheckin.created_at < now,
            ).order_by(DeviceCheckin.created_at.desc()).first()
            if prior and prior.lat and prior.lng:
                dist = haversine(prior.lat, prior.lng, flat, flng)
                minutes = max((now - prior.created_at).total_seconds() / 60, 0.1)
                # ~200 km/h is already generous for "how fast could a
                # person plausibly travel between two check-ins"
                if dist > 1000 and (dist / 1000) / (minutes / 60) > 200:
                    _flag(db, session.id, student_id,
                          f"Checked in {int(dist)}m from a location checked in {int(minutes)} min ago — implausible travel speed.",
                          severity="high")
        except (ValueError, TypeError):
            pass
    db.add(DeviceCheckin(session_id=session.id, student_id=student_id, device_id=device_id, lat=lat, lng=lng))


@router.post("/qr-gps-face", response_model=AttendanceOut, status_code=201)
def mark_full_flow(
    payload: AttendanceMarkQR,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    settings = get_settings(db)
    rotate_seconds = settings.qr_expiry or 45

    # BUG FIX (security): the QR code used to be one static token for the
    # whole session — trivially screenshotted and shared. It's now a
    # rotating "{session_id}:{token}" payload (see core/security.py); this
    # first pulls out the session_id (no trust placed in it yet), loads
    # that session, then verifies the token against ITS real seed.
    session = None
    if ":" in payload.qr_token:
        try:
            sid_str, _tok = payload.qr_token.split(":", 1)
            candidate = db.query(Session).filter(Session.id == int(sid_str), Session.status == SessionStatus.active).first()
        except (ValueError, TypeError):
            candidate = None
        if candidate and candidate.qr_token and verify_qr_payload(candidate.qr_token, payload.qr_token, rotate_seconds) == candidate.id:
            session = candidate
    if not session:
        # Backward-compat: accept a raw static token too (old scanned QR
        # still open in someone's camera roll, or a client that hasn't
        # updated yet), so this rollout doesn't hard-break anyone mid-class.
        session = db.query(Session).filter(
            Session.qr_token == payload.qr_token,
            Session.status   == SessionStatus.active
        ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Invalid or expired QR code — ask faculty to refresh it.")

    # BUG FIX: branch/section were only ever enforced in the *listing*
    # endpoints (sessions/active, sessions/active/mine) for display
    # purposes — the actual marking endpoint never checked them. That
    # meant any logged-in student who got hold of a QR token (screenshot,
    # forwarded link, wrong WhatsApp group) could mark themselves present
    # in a class for a completely different branch/section, since only
    # the QR token + GPS were checked here. Apply the same flexible
    # branch/section match used everywhere else in the codebase; a
    # session with no branch/section set (e.g. an ad-hoc extra class) is
    # still open to everyone, matching existing behaviour elsewhere.
    if session.branch:
        import re as _re
        student_branch = (current_user.branch or getattr(current_user, "department", "") or "").strip()
        if not student_branch:
            raise HTTPException(status_code=403, detail="This session is for a specific branch and your profile has no branch set. Contact admin.")
        sb = session.branch.strip().lower()
        ub = student_branch.lower()
        sb_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', sb).strip()
        if not (ub == sb or sb_core in ub or ub in sb or sb in ub):
            raise HTTPException(status_code=403, detail="This session is for a different branch than yours.")
    if session.section:
        student_section = (current_user.section or "").strip().upper()
        if student_section != session.section.strip().upper():
            raise HTTPException(status_code=403, detail="This session is for a different section than yours.")

    # Sub-section check — if session has a sub_section set (lab batch),
    # only students whose course matches that sub_section can mark attendance
    if session.sub_section:
        student_subsec = (current_user.sub_section or "").strip().upper()
        session_subsec = session.sub_section.strip().upper()
        if student_subsec != session_subsec:
            raise HTTPException(
                status_code=403,
                detail=f"This lab session is for batch {session.sub_section} only. Your batch is {current_user.sub_section or 'not set'}."
            )

    # GPS check — server-side
    if session.gps_lat and session.gps_lng:
        if not payload.student_lat or not payload.student_lng:
            raise HTTPException(status_code=400, detail="GPS coordinates required.")
        try:
            dist    = haversine(session.gps_lat, session.gps_lng,
                                payload.student_lat, payload.student_lng)
            allowed = settings.gps_range if settings.gps_range is not None else 50
            if dist > allowed:
                raise HTTPException(
                    status_code=403,
                    detail=f"You are {int(dist)}m away. Must be within {allowed}m of classroom."
                )
        except HTTPException:
            raise
        except Exception:
            pass

    # BUG FIX / NEW FEATURE: face matching now actually happens server-side.
    # Previously face-api.js ran entirely in the browser and just told the
    # server "matched" — calling this endpoint directly (devtools, curl,
    # a replayed request) skipped the camera check completely.
    if settings.face_required:
        matched, reason = face_descriptor_matches(current_user.face_embedding, payload.face_descriptor)
        if not matched:
            raise HTTPException(status_code=403, detail=f"Face verification failed: {reason}")

    existing = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == session.id,
        AttendanceRecord.student_id == current_user.inst_id
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Attendance already marked.")

    # NEW FEATURE: proxy-detection heuristics — logs this check-in and
    # raises a review flag for faculty/admin if something looks off. Never
    # blocks the student; see _check_proxy_signals() docstring.
    _check_proxy_signals(db, session, current_user.inst_id, payload.device_id,
                          payload.student_lat, payload.student_lng)

    att_status = AttendanceStatus.present
    if session.started_at:
        if datetime.utcnow() > session.started_at + timedelta(minutes=session.grace_minutes):
            att_status = AttendanceStatus.late

    record = AttendanceRecord(
        session_id  = session.id,
        student_id  = current_user.inst_id,
        method      = AttendanceMethod.qr_gps_face,
        status      = att_status,
        student_lat = payload.student_lat,
        student_lng = payload.student_lng,
    )
    db.add(AttendanceAuditLog(session_id=session.id, student_id=current_user.inst_id,
                               changed_by=current_user.inst_id, action="create",
                               new_status=att_status.value if hasattr(att_status, "value") else str(att_status)))
    # BUG FIX: the "already marked" check above is a check-then-insert, so
    # two requests arriving together (double-tapping scan, an offline
    # retry, or the PWA replaying a queued request) both pass it and the
    # second one hits the uq_session_student unique constraint — which
    # used to surface as a confusing 500 instead of the 409 the first
    # check would have given. Catch it and return the same clean 409.
    try:
        db.add(record); db.commit(); db.refresh(record)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Attendance already marked.")
    return record


@router.post("/manual", response_model=AttendanceOut, status_code=201)
def mark_manual(
    payload: AttendanceMarkManual,
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    session = db.query(Session).filter(Session.id == payload.session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Check faculty owns this session
    if current_user.role == UserRole.faculty and session.faculty_id != current_user.inst_id:
        raise HTTPException(status_code=403, detail="This session belongs to another faculty.")

    # Check 10-minute edit window
    check_edit_window(session, db)

    # BUG FIX: previously nothing checked that payload.student_id was a
    # real student before inserting — a typo'd enrollment number would
    # hit the AttendanceRecord.student_id foreign key constraint at
    # commit time, raise an unhandled IntegrityError, and surface as a
    # raw 500 (see also the main.py exception-handler fix).
    if not db.query(Student).filter(Student.inst_id == payload.student_id).first():
        raise HTTPException(status_code=404, detail=f"No student found with ID {payload.student_id}.")

    existing = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == payload.session_id,
        AttendanceRecord.student_id == payload.student_id
    ).first()
    if existing:
        # NEW: audit log — record who changed this edit and what it was
        # before/after, so a disputed record can be traced.
        db.add(AttendanceAuditLog(
            record_id=existing.id, session_id=existing.session_id, student_id=existing.student_id,
            changed_by=current_user.inst_id, action="update",
            old_status=existing.status.value if hasattr(existing.status, "value") else str(existing.status),
            new_status=payload.status.value if hasattr(payload.status, "value") else str(payload.status),
            old_notes=existing.notes, new_notes=payload.notes,
        ))
        existing.status = payload.status
        existing.method = AttendanceMethod.manual
        existing.notes  = payload.notes
        db.commit(); db.refresh(existing)
        return existing

    record = AttendanceRecord(
        session_id=payload.session_id, student_id=payload.student_id,
        method=AttendanceMethod.manual, status=payload.status, notes=payload.notes,
    )
    db.add(AttendanceAuditLog(session_id=payload.session_id, student_id=payload.student_id,
                               changed_by=current_user.inst_id, action="create",
                               new_status=payload.status.value if hasattr(payload.status, "value") else str(payload.status),
                               new_notes=payload.notes))
    # Same check-then-insert race as the QR endpoint: if a student marks
    # themselves via QR (or another faculty saves the same row) in the
    # gap between the check above and this commit, fall back to updating
    # the row that won instead of returning a 500.
    try:
        db.add(record); db.commit(); db.refresh(record)
    except IntegrityError:
        db.rollback()
        existing = db.query(AttendanceRecord).filter(
            AttendanceRecord.session_id == payload.session_id,
            AttendanceRecord.student_id == payload.student_id
        ).first()
        if not existing:
            raise HTTPException(status_code=409, detail="Could not save attendance. Please retry.")
        existing.status = payload.status
        existing.method = AttendanceMethod.manual
        existing.notes  = payload.notes
        db.commit(); db.refresh(existing)
        return existing
    return record


@router.get("/session/{session_id}", response_model=AttendanceListOut)
def session_attendance(
    session_id: int,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    records = db.query(AttendanceRecord).filter(AttendanceRecord.session_id == session_id).all()
    return {"total": len(records), "records": records}


@router.get("/student/{student_id}", response_model=AttendanceListOut)
def student_history(
    student_id: str,
    course_id:  Optional[str] = None,
    skip: int = 0, limit: int = 200,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if current_user.role == UserRole.student and current_user.inst_id != student_id:
        raise HTTPException(status_code=403, detail="Access denied.")
    q = db.query(AttendanceRecord).filter(AttendanceRecord.student_id == student_id)
    if course_id:
        q = q.join(Session, AttendanceRecord.session_id == Session.id).filter(Session.course_id == course_id)
    total   = q.count()
    records = q.order_by(AttendanceRecord.marked_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "records": records}


# ── Feature 1: Excel / CSV Export ─────────────────────────────────────────────
import io, csv
from fastapi import Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import joinedload
from models.models import Course

@router.get("/export/session/{session_id}")
def export_session_attendance(
    session_id: int,
    format: str = Query("excel", pattern="^(excel|csv)$"),
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Export attendance for a session as Excel or CSV."""
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Get all students in this section/branch
    q = db.query(Student).filter(Student.status == "active")
    if session.branch:
        # Use flexible branch matching to handle format differences
        # e.g. "CSE(AI-ML-DL)" matches "B.Tech CSE (AI-ML-DL)"
        from sqlalchemy import func
        sb = session.branch.strip().lower()
        # Extract core branch (remove degree prefix)
        import re
        sb_core = re.sub(r'^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', sb, flags=re.IGNORECASE).strip()
        q = q.filter(
            (func.lower(Student.branch) == sb) |
            (Student.branch.ilike(f'%{sb_core}%')) |
            (Student.branch.ilike(f'%{sb}%')) |
            (Student.department.ilike(f'%{sb_core}%'))
        )
    if session.section: q = q.filter(
        func.lower(Student.section) == session.section.strip().lower()
    )
    students = q.order_by(Student.full_name).all()

    # Get attendance records
    records = db.query(AttendanceRecord).filter(
        AttendanceRecord.session_id == session_id
    ).all()
    marked = {r.student_id: r for r in records}

    # Get course name
    course = db.query(Course).filter(Course.id == session.course_id).first()

    rows = []
    for i, stu in enumerate(students, 1):
        rec = marked.get(stu.inst_id)
        rows.append({
            "S.No":           i,
            "Enrollment No.": stu.inst_id,
            "Student Name":   stu.full_name,
            "Branch":         stu.branch or "",
            "Section":        stu.section or "",
            "Status":         rec.status.value.upper() if rec else "ABSENT",
            "Method":         rec.method.value if rec else "-",
            "Marked At":      rec.marked_at.strftime("%d-%b-%Y %H:%M") if rec else "-",
        })

    filename = f"attendance_{course.code if course else 'session'}_{session_id}"

    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys() if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
        )
    else:
        # Excel
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed.")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Attendance"

        # Title row
        ws.merge_cells("A1:H1")
        title_cell = ws["A1"]
        title_cell.value = f"Attendance Report — {course.name if course else 'Session'} | {session.title or ''}"
        title_cell.font = Font(bold=True, size=13)
        title_cell.alignment = Alignment(horizontal="center")

        # Info row
        ws.merge_cells("A2:H2")
        ws["A2"].value = f"Date: {session.scheduled_at.strftime('%d %B %Y')} | Branch: {session.branch or '-'} | Section: {session.section or '-'} | Total Students: {len(students)}"
        ws["A2"].font = Font(size=10, italic=True)
        ws["A2"].alignment = Alignment(horizontal="center")

        # Header row
        headers = list(rows[0].keys()) if rows else []
        header_fill   = PatternFill("solid", fgColor="1a3c6e")
        header_font   = Font(bold=True, color="FFFFFF", size=11)
        thin_border   = Border(
            left=Side(style="thin"), right=Side(style="thin"),
            top=Side(style="thin"), bottom=Side(style="thin")
        )
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=3, column=col, value=h)
            cell.fill   = header_fill
            cell.font   = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.border = thin_border

        # Data rows
        present_fill = PatternFill("solid", fgColor="d1fae5")
        absent_fill  = PatternFill("solid", fgColor="fee2e2")
        late_fill    = PatternFill("solid", fgColor="fef3c7")

        for row_idx, row in enumerate(rows, 4):
            for col_idx, (key, val) in enumerate(row.items(), 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = thin_border
                cell.alignment = Alignment(horizontal="center" if col_idx in [1,4,5,6,7,8] else "left")
                if key == "Status":
                    if val == "PRESENT": cell.fill = present_fill
                    elif val == "ABSENT": cell.fill = absent_fill
                    elif val == "LATE":  cell.fill = late_fill

        # Column widths
        col_widths = [6, 18, 28, 20, 10, 12, 14, 20]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

        # Summary below data
        summary_row = len(rows) + 5
        present_count = sum(1 for r in rows if r["Status"] == "PRESENT")
        absent_count  = sum(1 for r in rows if r["Status"] == "ABSENT")
        late_count    = sum(1 for r in rows if r["Status"] == "LATE")
        ws.cell(row=summary_row, column=1, value="Summary:").font = Font(bold=True)
        ws.cell(row=summary_row, column=2, value=f"Present: {present_count}")
        ws.cell(row=summary_row, column=3, value=f"Absent: {absent_count}")
        ws.cell(row=summary_row, column=4, value=f"Late: {late_count}")
        ws.cell(row=summary_row, column=5, value=f"Total: {len(rows)}")

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
        )


@router.get("/export/student/{student_id}")
def export_student_attendance(
    student_id: str,
    format: str = Query("excel", pattern="^(excel|csv)$"),
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """Export a student's complete attendance record."""
    if current_user.role == UserRole.student and current_user.inst_id != student_id:
        raise HTTPException(status_code=403)

    student = db.query(Student).filter(Student.inst_id == student_id).first()
    if not student: raise HTTPException(status_code=404)

    records = db.query(AttendanceRecord)\
        .filter(AttendanceRecord.student_id == student_id)\
        .order_by(AttendanceRecord.marked_at.desc()).all()

    # Load sessions + courses in bulk — fixes N+1 query
    _sess_ids = list({r.session_id for r in records})
    _sessions_map = {s.id: s for s in db.query(Session).filter(Session.id.in_(_sess_ids)).all()} if _sess_ids else {}
    _course_ids = list({s.course_id for s in _sessions_map.values()})
    _courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(_course_ids)).all()} if _course_ids else {}

    rows = []
    for i, rec in enumerate(records, 1):
        sess   = _sessions_map.get(rec.session_id)
        course = _courses_map.get(sess.course_id) if sess else None
        rows.append({
            "S.No":       i,
            "Date":       rec.marked_at.strftime("%d-%b-%Y") if rec.marked_at else "-",
            "Course":     course.name if course else "-",
            "Code":       course.code if course else "-",
            "Session":    sess.title if sess else "-",
            "Status":     rec.status.value.upper(),
            "Method":     rec.method.value,
            "Time":       rec.marked_at.strftime("%H:%M") if rec.marked_at else "-",
        })

    filename = f"attendance_{student.inst_id}_{student.full_name.replace(' ','_')}"

    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys() if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
        )
    else:
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        except ImportError:
            raise HTTPException(status_code=500, detail="openpyxl not installed.")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "My Attendance"

        ws.merge_cells("A1:H1")
        ws["A1"].value = f"Attendance Report — {student.full_name} ({student.inst_id})"
        ws["A1"].font  = Font(bold=True, size=13)
        ws["A1"].alignment = Alignment(horizontal="center")

        ws.merge_cells("A2:H2")
        present = sum(1 for r in rows if r["Status"] == "PRESENT")
        total   = len(rows)
        pct     = round((present / total) * 100) if total else 0
        ws["A2"].value = f"Branch: {student.branch or '-'} | Section: {student.section or '-'} | Overall: {present}/{total} ({pct}%)"
        ws["A2"].font  = Font(size=10, italic=True)
        ws["A2"].alignment = Alignment(horizontal="center")

        headers = list(rows[0].keys()) if rows else []
        header_fill = PatternFill("solid", fgColor="1a3c6e")
        thin_border = Border(left=Side(style="thin"), right=Side(style="thin"),
                             top=Side(style="thin"), bottom=Side(style="thin"))
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=3, column=col, value=h)
            cell.fill = header_fill
            cell.font = Font(bold=True, color="FFFFFF", size=11)
            cell.alignment = Alignment(horizontal="center")
            cell.border = thin_border

        present_fill = PatternFill("solid", fgColor="d1fae5")
        absent_fill  = PatternFill("solid", fgColor="fee2e2")
        late_fill    = PatternFill("solid", fgColor="fef3c7")
        for row_idx, row in enumerate(rows, 4):
            for col_idx, (key, val) in enumerate(row.items(), 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.border = thin_border
                cell.alignment = Alignment(horizontal="center")
                if key == "Status":
                    if val == "PRESENT": cell.fill = present_fill
                    elif val == "ABSENT": cell.fill = absent_fill
                    elif val == "LATE":  cell.fill = late_fill

        col_widths = [6, 14, 30, 10, 25, 10, 14, 10]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}.xlsx"}
        )


# ── Feature 2: AI Insights ────────────────────────────────────────────────────
@router.get("/insights/student/{student_id}")
def student_insights(
    student_id: str,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """AI-style attendance insights for a student."""
    if current_user.role == UserRole.student and current_user.inst_id != student_id:
        raise HTTPException(status_code=403)

    student = db.query(Student).filter(Student.inst_id == student_id).first()
    if not student: raise HTTPException(status_code=404)

    records = db.query(AttendanceRecord)\
        .filter(AttendanceRecord.student_id == student_id).all()

    sessions_attended = [r for r in records if r.status.value == "present"]
    # Count only sessions relevant to THIS student (matching branch+section)
    from sqlalchemy import or_ as _or2, func as _func2
    import re as _re2
    b = (student.branch or '').strip()
    b_core = _re2.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b).strip()
    sess_q = db.query(Session).filter(Session.status == "closed")
    if b:
        sess_q = sess_q.filter(_or2(
            Session.branch == None,
            Session.branch == '',
            Session.branch.ilike(b),
            Session.branch.ilike(f'%{b_core}%'),
        ))
    if student.section:
        sess_q = sess_q.filter(_or2(
            Session.section == None,
            Session.section == '',
            _func2.upper(Session.section) == student.section.strip().upper(),
        ))
    total_sessions = sess_q.count()

    present_count = len(sessions_attended)
    pct = round((present_count / total_sessions) * 100) if total_sessions else 0

    # Classes needed to reach 75%
    needed = 0
    if pct < 75 and total_sessions > 0:
        # solve: (present + x) / (total + x) >= 0.75
        x = 0
        while True:
            if total_sessions + x == 0: break
            if (present_count + x) / (total_sessions + x) >= 0.75:
                needed = x
                break
            x += 1
            if x > 200: needed = -1; break

    # Load all sessions + courses in ONE query — fixes N+1 problem
    from collections import defaultdict
    session_ids = list({r.session_id for r in records})
    all_sessions = {s.id: s for s in db.query(Session).filter(Session.id.in_(session_ids)).all()} if session_ids else {}
    course_ids_needed = list({s.course_id for s in all_sessions.values()})
    all_courses = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids_needed)).all()} if course_ids_needed else {}

    # Day-wise analysis
    day_stats = defaultdict(lambda: {"present": 0, "total": 0})
    for rec in records:
        sess = all_sessions.get(rec.session_id)
        if sess and sess.scheduled_at:
            day = sess.scheduled_at.strftime("%A")
            day_stats[day]["total"] += 1
            if rec.status.value == "present":
                day_stats[day]["present"] += 1

    worst_day = None
    worst_pct = 100
    for day, stat in day_stats.items():
        if stat["total"] > 0:
            dp = round((stat["present"] / stat["total"]) * 100)
            if dp < worst_pct:
                worst_pct = dp
                worst_day = day

    # Course-wise breakdown
    course_stats = defaultdict(lambda: {"present": 0, "total": 0, "name": ""})
    for rec in records:
        sess = all_sessions.get(rec.session_id)
        if sess:
            course = all_courses.get(sess.course_id)
            cname = course.name if course else f"Course {sess.course_id}"
            course_stats[sess.course_id]["name"] = cname
            course_stats[sess.course_id]["total"] += 1
            if rec.status.value == "present":
                course_stats[sess.course_id]["present"] += 1

    courses_breakdown = []
    for cid, stat in course_stats.items():
        cp = round((stat["present"] / stat["total"]) * 100) if stat["total"] else 0
        courses_breakdown.append({
            "course_id":   cid,
            "course_name": stat["name"],
            "present":     stat["present"],
            "total":       stat["total"],
            "percentage":  cp,
            "status":      "safe" if cp >= 75 else "warning" if cp >= 60 else "danger"
        })
    courses_breakdown.sort(key=lambda x: x["percentage"])

    # Generate insights
    insights = []
    if pct >= 75:
        insights.append({"type": "success", "message": f"Your attendance is {pct}% - above the 75% minimum. Keep it up!"})
    elif pct >= 60:
        insights.append({"type": "warning", "message": f"Your attendance is {pct}% - below 75%. You need {needed} more consecutive classes to be safe."})
    else:
        insights.append({"type": "danger", "message": f"Critical! Your attendance is only {pct}%. You need {needed} more classes to reach 75%."})

    if worst_day and worst_pct < 70:
        insights.append({"type": "warning", "message": f"You miss classes most on {worst_day}s ({worst_pct}% attendance). Try to improve."})

    danger_courses = [c for c in courses_breakdown if c["status"] == "danger"]
    if danger_courses:
        names = ", ".join(c["course_name"] for c in danger_courses[:2])
        insights.append({"type": "danger", "message": f"Critical shortage in: {names}. Attend every remaining class."})

    return {
        "student": {"id": student.id, "name": student.full_name, "inst_id": student.inst_id},
        "overall": {
            "present":    present_count,
            "total":      total_sessions,
            "percentage": pct,
            "status":     "safe" if pct >= 75 else "warning" if pct >= 60 else "danger",
            "classes_needed_for_75": needed if pct < 75 else 0,
        },
        "worst_day":         {"day": worst_day, "percentage": worst_pct} if worst_day else None,
        "courses_breakdown": courses_breakdown,
        "insights":          insights,
    }


@router.get("/insights/section")
def section_insights(
    branch:  Optional[str] = None,
    section: Optional[str] = None,
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """AI insights for an entire section — identify at-risk students."""
    q = db.query(Student).filter(Student.status == "active")
    if branch:
        import re as _re
        from sqlalchemy import func as _func, or_ as _or
        b_raw  = branch.strip()
        b_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', b_raw).strip()
        q = q.filter(_or(
            Student.branch.ilike(b_raw),
            Student.branch.ilike(f'%{b_core}%'),
            Student.branch.ilike(f'%{b_raw}%'),
            Student.department.ilike(f'%{b_core}%'),
        ))
    if section:
        from sqlalchemy import func as _func2, or_ as _or2
        q = q.filter(_or2(
            _func2.upper(Student.section) == section.strip().upper(),
        ))
    students = q.all()

    total_sessions = db.query(Session).filter(
        Session.status == "closed",
        Session.branch  == branch,
        Session.section == section,
    ).count() if branch and section else 0

    at_risk, safe, critical = [], [], []
    for stu in students:
        # BUG FIX: this used to count EVERY attendance record the student
        # has ever had, across every branch/section/course they've ever
        # been in, while total_sessions above only counts closed sessions
        # for THIS branch+section. Mixing an unscoped numerator with a
        # scoped denominator produced meaningless percentages (could even
        # exceed 100%). Scope both sides the same way.
        present = db.query(AttendanceRecord).join(
            Session, AttendanceRecord.session_id == Session.id
        ).filter(
            AttendanceRecord.student_id == stu.inst_id,
            AttendanceRecord.status == AttendanceStatus.present,
            Session.status == SessionStatus.closed,
            Session.branch == branch,
            Session.section == section,
        ).count()
        pct = round((present / total_sessions) * 100) if total_sessions else 0
        entry = {"id": stu.id, "name": stu.full_name, "inst_id": stu.inst_id,
                 "present": present, "total": total_sessions, "percentage": pct}
        if pct < 60:   critical.append(entry)
        elif pct < 75: at_risk.append(entry)
        else:          safe.append(entry)

    return {
        "section_summary": {
            "branch": branch, "section": section,
            "total_students":   len(students),
            "total_sessions":   total_sessions,
            "safe_count":       len(safe),
            "at_risk_count":    len(at_risk),
            "critical_count":   len(critical),
        },
        "critical_students": sorted(critical, key=lambda x: x["percentage"]),
        "at_risk_students":  sorted(at_risk,  key=lambda x: x["percentage"]),
        "safe_students":     sorted(safe,      key=lambda x: x["percentage"], reverse=True),
    }


@router.get("/audit-log")
def audit_log(
    session_id: Optional[int] = None,
    student_id: Optional[str] = None,
    skip: int = 0, limit: int = 200,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """NEW: real, server-side, persistent audit trail. Replaces the old
    admin-panel 'Audit Log' page, which was a plain in-memory JS array —
    reset on every refresh, never survived a browser change, and only
    ever recorded 3 of the app's many mutating actions (approve/delete/
    bulk-approve user). This one is populated by attendance
    create/update actions (see mark_manual() and mark_full_flow() above)
    and persists in the database."""
    q = db.query(AttendanceAuditLog)
    if session_id is not None:
        q = q.filter(AttendanceAuditLog.session_id == session_id)
    if student_id:
        q = q.filter(AttendanceAuditLog.student_id == student_id)
    total = q.count()
    rows = q.order_by(AttendanceAuditLog.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "entries": [
        {"id": r.id, "record_id": r.record_id, "session_id": r.session_id, "student_id": r.student_id,
         "changed_by": r.changed_by, "action": r.action, "old_status": r.old_status, "new_status": r.new_status,
         "old_notes": r.old_notes, "new_notes": r.new_notes, "created_at": r.created_at.isoformat()}
        for r in rows
    ]}


# ── Defaulter list ────────────────────────────────────────────────────────
# Per-course attendance %, the way it's actually tracked for exam
# eligibility ("you're at 68% in DBMS"), not just an overall figure.
# Deliberately status-only — present/total/percent, nothing framed as "you
# can still skip N more classes".
@router.get("/defaulters", response_model=list[DefaulterOut])
def defaulters(
    course_id: str,
    threshold: float = Query(75.0, ge=0, le=100),
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    from models.models import Course
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")

    sessions = db.query(Session).filter(Session.course_id == course_id, Session.status == SessionStatus.closed).all()
    if not sessions:
        return []
    session_ids = [s.id for s in sessions]

    # Which students are actually in scope for this course (its branch/section)
    q = db.query(Student).filter(Student.status == "active")
    if course.branch:
        q = q.filter(Student.branch.ilike(f"%{course.branch}%"))
    if course.section:
        from sqlalchemy import func as _f
        q = q.filter(_f.upper(Student.section) == course.section.strip().upper())
    students = q.all()

    excused = _excused_dates_by_student(db, [s.inst_id for s in students])

    out = []
    for stu in students:
        applicable = [s for s in sessions if s.scheduled_at.date() not in excused.get(stu.inst_id, set())]
        if not applicable:
            continue
        applicable_ids = {s.id for s in applicable}
        present = db.query(AttendanceRecord).filter(
            AttendanceRecord.student_id == stu.inst_id,
            AttendanceRecord.session_id.in_(applicable_ids),
            AttendanceRecord.status == AttendanceStatus.present,
        ).count()
        pct = round((present / len(applicable)) * 100, 1)
        if pct < threshold:
            out.append(DefaulterOut(
                student_id=stu.inst_id, full_name=stu.full_name,
                course_id=course.id, course_name=course.name,
                present=present, total=len(applicable), percent=pct,
            ))
    out.sort(key=lambda d: d.percent)
    return out


def _excused_dates_by_student(db: DBSession, student_ids: list) -> dict:
    """Approved leave/OD requests, expanded to a set of excused calendar
    dates per student — used to exclude covered sessions from the
    attendance-percentage denominator so a documented absence doesn't
    unfairly tank someone's percentage."""
    if not student_ids:
        return {}
    approved = db.query(LeaveRequest).filter(
        LeaveRequest.student_id.in_(student_ids),
        LeaveRequest.status == "approved",
    ).all()
    result = {}
    for lr in approved:
        d = lr.from_date.date()
        end = lr.to_date.date()
        days = set()
        while d <= end:
            days.add(d)
            d += timedelta(days=1)
        result.setdefault(lr.student_id, set()).update(days)
    return result


# ── NEW: proxy-detection flag review ─────────────────────────────────────
@router.get("/flags", response_model=list[AttendanceFlagOut])
def list_flags(
    session_id: Optional[int] = None,
    resolved: Optional[bool] = None,
    skip: int = 0, limit: int = 100,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    q = db.query(AttendanceFlag)
    if session_id is not None:
        q = q.filter(AttendanceFlag.session_id == session_id)
    if resolved is not None:
        q = q.filter(AttendanceFlag.resolved == resolved)
    return q.order_by(AttendanceFlag.created_at.desc()).offset(skip).limit(limit).all()


@router.patch("/flags/{flag_id}", response_model=AttendanceFlagOut)
def resolve_flag(
    flag_id: int,
    payload: FlagResolveRequest,
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    flag = db.query(AttendanceFlag).filter(AttendanceFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(status_code=404, detail="Flag not found.")
    flag.resolved = payload.resolved
    flag.resolved_by = current_user.inst_id
    db.commit(); db.refresh(flag)
    return flag


# ── NEW: monthly attendance report (xlsx) ────────────────────────────────
@router.get("/report/monthly")
def monthly_report(
    year: int, month: int,
    course_id: Optional[str] = None,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from fastapi.responses import StreamingResponse
    from models.models import Course

    start = datetime(year, month, 1)
    end = datetime(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    q = db.query(Session).filter(Session.scheduled_at >= start, Session.scheduled_at < end,
                                  Session.status == SessionStatus.closed)
    if course_id:
        q = q.filter(Session.course_id == course_id)
    sessions = q.order_by(Session.scheduled_at).all()
    session_ids = [s.id for s in sessions]

    wb = Workbook()
    ws = wb.active
    ws.title = f"{year}-{month:02d}"
    headers = ["Student ID", "Name", "Branch", "Section", "Present", "Total Sessions", "Percentage"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)

    if session_ids:
        student_q = db.query(Student).filter(Student.status == "active")
        for stu in student_q.all():
            present = db.query(AttendanceRecord).filter(
                AttendanceRecord.student_id == stu.inst_id,
                AttendanceRecord.session_id.in_(session_ids),
                AttendanceRecord.status == AttendanceStatus.present,
            ).count()
            marked = db.query(AttendanceRecord).filter(
                AttendanceRecord.student_id == stu.inst_id,
                AttendanceRecord.session_id.in_(session_ids),
            ).count()
            if marked == 0:
                continue
            pct = round((present / marked) * 100, 1)
            ws.append([stu.inst_id, stu.full_name, stu.branch or "", stu.section or "",
                       present, marked, pct])

    for col in ws.columns:
        width = max((len(str(cell.value)) for cell in col if cell.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(width + 3, 40)

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"attendance_report_{year}_{month:02d}" + (f"_{course_id}" if course_id else "") + ".xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
