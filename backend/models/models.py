"""SQLAlchemy ORM models — v3 with timetable, branch/section/face/GPS."""
from datetime import datetime
import enum
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from db.database import Base


class UserRole(str, enum.Enum):
    student = "student"
    faculty = "faculty"
    admin   = "admin"

class UserStatus(str, enum.Enum):
    pending  = "pending"
    active   = "active"
    inactive = "inactive"

class AttendanceMethod(str, enum.Enum):
    qr          = "qr"
    qr_gps_face = "qr+gps+face"
    manual      = "manual"

class AttendanceStatus(str, enum.Enum):
    present = "present"
    absent  = "absent"
    late    = "late"

class SessionStatus(str, enum.Enum):
    scheduled = "scheduled"
    active    = "active"
    closed    = "closed"

class DayOfWeek(str, enum.Enum):
    monday    = "monday"
    tuesday   = "tuesday"
    wednesday = "wednesday"
    thursday  = "thursday"
    friday    = "friday"
    saturday  = "saturday"


class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True, index=True)
    full_name       = Column(String(120), nullable=False)
    inst_id         = Column(String(50), unique=True, nullable=False, index=True)
    email           = Column(String(150), unique=True, nullable=False, index=True)
    role            = Column(Enum(UserRole), default=UserRole.student, nullable=False)
    status          = Column(Enum(UserStatus), default=UserStatus.pending, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    department      = Column(String(150), nullable=True)
    branch          = Column(String(150), nullable=True)
    section         = Column(String(20),  nullable=True)
    semester        = Column(String(20),  nullable=True)
    course          = Column(String(100), nullable=True)   # e.g. "B.Tech"
    sub_section     = Column(String(20),  nullable=True)   # e.g. "A1", "A2" — lab batch  ← ADDED
    face_registered = Column(Boolean, default=False)
    face_embedding  = Column(Text, nullable=True)
    face_image_b64  = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)

    sessions_taught    = relationship("Session", back_populates="faculty")
    attendance_records = relationship("AttendanceRecord", back_populates="student",
                                      foreign_keys="AttendanceRecord.student_id")
    timetable_slots    = relationship("TimetableSlot", back_populates="faculty")


class Course(Base):
    __tablename__ = "courses"
    id         = Column(Integer, primary_key=True, index=True)
    code       = Column(String(20), unique=True, nullable=False)
    name       = Column(String(150), nullable=False)
    department = Column(String(150), nullable=True)
    branch     = Column(String(150), nullable=True)
    section    = Column(String(20),  nullable=True)
    semester   = Column(String(20),  nullable=True)
    course_type= Column(String(100), nullable=True)   # e.g. "B.Tech"
    credits    = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.utcnow)
    sessions   = relationship("Session", back_populates="course")
    timetable_slots = relationship("TimetableSlot", back_populates="course")


class TimetableSlot(Base):
    """A recurring weekly class slot — created by admin."""
    __tablename__ = "timetable_slots"
    id          = Column(Integer, primary_key=True, index=True)
    course_id   = Column(Integer, ForeignKey("courses.id"), nullable=False)
    faculty_id  = Column(Integer, ForeignKey("users.id"),   nullable=False)
    day_of_week = Column(Enum(DayOfWeek), nullable=False)   # Monday–Saturday
    start_time  = Column(String(10), nullable=False)         # "09:00"
    end_time    = Column(String(10), nullable=False)         # "10:00"
    room        = Column(String(100), nullable=True)
    branch      = Column(String(150), nullable=True)
    section     = Column(String(20),  nullable=True)
    sub_section = Column(String(20),  nullable=True)         # e.g. "A1","A2" for labs only
    semester    = Column(String(20),  nullable=True)
    course_type = Column(String(100), nullable=True)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    course  = relationship("Course", back_populates="timetable_slots")
    faculty = relationship("User",   back_populates="timetable_slots")


class Session(Base):
    """A live attendance session — created from a timetable slot."""
    __tablename__ = "sessions"
    id            = Column(Integer, primary_key=True, index=True)
    course_id     = Column(Integer, ForeignKey("courses.id"), nullable=False)
    faculty_id    = Column(Integer, ForeignKey("users.id"),   nullable=False)
    timetable_id  = Column(Integer, ForeignKey("timetable_slots.id"), nullable=True)
    title         = Column(String(200), nullable=True)
    qr_token      = Column(String(200), unique=True, nullable=True)
    location      = Column(String(200), nullable=True)
    branch        = Column(String(150), nullable=True)
    section       = Column(String(20),  nullable=True)
    sub_section   = Column(String(20),  nullable=True)       # e.g. "A1","A2" for lab sessions
    semester      = Column(String(20),  nullable=True)
    course_type   = Column(String(100), nullable=True)
    gps_lat       = Column(String(50),  nullable=True)
    gps_lng       = Column(String(50),  nullable=True)
    status        = Column(Enum(SessionStatus), default=SessionStatus.active)
    scheduled_at  = Column(DateTime, nullable=False)
    started_at    = Column(DateTime, nullable=True)
    ended_at      = Column(DateTime, nullable=True)
    grace_minutes = Column(Integer, default=15)
    created_at    = Column(DateTime, default=datetime.utcnow)

    course     = relationship("Course", back_populates="sessions")
    faculty    = relationship("User",   back_populates="sessions_taught")
    attendance = relationship("AttendanceRecord", back_populates="session",
                              cascade="all, delete-orphan")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    __table_args__ = (UniqueConstraint("session_id", "student_id", name="uq_session_student"),)
    id          = Column(Integer, primary_key=True, index=True)
    session_id  = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    student_id  = Column(Integer, ForeignKey("users.id"),   nullable=False)
    method      = Column(Enum(AttendanceMethod), default=AttendanceMethod.qr)
    status      = Column(Enum(AttendanceStatus), default=AttendanceStatus.present)
    marked_at   = Column(DateTime, default=datetime.utcnow)
    notes       = Column(String(300), nullable=True)
    student_lat = Column(String(50),  nullable=True)
    student_lng = Column(String(50),  nullable=True)

    session = relationship("Session", back_populates="attendance")
    student = relationship("User", back_populates="attendance_records", foreign_keys=[student_id])


class SystemSettings(Base):
    __tablename__ = "system_settings"
    id            = Column(Integer, primary_key=True, default=1)
    gps_range     = Column(Integer, default=50)
    face_required = Column(Boolean, default=True)
    qr_expiry     = Column(Integer, default=45)
    inst_name     = Column(String(200), default="Teerthanker Mahaveer University")
    manual_edit_window = Column(Integer, default=10)  # minutes after session end
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
