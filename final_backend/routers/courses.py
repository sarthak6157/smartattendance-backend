"""Courses routes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import Course, User, UserRole
from schemas.schemas import CourseCreate, CourseOut

router = APIRouter()
AdminOnly = require_roles(UserRole.admin)


@router.get("", response_model=list[CourseOut])
def list_courses(_: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    return db.query(Course).order_by(Course.name).all()


@router.post("", response_model=CourseOut, status_code=201)
def create_course(payload: CourseCreate, _: User = Depends(AdminOnly), db: DBSession = Depends(get_db)):
    if db.query(Course).filter(Course.code == payload.code.upper()).first():
        raise HTTPException(status_code=400, detail="Course code already exists.")
    course = Course(code=payload.code.upper(), name=payload.name, department=payload.department, credits=payload.credits)
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}", status_code=204)
def delete_course(course_id: int, _: User = Depends(AdminOnly), db: DBSession = Depends(get_db)):
    course = db.query(Course).filter(Course.id == course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found.")
    db.delete(course)
    db.commit()
