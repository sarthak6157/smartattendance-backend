"""Leave request workflow.

Students submit a leave request for a date range with a reason. An
approved STUDENT request excludes its date range from that student's
attendance-percentage denominator (see attendance.py's
_excused_dates_by_student and defaulters()).

NEW FEATURE: faculty can now submit leave requests too. Reviewing a
faculty leave request is admin-only (a faculty peer-approving, or
worse self-approving, their own leave isn't appropriate — see the role
check in review_leave()). Once approved, admin uses the
affected-classes/assign-substitute endpoints below to arrange coverage;
the substitute doesn't get anything auto-created for them — they start
the class themselves via the existing "Extra Class" flow when it's
actually time, same QR/GPS/face machinery as always. This table is
purely the bookkeeping record of who's covering what.

NOTE: On-Duty (OD) was removed as a request type at the user's request —
only "leave" exists now. leave_type is kept as a column/field (rather
than deleted outright) since it costs nothing to leave in place and
makes reintroducing OD, or adding another type later, a small change
instead of a schema migration.
"""
from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from core.security import require_roles
from db.database import get_db
from models.models import (Course, Faculty, LeaveRequest,
                            SubstituteAssignment, TimetableSlot, UserRole)
from schemas.schemas import (AssignSubstituteRequest, LeaveCreate, LeaveListOut, LeaveOut,
                              LeaveReview, SubstituteAssignmentOut)

router = APIRouter()

DAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@router.post("", response_model=LeaveOut, status_code=201)
def create_leave(
    payload: LeaveCreate,
    current_user = Depends(require_roles(UserRole.student, UserRole.faculty)),
    db: DBSession = Depends(get_db),
):
    if payload.to_date < payload.from_date:
        raise HTTPException(status_code=400, detail="to_date cannot be before from_date.")
    # BUG FIX / CHANGE: OD removed per request — "leave" is now the only
    # accepted type, regardless of what a client sends (defensive against
    # a stale cached frontend still offering the OD option).
    leave_type = "leave"
    if not payload.reason or not payload.reason.strip():
        raise HTTPException(status_code=400, detail="A reason is required.")
    req = LeaveRequest(
        student_id=current_user.inst_id if current_user.role == UserRole.student else None,
        faculty_id=current_user.inst_id if current_user.role == UserRole.faculty else None,
        from_date=payload.from_date, to_date=payload.to_date,
        leave_type=leave_type, reason=payload.reason.strip()[:500],
        status="pending",
    )
    db.add(req); db.commit(); db.refresh(req)
    return req


@router.get("/mine", response_model=LeaveListOut)
def my_leave_requests(
    current_user = Depends(require_roles(UserRole.student, UserRole.faculty)),
    db: DBSession = Depends(get_db),
):
    id_col = LeaveRequest.student_id if current_user.role == UserRole.student else LeaveRequest.faculty_id
    q = db.query(LeaveRequest).filter(id_col == current_user.inst_id)\
          .order_by(LeaveRequest.created_at.desc())
    items = q.all()
    return {"total": len(items), "requests": items}


@router.get("", response_model=LeaveListOut)
def list_leave_requests(
    status_: Optional[str] = Query(None, alias="status"),
    student_id: Optional[str] = None,
    requester_type: Optional[str] = Query(None, description="'student' or 'faculty'"),
    skip: int = 0, limit: int = 100,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    q = db.query(LeaveRequest)
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    if student_id:
        q = q.filter(LeaveRequest.student_id == student_id)
    if requester_type == "student":
        q = q.filter(LeaveRequest.student_id.isnot(None))
    elif requester_type == "faculty":
        q = q.filter(LeaveRequest.faculty_id.isnot(None))
    total = q.count()
    items = q.order_by(LeaveRequest.created_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "requests": items}


@router.patch("/{leave_id}", response_model=LeaveOut)
def review_leave(
    leave_id: int,
    payload: LeaveReview,
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    if payload.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be 'approved' or 'rejected'.")
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Leave request not found.")
    if req.status != "pending":
        raise HTTPException(status_code=409, detail=f"This request was already {req.status}.")
    # NEW: a faculty member's leave request can only be reviewed by an
    # admin — a peer faculty approving (or, worse, a faculty somehow
    # approving their own) leave request isn't appropriate. Student leave
    # requests are unaffected — faculty/admin can both review those, same
    # as before.
    if req.faculty_id and current_user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Faculty leave requests can only be reviewed by an admin.")
    req.status = payload.status
    req.reviewed_by = current_user.inst_id
    req.review_note = payload.review_note
    req.updated_at = datetime.utcnow()
    db.commit(); db.refresh(req)
    return req


# ── NEW: substitute assignment for approved faculty leave ────────────────
@router.get("/{leave_id}/affected-classes")
def affected_classes(
    leave_id: int,
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """For an approved faculty leave request, list every occurrence of
    that faculty's recurring timetable slots that falls within the leave
    date range — one row per (slot, calendar date) — so admin can assign
    a substitute for each. Already-assigned ones are flagged so the UI
    doesn't offer to double-assign."""
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Leave request not found.")
    if not req.faculty_id:
        raise HTTPException(status_code=400, detail="This is not a faculty leave request.")
    if req.status != "approved":
        raise HTTPException(status_code=400, detail="Leave must be approved before assigning substitutes.")

    slots = db.query(TimetableSlot).filter(
        TimetableSlot.faculty_id == req.faculty_id,
        TimetableSlot.is_active == True,
    ).all()
    course_ids = list({s.course_id for s in slots})
    courses = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()} if course_ids else {}

    existing = {
        (a.timetable_slot_id, a.class_date.date()): a
        for a in db.query(SubstituteAssignment).filter(SubstituteAssignment.leave_request_id == leave_id).all()
    }

    out = []
    d = req.from_date.date()
    end = req.to_date.date()
    while d <= end:
        day_name = DAY_ORDER[d.weekday()]
        for s in slots:
            slot_day = s.day_of_week.value if hasattr(s.day_of_week, "value") else str(s.day_of_week)
            if slot_day.lower() != day_name:
                continue
            key = (s.id, d)
            assigned = existing.get(key)
            out.append({
                "timetable_slot_id": s.id,
                "class_date": d.isoformat(),
                "day": day_name,
                "start_time": s.start_time, "end_time": s.end_time,
                "course_id": s.course_id,
                "course_name": courses.get(s.course_id).name if courses.get(s.course_id) else s.course_id,
                "branch": s.branch, "section": s.section, "room": s.room,
                "substitute_faculty_id": assigned.substitute_faculty_id if assigned else None,
                "assignment_id": assigned.id if assigned else None,
            })
        d += timedelta(days=1)
    return {"leave_id": leave_id, "faculty_id": req.faculty_id, "classes": out}


@router.post("/{leave_id}/assign-substitute", response_model=SubstituteAssignmentOut, status_code=201)
def assign_substitute(
    leave_id: int,
    payload: AssignSubstituteRequest,
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req or not req.faculty_id:
        raise HTTPException(status_code=404, detail="Faculty leave request not found.")
    if req.status != "approved":
        raise HTTPException(status_code=400, detail="Leave must be approved first.")
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == payload.timetable_slot_id).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Timetable slot not found.")
    # BUG FIX: this never checked that the slot actually belongs to the
    # faculty member whose leave this is — a stray or malicious
    # timetable_slot_id would silently create a bogus substitute record
    # for a completely unrelated class.
    if slot.faculty_id != req.faculty_id:
        raise HTTPException(status_code=400, detail="That timetable slot doesn't belong to the faculty member on leave.")
    # BUG FIX: also never checked the date is actually within the
    # approved leave range, or that it falls on the day of week the slot
    # actually runs on — either gap lets a typo'd date create a
    # substitute assignment for a class that was never actually affected.
    class_date_only = payload.class_date.date()
    if not (req.from_date.date() <= class_date_only <= req.to_date.date()):
        raise HTTPException(status_code=400, detail="class_date is outside this leave request's date range.")
    slot_day = slot.day_of_week.value if hasattr(slot.day_of_week, "value") else str(slot.day_of_week)
    if DAY_ORDER[class_date_only.weekday()] != slot_day.lower():
        raise HTTPException(status_code=400, detail=f"That slot runs on {slot_day}, not {DAY_ORDER[class_date_only.weekday()]}.")
    substitute = db.query(Faculty).filter(Faculty.inst_id == payload.substitute_faculty_id).first()
    if not substitute:
        raise HTTPException(status_code=404, detail="Substitute faculty not found.")
    if payload.substitute_faculty_id == req.faculty_id:
        raise HTTPException(status_code=400, detail="Substitute can't be the same faculty who's on leave.")

    existing = db.query(SubstituteAssignment).filter(
        SubstituteAssignment.leave_request_id == leave_id,
        SubstituteAssignment.timetable_slot_id == payload.timetable_slot_id,
        SubstituteAssignment.class_date == payload.class_date,
    ).first()
    if existing:
        existing.substitute_faculty_id = payload.substitute_faculty_id
        db.commit(); db.refresh(existing)
        return existing

    assignment = SubstituteAssignment(
        leave_request_id=leave_id, timetable_slot_id=payload.timetable_slot_id,
        class_date=payload.class_date, original_faculty_id=req.faculty_id,
        substitute_faculty_id=payload.substitute_faculty_id, course_id=slot.course_id,
    )
    db.add(assignment); db.commit(); db.refresh(assignment)
    return assignment


@router.get("/substitute-assignments/mine", response_model=list[SubstituteAssignmentOut])
def my_substitute_assignments(
    current_user = Depends(require_roles(UserRole.faculty)),
    db: DBSession = Depends(get_db),
):
    """What a substitute sees on their own dashboard — upcoming classes
    they've been asked to cover, today or in the future."""
    today = datetime.utcnow().date()
    return db.query(SubstituteAssignment).filter(
        SubstituteAssignment.substitute_faculty_id == current_user.inst_id,
        SubstituteAssignment.class_date >= datetime(today.year, today.month, today.day),
    ).order_by(SubstituteAssignment.class_date).all()
