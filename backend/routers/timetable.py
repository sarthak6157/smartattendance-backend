"""Timetable routes — admin creates slots, faculty goes live."""
from datetime import datetime
import secrets
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import TimetableSlot, Session, SessionStatus, User, UserRole, Course, DayOfWeek
from pydantic import BaseModel

router = APIRouter()
AdminOnly      = require_roles(UserRole.admin)
FacultyOrAdmin = require_roles(UserRole.faculty, UserRole.admin)


# ── Schemas ──────────────────────────────────────────────────────────────────

class SlotCreate(BaseModel):
    course_id:   int
    faculty_id:  int
    day_of_week: str
    start_time:  str
    end_time:    str
    room:        Optional[str] = None
    branch:      Optional[str] = None
    section:     Optional[str] = None
    sub_section: Optional[str] = None   # e.g. "A1","A2" — only for labs
    semester:    Optional[str] = None
    course_type: Optional[str] = None

class SlotOut(BaseModel):
    id:          int
    course_id:   int
    faculty_id:  int
    day_of_week: str
    start_time:  str
    end_time:    str
    room:        Optional[str]
    branch:      Optional[str]
    section:     Optional[str]
    sub_section: Optional[str] = None
    semester:    Optional[str]
    course_type: Optional[str]
    is_active:   bool
    course_name: Optional[str] = None
    faculty_name:Optional[str] = None

    model_config = {"from_attributes": True, "use_enum_values": True}

    def model_post_init(self, __context):
        # Ensure day_of_week is always a plain string not enum object
        if hasattr(self.day_of_week, 'value'):
            object.__setattr__(self, 'day_of_week', self.day_of_week.value)

class GoLiveRequest(BaseModel):
    gps_lat: Optional[str] = None
    gps_lng: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

# ⚠️ /debug/student-match MUST be before /{slot_id} routes to avoid 404

@router.get("/debug/student-match")
def debug_student_match(
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    from sqlalchemy import distinct
    all_branches = db.query(distinct(TimetableSlot.branch)).all()
    all_sections = db.query(distinct(TimetableSlot.section)).all()
    return {
        "student_branch":      current_user.branch,
        "student_department":  current_user.department,
        "student_section":     current_user.section,
        "student_sub_section": current_user.sub_section,   # ← FIXED: was current_user.course
        "timetable_branches":  [r[0] for r in all_branches],
        "timetable_sections":  [r[0] for r in all_sections],
    }


@router.get("", response_model=List[SlotOut])
def list_slots(
    branch:     Optional[str] = Query(None),
    section:    Optional[str] = Query(None),
    faculty_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    from sqlalchemy import func, or_
    q = db.query(TimetableSlot).filter(TimetableSlot.is_active == True)

    if current_user.role == UserRole.faculty:
        q = q.filter(TimetableSlot.faculty_id == current_user.id)
    elif faculty_id:
        q = q.filter(TimetableSlot.faculty_id == faculty_id)

    # For students: auto-inject their branch/section/sub_section
    if current_user.role == UserRole.student:
        effective_branch     = branch   or current_user.branch or current_user.department
        effective_section    = section  or current_user.section
        # ── FIX: read sub_section directly from the users table ──────────────
        # Previously this was reading current_user.course (which holds "B.Tech")
        # and trying to guess if it was a lab batch — that was always wrong.
        # Now we read current_user.sub_section which is the actual DB column.
        effective_subsection = (current_user.sub_section or "").strip() or None
    else:
        effective_branch     = branch
        effective_section    = section
        effective_subsection = None

    # ── Branch Matching ───────────────────────────────────────────────────────
    # Problem: slot branch = "CSE(AI-ML-DL)" but user branch = "CSE (AI-ML-DL)"
    # Solution: extract CORE keywords and match loosely

    def extract_core(branch_str):
        """Remove degree prefix and normalize branch string."""
        import re
        if not branch_str:
            return ""
        s = branch_str.strip().lower()
        prefixes = [
            'b.tech - ', 'b.tech-', 'b.tech ', 'btech ',
            'b.e - ', 'b.e-', 'b.e ',
            'm.tech - ', 'm.tech-', 'm.tech ',
            'bca - ', 'bca-', 'bca ',
            'mca - ', 'mca-', 'mca ',
            'mba - ', 'mba-', 'mba ',
            'b.sc - ', 'b.sc-', 'b.sc ',
            'b.pharma - ', 'b.pharma ',
        ]
        for p in prefixes:
            if s.startswith(p):
                s = s[len(p):]
                break
        s = re.sub(r'[\s\-_]+', ' ', s).strip()
        return s

    if effective_branch:
        eb = effective_branch.strip().lower()
        eb_core = extract_core(eb)
        eb_short = eb_core.split('(')[0].strip() if eb_core else ''

        if current_user.role == UserRole.student:
            conditions = [
                TimetableSlot.branch == None,
                TimetableSlot.branch == '',
                func.lower(TimetableSlot.branch) == eb,
                func.strpos(func.lower(TimetableSlot.branch), eb) > 0,
                func.strpos(eb, func.lower(TimetableSlot.branch)) > 0,
            ]
            if eb_core:
                conditions.append(func.strpos(func.lower(TimetableSlot.branch), eb_core) > 0)
            if eb_short:
                conditions.append(func.strpos(func.lower(TimetableSlot.branch), eb_short) > 0)
            q = q.filter(or_(*conditions))
        else:
            q = q.filter(
                or_(
                    func.lower(TimetableSlot.branch) == eb,
                    func.strpos(func.lower(TimetableSlot.branch), eb) > 0,
                    func.strpos(eb, func.lower(TimetableSlot.branch)) > 0,
                )
            )

    # ── Section + Sub-section Filter ─────────────────────────────────────────
    #
    # How lab slots work in your timetable_slots table:
    #   Theory slots → section = "A",  sub_section = NULL
    #   Lab slots    → section = "A1", sub_section = NULL   (section IS the batch)
    #
    # A student with section="A" and sub_section="A1" should see:
    #   - All theory slots where slot.section = "A"
    #   - Their lab slots where slot.section = "A1"
    #
    # So the query becomes:
    #   slot.section = student.section   (catches theory)
    #   OR slot.section = student.sub_section  (catches labs)

    if effective_section:
        sec = effective_section.strip().upper()

        if current_user.role == UserRole.student:
            if effective_subsection:
                subsec = effective_subsection.strip().upper()
                from sqlalchemy import and_, or_
                # Student sees: theory (section=A) OR their lab batch (section=A1)
                q = q.filter(
                    or_(
                        func.upper(TimetableSlot.section) == sec,        # theory rows
                        func.upper(TimetableSlot.section) == subsec,     # lab rows
                    )
                )
            else:
                # No sub_section assigned — only show main section slots
                q = q.filter(
                    or_(
                        TimetableSlot.section == None,
                        func.upper(TimetableSlot.section) == sec,
                    )
                )
        else:
            q = q.filter(func.lower(TimetableSlot.section) == sec.lower())

    slots = q.order_by(TimetableSlot.day_of_week, TimetableSlot.start_time).all()
    result = []
    for s in slots:
        co  = db.query(Course).filter(Course.id == s.course_id).first()
        fac = db.query(User).filter(User.id == s.faculty_id).first()
        d = SlotOut.model_validate(s)
        # Explicitly convert enum to string value
        d.day_of_week  = s.day_of_week.value if hasattr(s.day_of_week, 'value') else str(s.day_of_week)
        d.course_name  = co.name       if co  else None
        d.faculty_name = fac.full_name if fac else None
        result.append(d)
    return result


@router.post("", response_model=SlotOut, status_code=201)
def create_slot(
    payload: SlotCreate,
    _: User = Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    co  = db.query(Course).filter(Course.id == payload.course_id).first()
    fac = db.query(User).filter(User.id == payload.faculty_id, User.role == UserRole.faculty).first()
    if not co:  raise HTTPException(status_code=404, detail="Course not found.")
    if not fac: raise HTTPException(status_code=404, detail="Faculty not found.")

    # Normalize day_of_week to lowercase to match DB enum
    data = payload.model_dump()
    data['day_of_week'] = data['day_of_week'].strip().lower()

    slot = TimetableSlot(**data)
    db.add(slot); db.commit(); db.refresh(slot)
    d = SlotOut.model_validate(slot)
    d.course_name = co.name; d.faculty_name = fac.full_name
    return d


@router.delete("/{slot_id}", status_code=204)
def delete_slot(slot_id: int, _: User = Depends(AdminOnly), db: DBSession = Depends(get_db)):
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
    if not slot: raise HTTPException(status_code=404)
    db.delete(slot); db.commit()


@router.post("/{slot_id}/go-live")
def go_live(
    slot_id: int,
    payload: GoLiveRequest,
    current_user: User = Depends(FacultyOrAdmin),
    db: DBSession = Depends(get_db),
):
    """Faculty clicks Go Live on a timetable slot → creates active session."""
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id, TimetableSlot.is_active == True).first()
    if not slot: raise HTTPException(status_code=404, detail="Timetable slot not found.")
    if current_user.role != UserRole.admin and slot.faculty_id != current_user.id:
        raise HTTPException(status_code=403, detail="This slot belongs to another faculty.")
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    existing = db.query(Session).filter(
        Session.timetable_id == slot_id,
        Session.status == SessionStatus.active,
        Session.created_at >= today_start,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="This class is already live today.")
    co = db.query(Course).filter(Course.id == slot.course_id).first()
    now = datetime.utcnow()

    # ── Time window check ──────────────────────────────────────────────────
    # IST = UTC + 5:30
    from datetime import timedelta
    ist_now = now + timedelta(hours=5, minutes=30)
    slot_start_h, slot_start_m = map(int, slot.start_time.split(':'))
    slot_end_h,   slot_end_m   = map(int, slot.end_time.split(':'))
    now_mins   = ist_now.hour * 60 + ist_now.minute
    start_mins = slot_start_h * 60 + slot_start_m - 5   # 5-min early buffer
    end_mins   = slot_end_h   * 60 + slot_end_m

    if now_mins < start_mins:
        mins_until = (slot_start_h*60+slot_start_m) - now_mins
        raise HTTPException(
            status_code=400,
            detail=f"Class hasn't started yet. Go Live opens {mins_until} minutes before class ({slot.start_time})."
        )
    if now_mins > end_mins:
        raise HTTPException(
            status_code=400,
            detail=f"Class time is over ({slot.end_time}). Go Live is locked after class ends."
        )

    session = Session(
        course_id    = slot.course_id,
        faculty_id   = slot.faculty_id,
        timetable_id = slot.id,
        title        = f"{co.name if co else 'Class'} — {slot.day_of_week} {slot.start_time}",
        location     = slot.room,
        branch       = slot.branch,
        section      = slot.section,
        sub_section  = slot.sub_section,   # pass sub_section to session
        semester     = slot.semester,
        course_type  = slot.course_type,
        gps_lat      = payload.gps_lat,
        gps_lng      = payload.gps_lng,
        status       = SessionStatus.active,
        scheduled_at = now,
        started_at   = now,
        qr_token     = secrets.token_urlsafe(16),
        grace_minutes= 15,
    )
    db.add(session); db.commit(); db.refresh(session)
    return {"session_id": session.id, "qr_token": session.qr_token, "message": "Session is now live!"}
