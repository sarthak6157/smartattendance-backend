"""Push notification routes — Web Push + Email alerts."""
import os, json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from core.security import get_current_user, require_roles
from db.database import get_db
from models.models import PushSubscriptionRecord, Student, UserRole, Session, SessionStatus, SystemSettings

router = APIRouter()

# ── Pydantic models ───────────────────────────────────────────────────────────
class PushSubscription(BaseModel):
    endpoint:   str
    keys:       dict   # {p256dh, auth}

class PushSubscriptionRecordIn(BaseModel):
    user_id:      str
    subscription: dict

# ── Push subscription endpoints ───────────────────────────────────────────────
# BUG FIX: these used to store subscriptions in a plain in-memory dict
# (_push_subscriptions), wiped on every server restart/redeploy. Now a
# real table (PushSubscriptionRecord, additive-only — no migration risk).
@router.post("/push/subscribe", status_code=201)
def subscribe_push(
    payload: PushSubscription,
    current_user = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    """Save a push subscription for the current user."""
    existing = db.query(PushSubscriptionRecord).filter(
        PushSubscriptionRecord.endpoint == payload.endpoint
    ).first()
    if existing:
        existing.user_id = current_user.inst_id
        existing.keys_json = json.dumps(payload.keys)
    else:
        db.add(PushSubscriptionRecord(
            user_id=current_user.inst_id, endpoint=payload.endpoint,
            keys_json=json.dumps(payload.keys),
        ))
    db.commit()
    return {"message": "Subscribed to push notifications!"}

@router.delete("/push/unsubscribe")
def unsubscribe_push(current_user = Depends(get_current_user), db: DBSession = Depends(get_db)):
    """Remove all push subscriptions for current user."""
    db.query(PushSubscriptionRecord).filter(PushSubscriptionRecord.user_id == current_user.inst_id).delete()
    db.commit()
    return {"message": "Unsubscribed from push notifications."}

# ── Send push notification to a user ─────────────────────────────────────────
def send_push_to_user(db: DBSession, user_id: str, title: str, body: str, url: str = "/"):
    """Send a web push notification to all devices of a user."""
    subs = db.query(PushSubscriptionRecord).filter(PushSubscriptionRecord.user_id == user_id).all()
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
    dead_ids = []
    for sub in subs:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint, "keys": json.loads(sub.keys_json)},
                data=payload,
                vapid_private_key=VAPID_PRIVATE,
                vapid_claims={"sub": f"mailto:{VAPID_EMAIL}"},
            )
            sent += 1
        except Exception:
            dead_ids.append(sub.id)  # expired/invalid subscription — browser unsubscribed it on its end

    if dead_ids:
        db.query(PushSubscriptionRecord).filter(PushSubscriptionRecord.id.in_(dead_ids)).delete(synchronize_session=False)
        db.commit()
    return sent


# ── Notify section when session goes live ─────────────────────────────────────
def notify_students_session_live(db: DBSession, session: Session) -> dict:
    """NEW: the actual notification logic, extracted so it can be called
    automatically from go_live()/create_extra_class() as well as from the
    manual endpoint below. Previously this only existed as something a
    faculty member had to remember to trigger by hand — dead code from
    the students' point of view unless someone clicked an extra button.

    BUG FIX (found on audit): this used to run `if session.branch: filter
    by branch` — for an ad-hoc extra class with no branch set (a valid,
    common case; ExtraClassRequest defaults branch to ""), that condition
    is False, the filter is skipped entirely, and EVERY active student in
    the whole university gets pushed a notification for a class
    completely unrelated to them. An unrestricted session now notifies
    nobody — this applies to the manual /notify/session-live/{id}
    endpoint too, since it calls this same function; broadcasting to
    every student across every college (Medical, Dental, Law,
    Engineering...) for one class going live isn't something either path
    should ever do by accident.

    BUG FIX (found on audit): branch matching was an exact `==` string
    comparison — inconsistent with the flexible "B.Tech CSE" vs "CSE"
    matching already used for the actual attendance-marking permission
    check in attendance.py's mark_full_flow(). An exact-match mismatch
    here meant real students could go un-notified for their own class
    just because their stored branch string was formatted slightly
    differently. Now uses the same matching logic both places."""
    if not session.branch:
        return {"students_count": 0, "pushes_sent": 0, "skipped": "no branch/section restriction on this session"}

    import re as _re
    sb = session.branch.strip().lower()
    sb_core = _re.sub(r'(?i)^(b\.tech|b\.e|m\.tech|bca|mca|mba|b\.sc)[\s\-]+', '', sb).strip()

    q = db.query(Student).filter(Student.status == "active")
    if session.section:
        q = q.filter(Student.section == session.section)
    candidates = q.all()
    students = []
    for stu in candidates:
        ub = (stu.branch or getattr(stu, "department", "") or "").strip().lower()
        if not ub:
            continue
        if ub == sb or sb_core in ub or ub in sb or sb in ub:
            students.append(stu)

    sent_count = 0
    for stu in students:
        sent_count += send_push_to_user(
            db, user_id=stu.inst_id,
            title="Class Started!",
            body=f"{session.title or 'Your class'} is now live. Mark your attendance now!",
            url="/",
        )
    return {"students_count": len(students), "pushes_sent": sent_count}


@router.post("/notify/session-live/{session_id}")
def notify_session_live(
    session_id: int,
    current_user = Depends(require_roles(UserRole.faculty, UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Manual trigger — kept for faculty who want to re-notify or notify
    a session the auto-notify toggle skipped. See notify_students_session_live()
    for the actual logic, and timetable.py's go_live() / sessions.py's
    create_extra_class() for the automatic call."""
    session = db.query(Session).filter(Session.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    if session.status != SessionStatus.active:
        raise HTTPException(status_code=400, detail="Session is not active.")

    result = notify_students_session_live(db, session)
    return {
        "message": f"Notified {result['students_count']} students, {result['pushes_sent']} push notifications sent.",
        **result,
    }


# ── Email notifications ───────────────────────────────────────────────────────
class EmailPayload(BaseModel):
    to:      str
    subject: str
    body:    str

@router.post("/email/send")
def send_email(
    payload: EmailPayload,
    _ = Depends(require_roles(UserRole.admin)),
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
    _ = Depends(require_roles(UserRole.admin)),
    db: DBSession = Depends(get_db),
):
    """Send email to all students below attendance threshold."""
    GMAIL_USER = os.getenv("GMAIL_USER", "")
    GMAIL_PASS = os.getenv("GMAIL_PASSWORD", "")
    if not GMAIL_USER or not GMAIL_PASS:
        raise HTTPException(status_code=503, detail="Email not configured.")

    q = db.query(Student).filter(Student.status == "active")
    if branch:  q = q.filter(Student.branch  == branch)
    if section: q = q.filter(Student.section == section)
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
            .filter(AttendanceRecord.student_id == stu.inst_id).all()
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
