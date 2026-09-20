"""
seed.py - Only creates admin account and default settings on first startup.
Real data (faculty, students, courses) is managed through the admin panel.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db.database import SessionLocal
from models.models import Admin, UserStatus, SystemSettings
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
                auto_notify_on_go_live=True,
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
    # BUG FIX: this used to always seed the same hardcoded admin1/Pass@123
    # on every fresh deployment, with no visibility beyond a startup log
    # line — anyone who has read this (public) source now knows the
    # default admin login for any TMU Smart Attendance instance that
    # hasn't been changed yet. Now configurable via env vars so a new
    # deployment can set its own admin credentials from day one; falls
    # back to the old default for backward compatibility with instances
    # that already seeded admin1, but prints a loud warning either way.
    admin_inst_id = os.getenv("ADMIN_INST_ID", "admin1")
    admin_email   = os.getenv("ADMIN_EMAIL", "admin@smartattendance.com")
    admin_password= os.getenv("ADMIN_PASSWORD", "Pass@123")
    try:
        existing = db.query(Admin).filter(Admin.inst_id == admin_inst_id).first()
        if not existing:
            db.add(Admin(
                full_name="System Admin",
                inst_id=admin_inst_id,
                email=admin_email,
                status=UserStatus.active,
                hashed_password=hash_password(admin_password),
            ))
            db.commit()
            print(f"Admin created → {admin_inst_id} / (password set from ADMIN_PASSWORD env, or default if unset)")
            if not os.getenv("ADMIN_PASSWORD"):
                print("=" * 70)
                print("⚠️  SECURITY WARNING: using the default seed password. Set")
                print("   ADMIN_INST_ID / ADMIN_EMAIL / ADMIN_PASSWORD env vars and log")
                print("   in to change it, or change it immediately via the admin panel.")
                print("=" * 70)
        else:
            print("Admin already exists.")
    except Exception as e:
        print(f"Admin seed error: {e}")
        db.rollback()

    db.close()
    print("========== SEED COMPLETE ==========\n")


if __name__ == "__main__":
    main()
