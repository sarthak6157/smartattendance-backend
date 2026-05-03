"""Pydantic schemas v2 — branch, section, face, GPS."""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr
from models.models import AttendanceMethod, AttendanceStatus, SessionStatus, UserRole, UserStatus


class LoginRequest(BaseModel):
    credential: str
    password: str

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
    id: int
    full_name: str
    inst_id: str
    email: str
    role: UserRole
    status: UserStatus
    department: Optional[str]
    branch: Optional[str]
    section: Optional[str]
    semester: Optional[str]
    face_registered: bool = False
    face_embedding:  Optional[str] = None   # 128-float JSON array for client-side matching
    created_at: datetime
    last_login: Optional[datetime]
    class Config:
        from_attributes = True

class UserListOut(BaseModel):
    total: int
    users: List[UserOut]

class CourseCreate(BaseModel):
    code: str
    name: str
    department: Optional[str] = None
    branch:     Optional[str] = None
    section:    Optional[str] = None
    semester:   Optional[str] = None
    credits: int = 3

class CourseOut(BaseModel):
    id: int
    code: str
    name: str
    department: Optional[str]
    branch:     Optional[str]
    section:    Optional[str]
    semester:   Optional[str]
    credits: int
    class Config:
        from_attributes = True

class SessionCreate(BaseModel):
    course_id: int
    title: Optional[str] = None
    location: Optional[str] = None
    scheduled_at: datetime
    grace_minutes: int = 15
    gps_lat: Optional[str] = None
    gps_lng: Optional[str] = None

class SessionOut(BaseModel):
    id: int
    course_id: int
    faculty_id: int
    title: Optional[str]
    qr_token: Optional[str]
    location: Optional[str]
    branch:   Optional[str]
    section:  Optional[str]
    gps_lat: Optional[str]
    gps_lng: Optional[str]
    status: SessionStatus
    scheduled_at: datetime
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    grace_minutes: int
    created_at: datetime
    class Config:
        from_attributes = True

class SessionListOut(BaseModel):
    total: int
    sessions: List[SessionOut]

class AttendanceMarkQR(BaseModel):
    qr_token: str
    student_lat: Optional[str] = None
    student_lng: Optional[str] = None

class AttendanceMarkManual(BaseModel):
    session_id: int
    student_id: int
    status: AttendanceStatus = AttendanceStatus.present
    notes: Optional[str] = None

class AttendanceOut(BaseModel):
    id: int
    session_id: int
    student_id: int
    method: AttendanceMethod
    status: AttendanceStatus
    marked_at: datetime
    notes: Optional[str]
    class Config:
        from_attributes = True

class AttendanceListOut(BaseModel):
    total: int
    records: List[AttendanceOut]

class SettingsOut(BaseModel):
    gps_range: int
    face_required: bool
    qr_expiry: int
    inst_name: str
    class Config:
        from_attributes = True

class SettingsUpdate(BaseModel):
    gps_range:     Optional[int]  = None
    face_required: Optional[bool] = None
    qr_expiry:     Optional[int]  = None
    inst_name:     Optional[str]  = None

TokenResponse.model_rebuild()

# Timetable schemas already handled inside timetable.py router directly
