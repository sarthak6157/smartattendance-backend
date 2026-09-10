"""Timetable routes — visual grid builder, copy, conflict detection."""
import re
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, and_
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import (TimetableSlot, Session, SessionStatus,
                           Faculty, Student, Admin, UserRole, Course, DayOfWeek)

router    = APIRouter()
AdminOnly = require_roles(UserRole.admin)

DAYS = ["monday","tuesday","wednesday","thursday","friday","saturday"]

# Default time slots
DEFAULT_SLOTS = [
    "09:10-09:25",
    "09:30-10:30",
    "10:30-11:25",
    "11:30-12:25",
    "12:30-13:25",
    "14:25-15:10",
    "15:10-15:55",
]

# ── Pydantic schemas ──────────────────────────────────────────────────────────
class SlotCreate(BaseModel):
    course_id:   str
    faculty_id:  Optional[str] = None   # optional for free classes
    day_of_week: str
    start_time:  str
    end_time:    str
    room:        Optional[str] = None
    branch:      Optional[str] = None
    section:     Optional[str] = None
    sub_section: Optional[str] = None
    semester:    Optional[str] = None
    course_type: Optional[str] = None

class SlotOut(BaseModel):
    id:           int
    course_id:    str
    faculty_id:   Optional[str] = None
    day_of_week:  str
    start_time:   str
    end_time:     str
    room:         Optional[str] = None
    branch:       Optional[str] = None
    section:      Optional[str] = None
    sub_section:  Optional[str] = None
    semester:     Optional[str] = None
    course_type:  Optional[str] = None
    is_active:    bool
    course_name:  Optional[str] = None
    course_code:  Optional[str] = None
    faculty_name: Optional[str] = None
    model_config  = {"from_attributes": True, "use_enum_values": True}

class GoLiveRequest(BaseModel):
    gps_lat: Optional[str] = None
    gps_lng: Optional[str] = None

class CopyTimetableRequest(BaseModel):
    from_branch:   str
    from_section:  str
    from_semester: str
    to_branch:     str
    to_section:    str
    to_semester:   str
    copy_teachers: bool = False  # if False, teachers = unassigned (faculty_id=1)


# ── Helper: extract core branch for fuzzy match ───────────────────────────────
def extract_core(branch_str: str) -> str:
    if not branch_str: return ""
    s = branch_str.strip().lower()
    for p in ["b.tech - ","b.tech-","b.tech ","btech ","b.e - ","b.e ","m.tech - ","m.tech "]:
        if s.startswith(p): s = s[len(p):]; break
    return re.sub(r"[\s\-_]+", " ", s).strip()


# ── List slots ────────────────────────────────────────────────────────────────
@router.get("", response_model=list[SlotOut])
def list_slots(
    branch:     Optional[str] = Query(None),
    section:    Optional[str] = Query(None),
    semester:   Optional[str] = Query(None),
    faculty_id: Optional[str] = Query(None),
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    q = db.query(TimetableSlot).filter(TimetableSlot.is_active == True)

    if current_user.role == UserRole.faculty:
        # Slots assigned to this faculty always show. Unassigned "free
        # period" slots (Library, Coding Practice, Mentor Interaction, etc.)
        # only show when they're genuinely global (no branch/section set —
        # e.g. a school-wide Mentor Interaction period) or when they belong
        # to a branch this faculty already teaches elsewhere. Previously
        # every unassigned slot from every branch/section was shown to every
        # faculty member, regardless of relevance.
        my_branches = {
            (b or '').strip().lower()
            for (b,) in db.query(TimetableSlot.branch)
                          .filter(TimetableSlot.faculty_id == current_user.inst_id)
                          .distinct()
        }
        my_branches.discard('')
        unassigned_conditions = [
            TimetableSlot.branch == None,
            TimetableSlot.branch == '',
        ]
        if my_branches:
            unassigned_conditions.append(func.lower(TimetableSlot.branch).in_(my_branches))
        q = q.filter(or_(
            TimetableSlot.faculty_id == current_user.inst_id,
            and_(TimetableSlot.faculty_id == None, or_(*unassigned_conditions)),
        ))
    elif faculty_id:
        q = q.filter(TimetableSlot.faculty_id == faculty_id)

    if current_user.role == UserRole.student:
        effective_branch   = branch  or current_user.branch or current_user.department
        effective_section  = section or current_user.section
        # Lab batch (e.g. "C1") lives in Student.sub_section, NOT Student.course
        # (course holds the degree type, e.g. "B.Tech"). Reading the wrong
        # field here meant effective_subsection was almost always empty,
        # which fell through to showing every batch's lab slots.
        effective_subsection = (current_user.sub_section or "").strip() or None
    else:
        effective_branch     = branch
        effective_section    = section
        effective_subsection = None

    if effective_branch:
        eb      = effective_branch.strip().lower()
        eb_core = extract_core(eb)
        eb_short = eb_core.split("(")[0].strip() if eb_core else ""
        conditions = [
            TimetableSlot.branch == None,
            TimetableSlot.branch == "",
            func.lower(TimetableSlot.branch) == eb,
            func.strpos(func.lower(TimetableSlot.branch), eb) > 0,
            func.strpos(eb, func.lower(TimetableSlot.branch)) > 0,
        ]
        if eb_core:  conditions.append(func.strpos(func.lower(TimetableSlot.branch), eb_core) > 0)
        if eb_short: conditions.append(func.strpos(func.lower(TimetableSlot.branch), eb_short) > 0)
        q = q.filter(or_(*conditions))

    if effective_section:
        sec = effective_section.strip().upper()
        if current_user.role == UserRole.student and effective_subsection:
            subsec = effective_subsection.strip().upper()
            q = q.filter(
                func.lower(TimetableSlot.section) == sec.lower(),
                or_(
                    TimetableSlot.sub_section == None,
                    func.lower(TimetableSlot.sub_section) == subsec.lower()
                )
            )
        else:
            q = q.filter(func.lower(TimetableSlot.section) == sec.lower())

    if semester:
        q = q.filter(func.lower(TimetableSlot.semester) == semester.strip().lower())

    slots = q.order_by(TimetableSlot.day_of_week, TimetableSlot.start_time).all()
    if not slots: return []

    # Bulk load courses and faculty
    course_ids  = list({s.course_id  for s in slots})
    faculty_ids = list({s.faculty_id for s in slots})
    courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()}
    faculty_map = {f.inst_id: f for f in db.query(Faculty).filter(Faculty.inst_id.in_(faculty_ids)).all()}

    result = []
    for s in slots:
        co  = courses_map.get(s.course_id)
        fac = faculty_map.get(s.faculty_id)
        d   = SlotOut.model_validate(s)
        d.day_of_week  = s.day_of_week.value if hasattr(s.day_of_week, "value") else str(s.day_of_week)
        d.course_name  = co.name       if co  else None
        d.course_code  = co.code       if co  else None
        d.faculty_name = fac.full_name if fac else None
        result.append(d)
    return result


# ── Get grid data for visual timetable builder ────────────────────────────────
@router.get("/grid")
def get_timetable_grid(
    branch:   str = Query(...),
    section:  str = Query(...),
    semester: str = Query(...),
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """
    Returns timetable as a grid dict:
    { "monday": { "09:30-10:30": { slot_data }, ... }, ... }
    Used by the visual timetable builder.
    """
    slots = db.query(TimetableSlot).filter(
        TimetableSlot.is_active == True,
        func.lower(TimetableSlot.branch)   == branch.strip().lower(),
        func.lower(TimetableSlot.section)  == section.strip().lower(),
        func.lower(TimetableSlot.semester) == semester.strip().lower(),
    ).all()

    course_ids  = list({s.course_id  for s in slots})
    faculty_ids = list({s.faculty_id for s in slots})
    courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()} if course_ids else {}
    faculty_map = {f.inst_id: f for f in db.query(Faculty).filter(Faculty.inst_id.in_(faculty_ids)).all()} if faculty_ids else {}

    grid = {day: {} for day in DAYS}
    for s in slots:
        day = s.day_of_week.value if hasattr(s.day_of_week, "value") else str(s.day_of_week)
        time_key = f"{s.start_time}-{s.end_time}"
        co  = courses_map.get(s.course_id)
        fac = faculty_map.get(s.faculty_id)
        entry = {
            "id":           s.id,
            "course_id":    s.course_id,
            "course_name":  co.name  if co  else "?",
            "course_code":  co.code  if co  else "?",
            "course_type":  s.course_type or (co.course_type if co else "theory"),
            "faculty_id":   s.faculty_id,
            "faculty_name": fac.full_name if fac else "Unassigned",
            "room":         s.room,
            "sub_section":  s.sub_section,
        }
        # A cell can legitimately hold more than one slot — e.g. two lab
        # batches (C1, C2) running in parallel at the same day/time. Store a
        # LIST per cell so none of them get silently overwritten.
        grid[day].setdefault(time_key, []).append(entry)
    return {"grid": grid, "time_slots": DEFAULT_SLOTS, "days": DAYS}


# ── Create slot ───────────────────────────────────────────────────────────────
@router.post("", response_model=SlotOut, status_code=201)
def create_slot(
    payload: SlotCreate,
    _ = Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    co  = db.query(Course).filter(Course.id == payload.course_id).first()
    if not co:  raise HTTPException(status_code=404, detail="Course not found.")

    # Free classes (Library, Tinkerer, Mentor) don't need a teacher
    FREE_CODES = {"LIBRARY", "TINKERER", "MENTOR", "CODING", "FREE"}
    is_free = co.code.upper() in FREE_CODES or co.credits == 0

    fac = None
    if payload.faculty_id:
        fac = db.query(Faculty).filter(Faculty.inst_id == payload.faculty_id).first()
        if not fac:
            raise HTTPException(status_code=404, detail="Faculty not found.")
    elif not is_free:
        raise HTTPException(status_code=400, detail="Please assign a teacher for this class.")

    data = payload.model_dump()
    data["day_of_week"] = data["day_of_week"].strip().lower()

    slot = TimetableSlot(**data)
    db.add(slot); db.commit(); db.refresh(slot)
    d = SlotOut.model_validate(slot)
    d.day_of_week  = slot.day_of_week.value if hasattr(slot.day_of_week, "value") else str(slot.day_of_week)
    d.course_name  = co.name
    d.course_code  = co.code
    d.faculty_name = fac.full_name if fac else None
    return d


# ── Update slot ───────────────────────────────────────────────────────────────
@router.patch("/{slot_id}", response_model=SlotOut)
def update_slot(
    slot_id: int,
    payload: SlotCreate,
    _ = Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
    if not slot: raise HTTPException(status_code=404, detail="Slot not found.")
    co  = db.query(Course).filter(Course.id == payload.course_id).first()
    if not co:  raise HTTPException(status_code=404, detail="Course not found.")

    # Free classes (Library, Tinkerer, Mentor) don't need a teacher — same rule as create_slot
    FREE_CODES = {"LIBRARY", "TINKERER", "MENTOR", "CODING", "FREE"}
    is_free = co.code.upper() in FREE_CODES or co.credits == 0

    fac = None
    if payload.faculty_id:
        fac = db.query(Faculty).filter(Faculty.inst_id == payload.faculty_id).first()
        if not fac:
            raise HTTPException(status_code=404, detail="Faculty not found.")
    elif not is_free:
        raise HTTPException(status_code=400, detail="Please assign a teacher for this class.")

    for k, v in payload.model_dump().items():
        if k == "day_of_week": v = v.strip().lower()
        setattr(slot, k, v)
    db.commit(); db.refresh(slot)

    d = SlotOut.model_validate(slot)
    d.day_of_week  = slot.day_of_week.value if hasattr(slot.day_of_week, "value") else str(slot.day_of_week)
    d.course_name  = co.name
    d.course_code  = co.code
    d.faculty_name = fac.full_name if fac else None
    return d


# ── Delete slot ───────────────────────────────────────────────────────────────
@router.delete("/{slot_id}", status_code=204)
def delete_slot(slot_id: int, _ = Depends(AdminOnly), db: DBSession = Depends(get_db)):
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
    if not slot: raise HTTPException(status_code=404, detail="Slot not found.")
    db.delete(slot); db.commit()


# ── Copy timetable ────────────────────────────────────────────────────────────
@router.post("/copy")
def copy_timetable(
    payload: CopyTimetableRequest,
    _ = Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    """Copy all slots from one section to another. Optionally copy teachers."""
    slots = db.query(TimetableSlot).filter(
        TimetableSlot.is_active == True,
        func.lower(TimetableSlot.branch)   == payload.from_branch.strip().lower(),
        func.lower(TimetableSlot.section)  == payload.from_section.strip().lower(),
        func.lower(TimetableSlot.semester) == payload.from_semester.strip().lower(),
    ).all()

    if not slots:
        raise HTTPException(status_code=404, detail="No slots found for source section.")

    # Delete existing slots in destination if any
    db.query(TimetableSlot).filter(
        TimetableSlot.is_active == True,
        func.lower(TimetableSlot.branch)   == payload.to_branch.strip().lower(),
        func.lower(TimetableSlot.section)  == payload.to_section.strip().lower(),
        func.lower(TimetableSlot.semester) == payload.to_semester.strip().lower(),
    ).delete(synchronize_session=False)

    # No admin/faculty placeholder — an unassigned slot just has faculty_id=None,
    # matching the free-period rule (faculty_id is nullable on TimetableSlot).
    placeholder_id = None

    new_slots = []
    for s in slots:
        new_slot = TimetableSlot(
            course_id   = s.course_id,
            faculty_id  = s.faculty_id if payload.copy_teachers else placeholder_id,
            day_of_week = s.day_of_week,
            start_time  = s.start_time,
            end_time    = s.end_time,
            room        = s.room,
            branch      = payload.to_branch,
            section     = payload.to_section,
            sub_section = s.sub_section,
            semester    = payload.to_semester,
            course_type = s.course_type,
            is_active   = True,
        )
        db.add(new_slot)
        new_slots.append(new_slot)

    db.commit()
    return {
        "copied":  len(new_slots),
        "message": f"Copied {len(new_slots)} slots to {payload.to_branch} Section {payload.to_section}.",
        "note":    "Teachers are unassigned — please assign them in the timetable editor." if not payload.copy_teachers else "Teachers copied from source section.",
    }


# ── Conflict check ────────────────────────────────────────────────────────────
@router.get("/conflicts")
def check_conflicts(
    branch:   Optional[str] = None,
    section:  Optional[str] = None,
    semester: Optional[str] = None,
    _ = Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    """Check for teacher conflicts — same teacher at same time in different places."""
    q = db.query(TimetableSlot).filter(TimetableSlot.is_active == True)
    if branch:   q = q.filter(func.lower(TimetableSlot.branch)   == branch.strip().lower())
    if section:  q = q.filter(func.lower(TimetableSlot.section)  == section.strip().lower())
    if semester: q = q.filter(func.lower(TimetableSlot.semester) == semester.strip().lower())
    slots = q.all()

    faculty_ids = list({s.faculty_id for s in slots})
    faculty_map = {f.inst_id: f for f in db.query(Faculty).filter(Faculty.inst_id.in_(faculty_ids)).all()}
    course_ids  = list({s.course_id  for s in slots})
    courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()}

    # Group by faculty + day + time
    from collections import defaultdict
    schedule = defaultdict(list)
    for s in slots:
        day = s.day_of_week.value if hasattr(s.day_of_week, "value") else str(s.day_of_week)
        key = (s.faculty_id, day, s.start_time)
        schedule[key].append(s)

    conflicts = []
    for (fac_id, day, time), slot_list in schedule.items():
        if len(slot_list) > 1:
            fac = faculty_map.get(fac_id)
            conflicts.append({
                "teacher":    fac.full_name if fac else f"Faculty {fac_id}",
                "day":        day,
                "time":       time,
                "sections":   [f"Sec {s.section}" for s in slot_list],
                "subjects":   [courses_map.get(s.course_id, type('x', (), {'name':'?'})()).name for s in slot_list],
            })

    return {"conflicts": conflicts, "total": len(conflicts)}


# ── Bulk timetable import ────────────────────────────────────────────────────
import io
from fastapi import UploadFile, File
from fastapi.responses import Response

BULK_HEADERS = ["Day", "StartTime", "EndTime", "Branch", "Section", "Semester",
                "SubjectCode", "FacultyID", "Room", "LabBatch", "ClassType"]

@router.get("/bulk-template")
def download_bulk_template(_=Depends(AdminOnly)):
    """Download an Excel template for bulk timetable import."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Timetable"
    ws.append(BULK_HEADERS)
    ws.append(["monday", "09:30", "10:30", "CSE (AI-ML-DL)", "C", "3rd",
               "EAS211", "TMU003", "4115", "", "theory"])
    ws.append(["monday", "14:25", "15:10", "CSE (AI-ML-DL)", "C", "3rd",
               "EAS262", "TMU005", "Lab-2", "C1", "lab"])
    ws.append(["monday", "14:25", "15:10", "CSE (AI-ML-DL)", "C", "3rd",
               "EAS262", "TMU007", "Lab-3", "C2", "lab"])
    ws.append(["tuesday", "09:30", "10:30", "CSE (AI-ML-DL)", "C", "3rd",
               "LIBRARY", "", "", "", "theory"])
    # widen columns a bit so the header text isn't cut off
    for i, h in enumerate(BULK_HEADERS, start=1):
        ws.column_dimensions[chr(64+i)].width = max(12, len(h)+2)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=timetable_template.xlsx"}
    )


@router.post("/bulk-import")
async def bulk_import_timetable(
    file: UploadFile = File(...),
    clear_existing: bool = Query(False),
    _=Depends(AdminOnly),
    db: DBSession = Depends(get_db),
):
    """
    Bulk-create timetable slots from the Excel/CSV template.
    Required columns: Day, StartTime, EndTime, Branch, Section, Semester, SubjectCode
    Optional columns: FacultyID (blank = free class like Library), Room, LabBatch, ClassType
    """
    filename = (file.filename or "").lower()
    rows = []  # list of dicts, one per data row
    raw = await file.read()

    if filename.endswith(".csv"):
        import csv
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        reader = csv.reader(io.StringIO(text))
        all_rows = list(reader)
    elif filename.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True)
        ws = wb.active
        all_rows = [[c.value for c in row] for row in ws.iter_rows()]
    else:
        raise HTTPException(status_code=400, detail="Only .xlsx or .csv files are supported (.xls is not — save as .xlsx first).")

    if not all_rows:
        raise HTTPException(status_code=400, detail="File is empty.")

    header = [str(h).strip() if h is not None else "" for h in all_rows[0]]
    # Map header names to column index, case-insensitively
    col_idx = {h.lower(): i for i, h in enumerate(header)}
    required = ["day", "starttime", "endtime", "branch", "section", "semester", "subjectcode"]
    missing = [r for r in required if r not in col_idx]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required column(s): {', '.join(missing)}. Expected headers: {', '.join(BULK_HEADERS)}")

    def cell(row, name, default=""):
        idx = col_idx.get(name)
        if idx is None or idx >= len(row) or row[idx] is None:
            return default
        return str(row[idx]).strip()

    # Pre-fetch courses/faculty once instead of querying per-row
    all_courses = {c.code.upper(): c for c in db.query(Course).all()}
    all_faculty = {f.inst_id.upper(): f for f in db.query(Faculty).all()}

    touched_scopes = set()   # (branch, section, semester) combos seen — for clear_existing
    to_create = []
    errors = []

    for i, raw_row in enumerate(all_rows[1:], start=2):  # row 1 = header
        if not any(raw_row):
            continue  # skip fully blank rows
        day       = cell(raw_row, "day").lower()
        start     = cell(raw_row, "starttime")
        end       = cell(raw_row, "endtime")
        branch    = cell(raw_row, "branch")
        section   = cell(raw_row, "section")
        semester  = cell(raw_row, "semester")
        code      = cell(raw_row, "subjectcode").upper()
        fac_id    = cell(raw_row, "facultyid")
        room      = cell(raw_row, "room")
        lab_batch = cell(raw_row, "labbatch")
        ctype     = cell(raw_row, "classtype").lower()

        if day not in DAYS:
            errors.append(f"Row {i}: invalid day '{day}' (expected one of {', '.join(DAYS)})")
            continue
        if not re.match(r'^\d{1,2}:\d{2}$', start) or not re.match(r'^\d{1,2}:\d{2}$', end):
            errors.append(f"Row {i}: invalid time format (expected HH:MM), got '{start}'-'{end}'")
            continue
        if not code or code not in all_courses:
            errors.append(f"Row {i}: unknown subject code '{code}'")
            continue
        co = all_courses[code]

        fac = None
        if fac_id:
            fac = all_faculty.get(fac_id.upper())
            if not fac:
                errors.append(f"Row {i}: unknown faculty ID '{fac_id}'")
                continue

        FREE_CODES = {"LIBRARY", "TINKERER", "MENTOR", "CODING", "FREE"}
        is_free = code in FREE_CODES or co.credits == 0
        if not fac and not is_free:
            errors.append(f"Row {i}: no FacultyID given for '{code}' — either provide one or leave it for free-period subjects (Library, Tinkerer, etc.)")
            continue

        touched_scopes.add((branch.lower(), section.lower(), semester.lower()))
        to_create.append(TimetableSlot(
            course_id=co.code, faculty_id=(fac.inst_id if fac else None),
            day_of_week=day, start_time=start, end_time=end,
            room=room or None, branch=branch, section=section, semester=semester,
            sub_section=lab_batch or None,
            course_type=ctype if ctype in ("theory", "lab") else (co.course_type or "theory"),
        ))

    if clear_existing and touched_scopes:
        for branch, section, semester in touched_scopes:
            db.query(TimetableSlot).filter(
                func.lower(TimetableSlot.branch) == branch,
                func.lower(TimetableSlot.section) == section,
                func.lower(TimetableSlot.semester) == semester,
            ).delete(synchronize_session=False)

    for slot in to_create:
        db.add(slot)
    db.commit()

    return {"added": len(to_create), "errors": errors}


# ── Go Live ───────────────────────────────────────────────────────────────────
@router.post("/{slot_id}/go-live")
def go_live(
    slot_id: int,
    payload: GoLiveRequest,
    current_user = Depends(require_roles(UserRole.faculty)),
    db: DBSession = Depends(get_db),
):
    import secrets
    slot = db.query(TimetableSlot).filter(TimetableSlot.id == slot_id).first()
    if not slot: raise HTTPException(status_code=404, detail="Slot not found.")
    # Free-period slots (Library, Coding Practice, etc.) have no assigned
    # teacher — any faculty can go live for them. Assigned slots still
    # require the owning faculty.
    if slot.faculty_id is not None and slot.faculty_id != current_user.inst_id:
        raise HTTPException(status_code=403, detail="This slot is not assigned to you.")

    active = db.query(Session).filter(
        Session.faculty_id == current_user.inst_id,
        Session.status     == SessionStatus.active,
    ).first()
    if active:
        raise HTTPException(status_code=400, detail="You already have an active session. End it first.")

    co  = db.query(Course).filter(Course.id == slot.course_id).first()
    now = datetime.utcnow()

    try:
        import pytz
        ist = pytz.timezone("Asia/Kolkata")
        ist_now = datetime.now(ist).replace(tzinfo=None)
    except ImportError:
        from datetime import timedelta
        ist_now = now + timedelta(hours=5, minutes=30)

    day_name = DAYS[ist_now.weekday()] if ist_now.weekday() < 6 else "saturday"
    slot_day = slot.day_of_week.value if hasattr(slot.day_of_week, "value") else str(slot.day_of_week)

    if slot_day.lower() != day_name.lower():
        raise HTTPException(status_code=400, detail=f"This slot is for {slot_day.title()}, not today ({day_name.title()}).")

    sh, sm = map(int, slot.start_time.split(":"))
    eh, em = map(int, slot.end_time.split(":"))
    now_m  = ist_now.hour * 60 + ist_now.minute
    slot_start_m = sh * 60 + sm
    slot_end_m   = eh * 60 + em

    if now_m < slot_start_m - 10:
        raise HTTPException(status_code=400, detail=f"Too early! Class starts at {slot.start_time}.")
    if now_m > slot_end_m:
        raise HTTPException(status_code=400, detail="Class time has passed.")

    session = Session(
        course_id    = slot.course_id,
        # Session.faculty_id is required (attendance/audit trail needs a
        # responsible person on record) — for a free period, that's whoever
        # actually went live, not the slot's (missing) assigned teacher.
        faculty_id   = slot.faculty_id if slot.faculty_id is not None else current_user.inst_id,
        timetable_id = slot.id,
        title        = f"{co.name if co else 'Class'} - {slot.section} {slot.start_time}",
        location     = slot.room,
        branch       = slot.branch,
        section      = slot.section,
        sub_section  = slot.sub_section,
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
    return {
        "session_id": session.id,
        "qr_token":   session.qr_token,
        "title":      session.title,
        "message":    "Session is now LIVE!",
    }
