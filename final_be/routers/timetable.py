"""Timetable routes — visual grid builder, copy, conflict detection."""
import re
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import (TimetableSlot, Session, SessionStatus,
                           Faculty, Student, Admin, UserRole, Course, DayOfWeek, SystemSettings)

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
        # Faculty only ever see classes actually assigned to them. Unassigned
        # "free period" slots (Library, Mentor Interaction, etc.) are NOT
        # shown here — a faculty claims a Mentor Interaction slot explicitly
        # via POST /timetable/claim-mentor-slot, which assigns it to them
        # directly, at which point it shows up like any other class.
        q = q.filter(TimetableSlot.faculty_id == current_user.inst_id)
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
        # BUG FIX: func.strpos() is Postgres-only — this whole query 500'd
        # on the SQLite fallback used when DATABASE_URL isn't set (e.g.
        # local dev). Same substring-match semantics via LIKE, which both
        # Postgres and SQLite support.
        lower_branch = func.lower(TimetableSlot.branch)
        conditions = [
            TimetableSlot.branch == None,
            TimetableSlot.branch == "",
            lower_branch == eb,
            lower_branch.like(f"%{eb}%"),
        ]
        if eb_core:  conditions.append(lower_branch.like(f"%{eb_core}%"))
        if eb_short: conditions.append(lower_branch.like(f"%{eb_short}%"))
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
    """Check for conflicts: same teacher OR same room double-booked at the same time.

    BUG FIX (found on audit): this used to query only the slots matching
    the branch/section/semester filter, then look for conflicts WITHIN
    that filtered set — so a room double-booked between, say, a CSE
    section and an ECE section would be completely invisible whenever
    anyone checked conflicts scoped to just one branch (which is the
    normal way the frontend calls this). Teacher conflicts had the exact
    same gap: a faculty member double-booked across two different
    branches wouldn't show up either. Since a room or a teacher is a
    university-wide resource, not a per-branch one, this now always
    builds the conflict map from EVERY active slot, and only uses the
    branch/section/semester filter to narrow which conflicts are
    reported (so the caller still gets a manageable list when editing
    one branch's timetable, without silently hiding the cross-branch
    conflicts that are usually the real problem)."""
    all_slots = db.query(TimetableSlot).filter(TimetableSlot.is_active == True).all()

    # Simple Python-side filter (cheap: a semester's worth of slots is
    # never large enough to need this pushed into SQL) rather than
    # re-querying — narrows which conflicts get REPORTED, without
    # narrowing which slots get COMPARED against each other (see note above).
    def in_scope(s):
        if branch   and (s.branch   or "").strip().lower()   != branch.strip().lower():   return False
        if section  and (s.section  or "").strip().lower()   != section.strip().lower():  return False
        if semester and (s.semester or "").strip().lower()   != semester.strip().lower(): return False
        return True

    slots = all_slots  # full set — used to BUILD the conflict map
    faculty_ids = list({s.faculty_id for s in slots if s.faculty_id})
    faculty_map = {f.inst_id: f for f in db.query(Faculty).filter(Faculty.inst_id.in_(faculty_ids)).all()} if faculty_ids else {}
    course_ids  = list({s.course_id  for s in slots})
    courses_map = {c.id: c for c in db.query(Course).filter(Course.id.in_(course_ids)).all()} if course_ids else {}

    # Group by faculty + day + time — skip unassigned (faculty_id is None)
    # slots entirely: multiple different free periods (Library in one
    # section, unclaimed Mentor Interaction in another) can legitimately
    # share the same day/time with no teacher assigned to either, and
    # grouping them under the same "None" key would falsely report that
    # as a conflict for a nonexistent teacher.
    from collections import defaultdict
    teacher_schedule = defaultdict(list)
    # NEW FEATURE: room/venue conflicts — same code path, grouped by room
    # instead of faculty. Two different sections both scheduled in the
    # same physical room at the same time is just as real a scheduling
    # problem as the same teacher being double-booked, and the previous
    # version never caught it at all. Blank/unset rooms are skipped for
    # the same reason unassigned faculty are — many free periods
    # legitimately have no room recorded, and grouping those under one
    # "" key would flood this with false positives.
    room_schedule = defaultdict(list)
    for s in slots:
        day = s.day_of_week.value if hasattr(s.day_of_week, "value") else str(s.day_of_week)
        if s.faculty_id:
            teacher_schedule[(s.faculty_id, day, s.start_time)].append(s)
        room = (s.room or "").strip()
        if room:
            room_schedule[(room.lower(), day, s.start_time)].append(s)

    conflicts = []
    any_filter = bool(branch or section or semester)
    for (fac_id, day, time), slot_list in teacher_schedule.items():
        if len(slot_list) > 1 and (not any_filter or any(in_scope(s) for s in slot_list)):
            fac = faculty_map.get(fac_id)
            conflicts.append({
                "type":       "teacher",
                "teacher":    fac.full_name if fac else f"Faculty {fac_id}",
                "day":        day,
                "time":       time,
                "sections":   [f"Sec {s.section}" for s in slot_list],
                "subjects":   [courses_map.get(s.course_id, type('x', (), {'name':'?'})()).name for s in slot_list],
            })
    for (room, day, time), slot_list in room_schedule.items():
        if len(slot_list) > 1 and (not any_filter or any(in_scope(s) for s in slot_list)):
            # Two sections sharing a room is only a real conflict if
            # they're not literally the same section/course slot
            # duplicated (e.g. a combined lecture) — check faculty differs
            # OR section differs to avoid flagging a single legitimate
            # shared class as a "conflict".
            distinct = {(s.section, s.course_id) for s in slot_list}
            if len(distinct) > 1:
                conflicts.append({
                    "type":       "room",
                    "room":       slot_list[0].room,
                    "day":        day,
                    "time":       time,
                    "sections":   [f"Sec {s.section}" for s in slot_list],
                    "subjects":   [courses_map.get(s.course_id, type('x', (), {'name':'?'})()).name for s in slot_list],
                    "teachers":   [(faculty_map.get(s.faculty_id).full_name if s.faculty_id and faculty_map.get(s.faculty_id) else "Unassigned") for s in slot_list],
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


# ── Faculty: claim Mentor Interaction for their own section(s) ─────────────
@router.post("/claim-mentor-slot")
def claim_mentor_slot(
    current_user = Depends(require_roles(UserRole.faculty)),
    db: DBSession = Depends(get_db),
):
    """
    A faculty member claims the 9:10-9:25 'Mentor Interaction' period for
    every branch/section/semester they already teach (inferred from their
    existing assigned slots) — creating a dedicated slot for each of their
    weekday classes if one doesn't already exist there, or taking over an
    unassigned one left by the admin's global bulk setup. Never touches a
    slot another faculty has already claimed.
    """
    mentor_course = db.query(Course).filter(func.upper(Course.code) == "MENTOR").first()
    if not mentor_course:
        raise HTTPException(status_code=404, detail="No 'Mentor Interaction' course exists yet — ask an admin to set it up first (Timetable → Setup Mentor Interaction).")

    my_scopes = db.query(TimetableSlot.branch, TimetableSlot.section, TimetableSlot.semester)\
                  .filter(TimetableSlot.faculty_id == current_user.inst_id,
                          TimetableSlot.branch  != None, TimetableSlot.branch  != "",
                          TimetableSlot.section != None, TimetableSlot.section != "")\
                  .distinct().all()
    if not my_scopes:
        raise HTTPException(status_code=400, detail="You don't have any assigned classes yet, so there's no section to set up Mentor Interaction for.")

    claimed, created, skipped = [], [], []
    for branch, section, semester in my_scopes:
        label = f"{branch} - Sec {section} ({semester or 'all sem'})"
        for day in DAYS:
            existing = db.query(TimetableSlot).filter(
                TimetableSlot.course_id == mentor_course.code,
                TimetableSlot.day_of_week == day,
                TimetableSlot.start_time == "09:10",
                func.lower(TimetableSlot.branch)  == branch.strip().lower(),
                func.lower(TimetableSlot.section) == section.strip().lower(),
                func.lower(func.coalesce(TimetableSlot.semester, '')) == (semester or '').strip().lower(),
            ).first()
            if existing:
                if existing.faculty_id and existing.faculty_id != current_user.inst_id:
                    skipped.append(label)
                    continue
                existing.faculty_id = current_user.inst_id
            else:
                db.add(TimetableSlot(
                    course_id=mentor_course.code, faculty_id=current_user.inst_id,
                    day_of_week=day, start_time="09:10", end_time="09:25",
                    branch=branch, section=section, semester=semester,
                    course_type="activity",
                ))
                created.append(day)
        if label not in skipped:
            claimed.append(label)

    db.commit()
    return {
        "claimed_sections": claimed,
        "skipped_sections": list(set(skipped)),
        "slots_created": len(created),
    }


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

    # NEW FEATURE: auto-notify students the instant class goes live —
    # previously notify_session_live() existed as an endpoint but nothing
    # ever called it, so it only fired if a faculty member remembered to
    # trigger it by hand. Respects the global admin toggle; never lets a
    # notification failure block the session actually going live.
    settings = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not settings or settings.auto_notify_on_go_live is not False:  # None == default-on
        try:
            from routers.notifications import notify_students_session_live
            notify_students_session_live(db, session)
        except Exception:
            pass  # best-effort — a push-notification hiccup must never break go-live

    return {
        "session_id": session.id,
        "qr_token":   session.qr_token,
        "title":      session.title,
        "message":    "Session is now LIVE!",
    }
