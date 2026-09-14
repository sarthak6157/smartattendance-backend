"""SQLAlchemy ORM models — v3 with timetable, branch/section/face/GPS."""
from datetime import datetime
import enum
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship, synonym
from db.database import Base, DB_SCHEMA

def _fk(table_dot_column: str) -> str:
    """Build a ForeignKey target, schema-qualified only when DB_SCHEMA is
    set (real Postgres/Supabase). BUG FIX: these were all hardcoded as
    "public.xxx" strings, which — like the __table_args__ schema below —
    broke every relationship on the SQLite fallback with
    'no such table: public.courses' etc. On SQLite (DB_SCHEMA is None)
    this now just returns "xxx" unqualified."""
    return f"{DB_SCHEMA}.{table_dot_column}" if DB_SCHEMA else table_dot_column


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


class Student(Base):
    __tablename__  = "students"
    __table_args__ = {"schema": DB_SCHEMA}
    # inst_id (e.g. "TCA023") is the primary key AND the only login credential.
    # `id` here is a SEPARATE, purely cosmetic field ("ST006"-style) — it is
    # NOT used for login, FKs, or identity anywhere in the code. Don't reuse
    # the old User.id-synonym trick here: these two columns hold genuinely
    # different values for students, unlike the users/courses migrations.
    inst_id         = Column(String(50), primary_key=True, unique=True, nullable=False, index=True)
    id              = Column(String(20), unique=True, nullable=True)   # "ST006" style display id — NOT the login credential
    full_name       = Column(String(120), nullable=False)
    email           = Column(String(150), unique=True, nullable=False, index=True)
    status          = Column(Enum(UserStatus), default=UserStatus.pending, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    department      = Column(String(150), nullable=True)
    branch          = Column(String(150), nullable=True)
    section         = Column(String(20),  nullable=True)
    semester        = Column(String(20),  nullable=True)
    course          = Column(String(100), nullable=True)   # e.g. "B.Tech"
    sub_section     = Column(String(20),  nullable=True)   # e.g. "A1", "A2" — lab batch
    face_registered = Column(Boolean, default=False)
    face_embedding  = Column(Text, nullable=True)
    face_image_b64  = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)

    # role is implied by which table a row lives in — not a DB column, just a
    # read-only attribute so existing code checking `.role` everywhere keeps
    # working unchanged.
    role = property(lambda self: UserRole.student)

    attendance_records = relationship("AttendanceRecord", back_populates="student",
                                      foreign_keys="AttendanceRecord.student_id")


class Faculty(Base):
    __tablename__  = "faculty"
    __table_args__ = {"schema": DB_SCHEMA}
    # inst_id (e.g. "TMU003") is the primary key AND login credential.
    # `id` is a SEPARATE plain serial number ("Sno.") — cosmetic only, not
    # used for login, FKs, or identity anywhere.
    inst_id         = Column(String(50), primary_key=True, unique=True, nullable=False, index=True)
    id              = Column(Integer, unique=True, nullable=True)   # plain serial number — NOT the login credential
    full_name       = Column(String(120), nullable=False)
    email           = Column(String(150), unique=True, nullable=False, index=True)
    status          = Column(Enum(UserStatus), default=UserStatus.pending, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    department      = Column(String(150), nullable=True)
    branch          = Column(String(150), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)

    role = property(lambda self: UserRole.faculty)
    # UserOut is shared across all three roles and expects these fields —
    # faculty don't have them as real columns, so stub them out as None
    # rather than let Pydantic's getattr blow up with AttributeError.
    section         = property(lambda self: None)
    sub_section     = property(lambda self: None)
    semester        = property(lambda self: None)
    course          = property(lambda self: None)
    face_registered = property(lambda self: False)
    face_embedding  = property(lambda self: None)

    sessions_taught = relationship("Session", back_populates="faculty")
    timetable_slots = relationship("TimetableSlot", back_populates="faculty")


class Admin(Base):
    __tablename__  = "admins"
    __table_args__ = {"schema": DB_SCHEMA}
    inst_id         = Column(String(50), primary_key=True, unique=True, nullable=False, index=True)
    full_name       = Column(String(120), nullable=False)
    email           = Column(String(150), unique=True, nullable=False, index=True)
    status          = Column(Enum(UserStatus), default=UserStatus.pending, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)

    role = property(lambda self: UserRole.admin)
    id   = property(lambda self: None)   # admins have no separate cosmetic id field — keeps UserOut serialization uniform across all 3 roles
    # Same reasoning as Faculty above — stub out fields UserOut expects that
    # admins don't have as real columns.
    department      = property(lambda self: None)
    branch          = property(lambda self: None)
    section         = property(lambda self: None)
    sub_section     = property(lambda self: None)
    semester        = property(lambda self: None)
    course          = property(lambda self: None)
    face_registered = property(lambda self: False)
    face_embedding  = property(lambda self: None)


# Maps a role to its table — used by login/current-user lookups that need to
# pick the right table at runtime.
ROLE_MODEL = {
    UserRole.student: Student,
    UserRole.faculty: Faculty,
    UserRole.admin:   Admin,
}


class Course(Base):
    __tablename__  = "courses"
    __table_args__ = {"schema": DB_SCHEMA}
    # code (e.g. "EAS211") is now the REAL primary key. `id` is kept as a
    # synonym for the same column so the rest of the codebase (Course.id,
    # course.id, etc.) keeps working unchanged.
    code       = Column(String(20), primary_key=True, unique=True, nullable=False)
    id         = synonym("code")
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
    __tablename__  = "timetable_slots"
    __table_args__ = {"schema": DB_SCHEMA}
    id          = Column(Integer, primary_key=True, index=True)
    course_id   = Column(String(20), ForeignKey(_fk("courses.code")), nullable=False)
    faculty_id  = Column(String(50), ForeignKey(_fk("faculty.inst_id")), nullable=True)   # nullable for free classes (Library, Tinkerer etc.)
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
    faculty = relationship("Faculty", back_populates="timetable_slots")


class Session(Base):
    """A live attendance session — created from a timetable slot."""
    __tablename__  = "sessions"
    __table_args__ = {"schema": DB_SCHEMA}
    id            = Column(Integer, primary_key=True, index=True)
    course_id     = Column(String(20), ForeignKey(_fk("courses.code")), nullable=False)
    faculty_id    = Column(String(50), ForeignKey(_fk("faculty.inst_id")), nullable=False)
    timetable_id  = Column(Integer, ForeignKey(_fk("timetable_slots.id")), nullable=True)
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
    faculty    = relationship("Faculty", back_populates="sessions_taught")
    attendance = relationship("AttendanceRecord", back_populates="session",
                              cascade="all, delete-orphan")


class AttendanceRecord(Base):
    __tablename__  = "attendance_records"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_session_student"),
        {"schema": DB_SCHEMA}
    )
    id          = Column(Integer, primary_key=True, index=True)
    session_id  = Column(Integer, ForeignKey(_fk("sessions.id"), ondelete="CASCADE"), nullable=False)
    student_id  = Column(String(50), ForeignKey(_fk("students.inst_id")), nullable=False)
    method      = Column(Enum(AttendanceMethod), default=AttendanceMethod.qr)
    status      = Column(Enum(AttendanceStatus), default=AttendanceStatus.present)
    marked_at   = Column(DateTime, default=datetime.utcnow)
    notes       = Column(String(300), nullable=True)
    student_lat = Column(String(50),  nullable=True)
    student_lng = Column(String(50),  nullable=True)

    session = relationship("Session", back_populates="attendance")
    student = relationship("Student", back_populates="attendance_records", foreign_keys=[student_id])


class SystemSettings(Base):
    __tablename__  = "system_settings"
    __table_args__ = {"schema": DB_SCHEMA}
    id            = Column(Integer, primary_key=True, default=1)
    gps_range     = Column(Integer, default=50)
    face_required = Column(Boolean, default=True)
    qr_expiry     = Column(Integer, default=45)
    inst_name     = Column(String(200), default="Teerthanker Mahaveer University")
    manual_edit_window = Column(Integer, default=10)  # minutes after session end
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
