"""Push notification routes — Web Push + Email alerts."""
import os, json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import User, UserRole, Session, SessionStatus

router = APIRouter()

# ── Pydantic models ───────────────────────────────────────────────────────────
class PushSubscription(BaseModel):
    endpoint:   str
    keys:       dict   # {p256dh, auth}

class PushSubscriptionRecord(BaseModel):
    user_id:      int
    subscription: dict

# In-memory store for push subscriptions (replace with DB table in production)
_push_subscriptions: dict[int, list[dict]] = {}

# ── Push subscription endpoints ───────────────────────────────────────────────
@router.post("/push/subscribe", status_code=201)
def subscribe_push(
    payload: PushSubscription,
    current_user: User = Depends(get_current_user),
):
    """Save a push subscription for the current user."""
    uid = current_user.id
    if uid not in _push_subscriptions:
        _push_subscriptions[uid] = []
    sub_dict = {"endpoint": payload.endpoint, "keys": payload.keys}
    # Avoid duplicate subscriptions
    if sub_dict not in _push_subscriptions[uid]:
        _push_subscriptions[uid].append(sub_dict)
    return {"message": "Subscribed to push notifications!"}

@router.delete("/push/unsubscribe")
def unsubscribe_push(current_user: User = Depends(get_current_user)):
    """Remove all push subscriptions for current user."""
    _push_subscriptions.pop(current_user.id, None)
    return {"message": "Unsubscribed from push notifications."}

# ── Send push notification to a user ─────────────────────────────────────────
def send_push_to_user(user_id: int, title: str, body: str, url: str = "/"):
    """Send a web push notification to all devices of a user."""
    subs = _push_subscriptions.get(user_id, [])
    if not subs:
        return 0

    VAPID_PRIVATE = os.getenv("VAPID_PRIVATE_KEY", "")
    VAPID_EMAIL   = os.getenv("VAPID_EMAIL", "admin@tmu.ac.in")

    if not VAPID_PRIVATE:
        return 0  # silently skip if not configured

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    failed = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": f"mailto:{VAPID_EMAIL}"},
            )
            sent += 1
        except Exception:
            failed.append(sub)

    # Remove failed subscriptions
    if failed:
        _push_subscriptions[user_id] = [s for s in subs if s not in failed]
    return sent


# ── Notify section when session goes live ─────────────────────────────────────
@router.post("/notify/session-live/{session_id}")
def notify_session_live(
    session_id: int,
    current_user: User = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Notify all students in a section that a session has gone live."""
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session is not active.")

    # Get students in this section
    q = db.query(User).filter(User.role == UserRole.student, User.status == "active")
    if session.branch:  q = q.filter(User.branch  == session.branch)
    if session.section: q = q.filter(User.section == session.section)
    students = q.all()

    sent_count = 0
    for stu in students:
        sent = send_push_to_user(
            user_id=stu.id,
            title="Class Started!",
            body=f"{session.title} is now live. Mark your attendance now!",
            url="/",
        )
        sent_count += sent

    return {
        "message":         f"Notified {len(students)} students, {sent_count} push notifications sent.",
        "students_count":  len(students),
        "pushes_sent":     sent_count,
    }


# ── Email notifications ───────────────────────────────────────────────────────
class EmailPayload(BaseModel):
    to:      str
    subject: str
    body:    str

@router.post("/email/send")
def send_email(
    payload: EmailPayload,
    _: User = Depends(require_roles(UserRole.admin)),
):
    """Send an email notification (admin only)."""
    GMAIL_USER = os.getenv("GMAIL_USER", "")
    GMAIL_PASS = os.getenv("GMAIL_PASSWORD", "")
    if not GMAIL_USER or not GMAIL_PASS:
        raise HTTPException(status_code=503, detail="Email not configured. Set GMAIL_USER and GMAIL_PASSWORD in environment variables.")
    try:
        import yagmail
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_PASS)
        yag.send(to=payload.to, subject=payload.subject, contents=payload.body)
        return {"message": f"Email sent to {payload.to}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Email failed: {str(e)}")


@router.post("/email/low-attendance")
def notify_low_attendance(
    branch:    Optional[str] = None,
    section:   Optional[str] = None,
    threshold: int = 75,
    _: User = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Send email to all students below attendance threshold."""
    GMAIL_USER = os.getenv("GMAIL_USER", "")
    GMAIL_PASS = os.getenv("GMAIL_PASSWORD", "")
    if not GMAIL_USER or not GMAIL_PASS:
        raise HTTPException(status_code=503, detail="Email not configured.")

    q = db.query(User).filter(User.role == UserRole.student, User.status == "active")
    if branch:  q = q.filter(User.branch  == branch)
    if section: q = q.filter(User.section == section)
    students = q.all()

    from models.models import AttendanceRecord
    total_sessions = db.query(Session).filter(
        Session.status  == "closed",
        Session.branch  == branch,
        Session.section == section,
    ).count() if branch and section else 0

    try:
        import yagmail
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_PASS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    sent, skipped = 0, 0
    for stu in students:
        if not stu.email:
            skipped += 1
            continue
        records = db.query(AttendanceRecord)\
            .filter(AttendanceRecord.student_id == stu.id).all()
        present = sum(1 for r in records if r.status.value == "present")
        pct = round((present / total_sessions) * 100) if total_sessions else 0
        if pct < threshold:
            needed = 0
            x = 0
            while total_sessions + x > 0 and (present + x) / (total_sessions + x) < 0.75:
                x += 1
                if x > 200: break
            needed = x
            subject = f"Attendance Warning - {pct}% | TMU Smart Attendance"
            body = f"""
Dear {stu.full_name},

This is an automated attendance alert from Teerthanker Mahaveer University.

Your current attendance is {pct}% ({present}/{total_sessions} classes).
Minimum required attendance is 75%.

You need to attend {needed} more consecutive classes to reach 75%.

Please ensure regular attendance to avoid any academic consequences.

Regards,
TMU Smart Attendance System
"""
            try:
                yag.send(to=stu.email, subject=subject, contents=body)
                sent += 1
            except Exception:
                skipped += 1

    return {
        "message": f"Sent {sent} email warnings, {skipped} skipped.",
        "sent":    sent,
        "skipped": skipped,
    }


# ── VAPID key helper ──────────────────────────────────────────────────────────
@router.get("/push/vapid-public-key")
def get_vapid_public_key():
    """Return the VAPID public key for frontend push subscription."""
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    if not key:
        return {"vapid_public_key": None, "enabled": False}
    return {"vapid_public_key": key, "enabled": True}
