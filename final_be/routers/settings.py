"""System settings routes."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import SystemSettings, User, UserRole
from schemas.schemas import SettingsOut, SettingsUpdate

router = APIRouter()
AdminOnly = require_roles(UserRole.admin)


def _get_or_create(db: DBSession) -> SystemSettings:
    s = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not s:
        s = SystemSettings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


@router.get("", response_model=SettingsOut)
def get_settings(_: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    return _get_or_create(db)


@router.patch("", response_model=SettingsOut)
def update_settings(payload: SettingsUpdate, _: User = Depends(AdminOnly), db: DBSession = Depends(get_db)):
    s = _get_or_create(db)
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    db.commit()
    db.refresh(s)
    return s
