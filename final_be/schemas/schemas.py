"""Pydantic schemas v2 — branch, section, face, GPS."""
from datetime import datetime
from typing import List, Optional, Union
from pydantic import BaseModel, EmailStr
from models.models import AttendanceMethod, AttendanceStatus, SessionStatus, UserRole, UserStatus


class LoginRequest(BaseModel):
    credential: str
    password: str
    role: UserRole   # which table to look in: student / faculty / admin

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

class UserCreate(BaseModel):
    full_name: str
    inst_id: str
    email: EmailStr
    role: UserRole = UserRole.student
    password: str
    department: Optional[str] = None
    branch: Optional[str] = None
    section: Optional[str] = None
    semester: Optional[str] = None
    course_type: Optional[str] = None

class UserUpdate(BaseModel):
    full_name:  Optional[str] = None
    email:      Optional[EmailStr] = None
    department: Optional[str] = None
    branch:     Optional[str] = None
    section:    Optional[str] = None
    semester:   Optional[str] = None
    course:     Optional[str] = None

class UserStatusUpdate(BaseModel):
    status: UserStatus

class FaceRegisterRequest(BaseModel):
    image_b64:       str            # base64 encoded face image from camera
    face_descriptor: Optional[str] = None  # JSON array of 128 floats from face-api.js

class UserOut(BaseModel):
    # "id" is now a purely cosmetic per-role display field — "ST006" for
    # students, a plain serial number for faculty, absent (None) for admins.
    # It is NEVER the login credential or a foreign key — that's inst_id.
    id: Optional[Union[str, int]] = None
    full_name: str
    inst_id: str
    email: str
    role: UserRole
    status: UserStatus
    department: Optional[str] = None
    branch:     Optional[str] = None
    section:    Optional[str] = None
    sub_section:Optional[str] = None   # lab batch e.g. A1, A2
    semester:   Optional[str] = None
    course:     Optional[str] = None   # degree type e.g. B.Tech
    face_registered: bool = False
    face_embedding:  Optional[str] = None
    created_at: datetime
    last_login: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    model_config = {"from_attributes": True, "use_enum_values": True}

class UserListOut(BaseModel):
    total: int
    users: List[UserOut]

class CourseCreate(BaseModel):
    code: str
    name: str
    department:  Optional[str] = None
    branch:      Optional[str] = None
    section:     Optional[str] = None
    semester:    Optional[str] = None
    course_type: Optional[str] = None   # theory or lab
    credits: int = 3

class CourseOut(BaseModel):
    id: str
    code: str
    name: str
    department:  Optional[str] = None
    branch:      Optional[str] = None
    section:     Optional[str] = None
    semester:    Optional[str] = None
    course_type: Optional[str] = None
    credits: int = 3
    model_config = {"from_attributes": True, "use_enum_values": True}

class SessionCreate(BaseModel):
    course_id: str
    title: Optional[str] = None
    location: Optional[str] = None
    scheduled_at: datetime
    grace_minutes: int = 15
    gps_lat: Optional[str] = None
    gps_lng: Optional[str] = None

class SessionOut(BaseModel):
    id: int
    course_id: str
    faculty_id: str
    timetable_id: Optional[int] = None
    title: Optional[str] = None
    qr_token: Optional[str] = None
    location: Optional[str] = None
    branch:      Optional[str] = None
    section:     Optional[str] = None
    sub_section: Optional[str] = None
    semester:    Optional[str] = None
    gps_lat: Optional[str] = None
    gps_lng: Optional[str] = None
    status: SessionStatus
    scheduled_at: datetime
    started_at:  Optional[datetime] = None
    ended_at:    Optional[datetime] = None
    grace_minutes: int = 15
    created_at: datetime
    model_config = {"from_attributes": True, "use_enum_values": True}

class SessionListOut(BaseModel):
    total: int
    sessions: List[SessionOut]

class AttendanceMarkQR(BaseModel):
    qr_token: str
    student_lat: Optional[str] = None
    student_lng: Optional[str] = None
    # New: 128-float face-api.js descriptor, compared server-side against
    # the student's registered embedding — previously the server trusted
    # the browser's own "face matched" claim with no verification at all.
    face_descriptor: Optional[List[float]] = None
    # New: a client-generated, localStorage-persisted random id (NOT a
    # hardware fingerprint — just enough to notice "the same browser just
    # marked five different students in one class").
    device_id: Optional[str] = None

class AttendanceMarkManual(BaseModel):
    session_id: int
    student_id: str
    status: AttendanceStatus = AttendanceStatus.present
    notes: Optional[str] = None

class AttendanceOut(BaseModel):
    id: int
    session_id: int
    student_id: str
    method: AttendanceMethod
    status: AttendanceStatus
    marked_at: datetime
    notes: Optional[str]
    model_config = {"from_attributes": True, "use_enum_values": True}

class AttendanceListOut(BaseModel):
    total: int
    records: List[AttendanceOut]

class SettingsOut(BaseModel):
    gps_range: int
    face_required: bool
    qr_expiry: int
    inst_name: str
    auto_notify_on_go_live: bool = True
    model_config = {"from_attributes": True, "use_enum_values": True}

class SettingsUpdate(BaseModel):
    gps_range:     Optional[int]  = None
    face_required: Optional[bool] = None
    qr_expiry:     Optional[int]  = None
    inst_name:     Optional[str]  = None
    auto_notify_on_go_live: Optional[bool] = None

TokenResponse.model_rebuild()

# ── New feature schemas ──────────────────────────────────────────────────────

class QRLiveOut(BaseModel):
    """Response for GET /sessions/{id}/qr-live — the rotating QR payload the
    faculty display page polls and re-renders every few seconds."""
    session_id: int
    qr_payload: str          # what actually goes INTO the QR code image
    rotates_every: int       # seconds
    seconds_remaining: int

class AttendanceFlagOut(BaseModel):
    id: int
    session_id: int
    student_id: Optional[str]
    reason: str
    severity: str
    resolved: bool
    created_at: datetime
    model_config = {"from_attributes": True}

class FlagResolveRequest(BaseModel):
    resolved: bool = True

class DefaulterOut(BaseModel):
    student_id: str
    full_name: str
    course_id: str
    course_name: str
    present: int
    total: int
    percent: float
    model_config = {"from_attributes": True}

class LeaveCreate(BaseModel):
    from_date: datetime
    to_date: datetime
    leave_type: str = "leave"  # OD removed — "leave" is the only type now
    reason: str

class LeaveReview(BaseModel):
    status: str  # "approved" | "rejected"
    review_note: Optional[str] = None

class LeaveOut(BaseModel):
    id: int
    student_id: Optional[str] = None
    faculty_id: Optional[str] = None
    from_date: datetime
    to_date: datetime
    leave_type: str
    reason: str
    status: str
    reviewed_by: Optional[str]
    review_note: Optional[str]
    created_at: datetime
    model_config = {"from_attributes": True}

class LeaveListOut(BaseModel):
    total: int
    requests: List[LeaveOut]

class AssignSubstituteRequest(BaseModel):
    timetable_slot_id: int
    class_date: datetime
    substitute_faculty_id: str

class SubstituteAssignmentOut(BaseModel):
    id: int
    leave_request_id: int
    timetable_slot_id: int
    class_date: datetime
    original_faculty_id: str
    substitute_faculty_id: str
    course_id: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}

# Timetable schemas already handled inside timetable.py router directly
