"""Smart Attendance System — FastAPI Backend"""
import sys, os, logging, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("smart_attendance")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

app = FastAPI(title="Smart Attendance System API", version="3.0.0")

# ── Security Headers Middleware ──────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Prevent MIME type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # XSS protection
        response.headers["X-XSS-Protection"] = "1; mode=block"
        # HSTS (only for HTTPS)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Remove server header
        # BUG FIX (critical): MutableHeaders in the Starlette version this
        # app installs (pulled in by fastapi==0.115.5) has no .pop() method
        # at all — this line raised AttributeError on every single request,
        # which Starlette then turned into a 500. del is idempotent (a
        # no-op if the header isn't present), so this is safe either way.
        del response.headers["server"]
        return response


# ── Global exception handler — ensures CORS headers on ALL 500 errors ────────
# BUG FIX: this used to put str(exc) straight into the response body, which
# leaks internals to any client — DB connection strings, table/column names,
# file paths, third-party library errors, etc. The full traceback is still
# logged server-side (visible in Render/Supabase logs) for debugging; the
# client only ever gets a generic message.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s:\n%s", request.method, request.url.path,
                 "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    origin = request.headers.get("origin", "*")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again or contact support if it persists."},
        headers={
            "Access-Control-Allow-Origin":      origin,
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Allow-Methods":     "*",
            "Access-Control-Allow-Headers":     "*",
        },
    )

# ── CORS must be added FIRST so it wraps everything ──────────────────────────
import os

# Get allowed origins from environment or use defaults
FRONTEND_URL  = os.getenv("FRONTEND_URL",  "https://sarthak6157-smartattendance-fronten.vercel.app")
FRONTEND_URL2 = os.getenv("FRONTEND_URL2", "https://smartattendance-frontend.vercel.app")

ALLOWED_ORIGINS = [
    FRONTEND_URL,
    FRONTEND_URL2,
    "http://localhost:3000",
    "http://localhost:8000",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
]
# Also add any extra origins from env (comma separated)
EXTRA = os.getenv("EXTRA_ORIGINS", "")
if EXTRA:
    ALLOWED_ORIGINS += [o.strip() for o in EXTRA.split(",") if o.strip()]

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=r"https://.*\.vercel\.app",  # allow ALL vercel deployments
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
from routers import (auth, users, sessions, attendance,
                     courses, settings as settings_router, timetable, notifications, leave)

app.include_router(auth.router,            prefix="/api/auth",          tags=["Auth"])
app.include_router(users.router,           prefix="/api/users",         tags=["Users"])
app.include_router(sessions.router,        prefix="/api/sessions",      tags=["Sessions"])
app.include_router(attendance.router,      prefix="/api/attendance",    tags=["Attendance"])
app.include_router(courses.router,         prefix="/api/courses",       tags=["Courses"])
app.include_router(settings_router.router, prefix="/api/settings",      tags=["Settings"])
app.include_router(timetable.router,       prefix="/api/timetable",     tags=["Timetable"])
app.include_router(notifications.router,   prefix="/api/notifications", tags=["Notifications"])
app.include_router(leave.router,           prefix="/api/leave",         tags=["Leave"])


# ── Startup — create tables + seed ────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    print("=== STARTUP ===")
    db_url = os.getenv("DATABASE_URL", "")
    # BUG FIX: this never actually checked for a sqlite:// URL — any
    # DATABASE_URL that didn't mention "supabase" or "neon" fell through
    # to "Using PostgreSQL database", including the sqlite fallback path,
    # which made every local/dev/test run print a flatly wrong DB type.
    if not db_url:
        print("⚠️  DATABASE_URL not set — using SQLite fallback")
    elif "sqlite" in db_url:
        print("✅ Using SQLite database:", db_url)
    elif "supabase" in db_url:
        print("✅ Using Supabase database")
    elif "neon" in db_url:
        print("✅ Using Neon database")
    else:
        print("✅ Using PostgreSQL database")

    try:
        from db.database import Base, engine
        Base.metadata.create_all(bind=engine)
        print("✅ Tables created/verified")
    except Exception as e:
        print(f"❌ Table creation failed: {e}")

    try:
        import seed
        seed.main()
        print("✅ Seed complete")
    except Exception as e:
        print(f"⚠️  Seed failed (non-fatal): {e}")

# ── Audit log (removed in-memory version — resets on every Render restart) ────
# Audit logs are not persisted. Use Supabase logs or Render logs instead.
@app.post("/api/audit/log")
async def record_audit(request: Request):
    return {"ok": True}  # No-op — logs visible in Render dashboard

@app.get("/api/audit/log")
async def get_audit():
    return []  # Use Render logs or Supabase logs for audit trail

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    db_url = os.getenv("DATABASE_URL", "sqlite")
    db_type = "supabase" if "supabase" in db_url else "neon" if "neon" in db_url else "sqlite"
    return {"status": "ok", "version": "3.0.0", "db": db_type}

# ── Manifest ──────────────────────────────────────────────────────────────────
@app.get("/manifest.json")
def serve_manifest():
    return JSONResponse(
        content={
            "name": "Smart Attendance — TMU",
            "short_name": "Attendance",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#1a3c6e",
            "theme_color": "#1a3c6e",
        },
        headers={"Content-Type": "application/manifest+json"},
    )

# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "message": "Smart Attendance API is running ✅",
        "docs": "/docs",
        "health": "/api/health",
        "frontend": os.getenv("FRONTEND_URL", "https://smartattendance-frontend.vercel.app"),
    }
