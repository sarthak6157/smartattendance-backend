"""Smart Attendance System — FastAPI Backend"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

app = FastAPI(title="Smart Attendance System API", version="3.0.0")

# ── Global exception handler — ensures CORS headers on ALL 500 errors ────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    origin = request.headers.get("origin", "*")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"},
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
                     courses, settings as settings_router, timetable, notifications)

app.include_router(auth.router,            prefix="/api/auth",          tags=["Auth"])
app.include_router(users.router,           prefix="/api/users",         tags=["Users"])
app.include_router(sessions.router,        prefix="/api/sessions",      tags=["Sessions"])
app.include_router(attendance.router,      prefix="/api/attendance",    tags=["Attendance"])
app.include_router(courses.router,         prefix="/api/courses",       tags=["Courses"])
app.include_router(settings_router.router, prefix="/api/settings",      tags=["Settings"])
app.include_router(timetable.router,       prefix="/api/timetable",     tags=["Timetable"])
app.include_router(notifications.router,   prefix="/api/notifications", tags=["Notifications"])

# ── Startup — create tables + seed ────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    print("=== STARTUP ===")
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("⚠️  DATABASE_URL not set — using SQLite fallback")
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

# ── Audit log (in-memory) ─────────────────────────────────────────────────────
_audit_log = []

@app.post("/api/audit/log")
async def record_audit(entry: dict, request: Request):
    from datetime import datetime as dt
    _audit_log.insert(0, {
        **entry,
        "server_time": dt.utcnow().isoformat(),
        "ip": request.client.host,
    })
    if len(_audit_log) > 500:
        _audit_log.pop()
    return {"ok": True}

@app.get("/api/audit/log")
async def get_audit():
    return _audit_log[:200]

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
