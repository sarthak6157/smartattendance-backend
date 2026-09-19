"""Leave request workflow — NEW FEATURE.

Students submit a leave request for a date range with a reason.
Faculty/admin approve or reject. An approved request excludes its date
range from that student's attendance-percentage denominator wherever
percentages are computed (see attendance.py's _excused_dates_by_student
and defaulters()) — so a documented, approved absence doesn't unfairly
tank someone's eligibility number.

NOTE: On-Duty (OD) was removed as a request type at the user's request —
only "leave" exists now. leave_type is kept as a column/field (rather
than deleted outright) since it costs nothing to leave in place and
makes reintroducing OD, or adding another type later, a small change
instead of a schema migration.
"""
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import LeaveRequest, Student, UserRole
from schemas.schemas import LeaveCreate, LeaveListOut, LeaveOut, LeaveReview

router = APIRouter()


@router.post("", response_model=LeaveOut, status_code=201)
def create_leave(
    payload: LeaveCreate,
    current_user = Depends(require_roles(UserRole.student)),
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
        student_id=current_user.inst_id,
        from_date=payload.from_date, to_date=payload.to_date,
        leave_type=leave_type, reason=payload.reason.strip()[:500],
        status="pending",
    )
    db.add(req); db.commit(); db.refresh(req)
    return req


@router.get("/mine", response_model=LeaveListOut)
def my_leave_requests(
    current_user = Depends(require_roles(UserRole.student)),
    db: DBSession = Depends(get_db),
):
    q = db.query(LeaveRequest).filter(LeaveRequest.student_id == current_user.inst_id)\
          .order_by(LeaveRequest.created_at.desc())
    items = q.all()
    return {"total": len(items), "requests": items}


@router.get("", response_model=LeaveListOut)
def list_leave_requests(
    status_: Optional[str] = Query(None, alias="status"),
    student_id: Optional[str] = None,
    skip: int = 0, limit: int = 100,
    _ = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    q = db.query(LeaveRequest)
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    if student_id:
        q = q.filter(LeaveRequest.student_id == student_id)
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
    req.status = payload.status
    req.reviewed_by = current_user.inst_id
    req.review_note = payload.review_note
    req.updated_at = datetime.utcnow()
    db.commit(); db.refresh(req)
    return req
