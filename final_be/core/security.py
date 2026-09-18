"""Security: JWT, password hashing, role guards."""
import os, warnings
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from db.database import get_db
from models.models import UserRole, ROLE_MODEL

_FALLBACK_SECRET = "SmartAttendance2025TMU@SecretKey#Sarthak"
SECRET_KEY = os.getenv("SECRET_KEY", _FALLBACK_SECRET)
if SECRET_KEY == _FALLBACK_SECRET:
    # BUG FIX: previously this fallback was silent. Anyone who reads this
    # (public) source code now knows the exact key used to sign every JWT
    # on any deployment that forgot to set SECRET_KEY — they could forge
    # valid admin tokens. Keeping the fallback so existing deployments
    # don't break on upgrade, but making it impossible to miss in logs.
    warnings.warn(
        "SECURITY WARNING: SECRET_KEY env var is not set — using the "
        "hardcoded fallback key from source. Anyone who has read this "
        "code can forge login tokens for ANY account, including admin. "
        "Set a random SECRET_KEY in your deployment environment ASAP "
        "(e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`).",
        stacklevel=1,
    )
    print("=" * 70)
    print("⚠️  SECURITY WARNING: SECRET_KEY not set — using an insecure")
    print("   default that is visible in the source code. Set SECRET_KEY")
    print("   in your environment before going to production.")
    print("=" * 70)
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

pwd_context   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def hash_password(plain: str) -> str:
    return pwd_context.hash(str(plain)[:72])

def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return pwd_context.verify(str(plain)[:72], hashed)
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    # Store sub as string to avoid JWT type issues
    if "sub" in to_encode:
        to_encode["sub"] = str(to_encode["sub"])
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        role_str = payload.get("role")
        if sub is None or role_str is None:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
        user_id = sub  # inst_id — the login credential, a string
        role = UserRole(role_str)
    except (JWTError, ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    # Users now live in one of three separate tables (students/faculty/admins)
    # — the JWT's role claim tells us which one to look in.
    model = ROLE_MODEL.get(role)
    if model is None:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    user = db.query(model).filter(model.inst_id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def require_roles(*roles):
    def _check(current_user = Depends(get_current_user)):
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="Permission denied")
        return current_user
    return _check


# ── Rotating QR token ─────────────────────────────────────────────────────
# NEW FEATURE (fixes a real gap): the QR code used to be a single static
# token for the whole class period — one screenshot forwarded to a group
# chat let the entire class "attend" from their hostel rooms. This makes
# the code embedded in the projected QR change every `rotate_seconds`
# (SystemSettings.qr_expiry, already existed as a dead/unused field —
# repurposed here rather than adding a new column) without needing any
# background job or extra DB writes: the token is deterministically
# derived from (a per-session secret seed, the session id, and the
# current time bucket), so any request — faculty asking "what's the
# current code" or a student submitting one — can verify it independently
# just by recomputing the same HMAC.
import hmac, hashlib, time

def rotating_qr_token(seed: str, session_id: int, bucket: int) -> str:
    msg = f"{session_id}:{bucket}".encode()
    key = f"{SECRET_KEY}:{seed}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:12]

def current_qr_bucket(rotate_seconds: int) -> int:
    return int(time.time() // max(rotate_seconds, 5))

def build_qr_payload(seed: str, session_id: int, rotate_seconds: int) -> str:
    bucket = current_qr_bucket(rotate_seconds)
    return f"{session_id}:{rotating_qr_token(seed, session_id, bucket)}"

def verify_qr_payload(seed: str, payload: str, rotate_seconds: int) -> Optional[int]:
    """Returns the session_id if payload is a currently-valid rotating
    token for that session (current bucket or the one before it, so a
    request that lands right on a rotation boundary or after a couple of
    seconds of network lag still succeeds), else None."""
    try:
        sid_str, token = payload.split(":", 1)
        session_id = int(sid_str)
    except (ValueError, AttributeError):
        return None
    bucket = current_qr_bucket(rotate_seconds)
    valid = {rotating_qr_token(seed, session_id, bucket),
             rotating_qr_token(seed, session_id, bucket - 1)}
    return session_id if token in valid else None


# ── Server-side face verification ────────────────────────────────────────
# BUG FIX / NEW FEATURE: face matching used to happen ENTIRELY in the
# browser (face-api.js decides "match", then just tells the server it
# matched) — the server never actually checked. Anyone with devtools open
# could call the attendance endpoint directly with no camera involved at
# all. This compares the descriptor the browser captured against the
# student's registered embedding using a standard Euclidean distance,
# same metric face-api.js itself uses for its own client-side threshold.
import json

FACE_MATCH_THRESHOLD = 0.5  # face-api.js's own recommended cutoff

def face_descriptor_matches(stored_embedding_json: Optional[str], submitted: Optional[list]) -> tuple[bool, str]:
    """Returns (matched, reason). `matched` is False (never throws) if
    either side is missing/malformed, so callers can decide what a
    missing registration means for their flow rather than getting a 500."""
    if not stored_embedding_json:
        return False, "No face registered for this account."
    if not submitted or len(submitted) != 128:
        return False, "No valid face capture received."
    try:
        stored = json.loads(stored_embedding_json)
    except (ValueError, TypeError):
        return False, "Stored face data is corrupted — please re-register your face."
    if not isinstance(stored, list) or len(stored) != 128:
        return False, "Stored face data is invalid — please re-register your face."
    try:
        dist = math_sqrt_sum_sq_diff(stored, submitted)
    except (TypeError, ValueError):
        return False, "Face capture data was invalid."
    if dist > FACE_MATCH_THRESHOLD:
        return False, f"Face did not match your registered face (distance {dist:.2f})."
    return True, "ok"

def math_sqrt_sum_sq_diff(a: list, b: list) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
