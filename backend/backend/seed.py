"""
seed.py - Only creates admin account and default settings on first startup.
Real data (faculty, students, courses) is managed through the admin panel.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import SessionLocal
from models.models import User, UserRole, UserStatus, SystemSettings
from core.security import hash_password


def main():
    db = SessionLocal()
    print("\n========== SEED STARTING ==========")

    # ── 1. System Settings (only if not exists) ──
    try:
        s = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
        if not s:
            db.add(SystemSettings(
                id=1,
                gps_range=50,
                face_required=True,
                qr_expiry=45,
                manual_edit_window=10,
                inst_name="Teerthanker Mahaveer University"
            ))
            db.commit()
            print("Default settings created.")
        else:
            print("Settings already exist.")
    except Exception as e:
        print(f"Settings seed error: {e}")
        db.rollback()

    # ── 2. Admin account (only if not exists) ──
    try:
        existing = db.query(User).filter(User.inst_id == "admin1").first()
        if not existing:
            db.add(User(
                full_name="System Admin",
                inst_id="admin1",
                email="admin@smartattendance.com",
                role=UserRole.admin,
                status=UserStatus.active,
                hashed_password=hash_password("Pass@123"),
                department="Administration",
            ))
            db.commit()
            print("Admin created → admin1 / Pass@123")
        else:
            print("Admin already exists.")
    except Exception as e:
        print(f"Admin seed error: {e}")
        db.rollback()

    db.close()
    print("========== SEED COMPLETE ==========\n")


if __name__ == "__main__":
    main()
