"""Smart Attendance System — FastAPI Backend"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

app = FastAPI(title="Smart Attendance System API", version="3.0.0")

# ── CORS ─────────────────────────────────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # open during development — restrict later
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── CSP Middleware ────────────────────────────────────────────────────────────
class PermissiveCSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
            "script-src * 'unsafe-inline' 'unsafe-eval' blob:; "
            "connect-src * data: blob:; "
            "img-src * data: blob:; "
            "font-src * data:; "
            "style-src * 'unsafe-inline';"
        )
        return response

app.add_middleware(PermissiveCSPMiddleware)

# ── Routers ───────────────────────────────────────────────────────────────────
from routers import (auth, users, sessions, attendance,
                     courses, settings as settings_router, timetable)

app.include_router(auth.router,            prefix="/api/auth",       tags=["Auth"])
app.include_router(users.router,           prefix="/api/users",      tags=["Users"])
app.include_router(sessions.router,        prefix="/api/sessions",   tags=["Sessions"])
app.include_router(attendance.router,      prefix="/api/attendance", tags=["Attendance"])
app.include_router(courses.router,         prefix="/api/courses",    tags=["Courses"])
app.include_router(settings_router.router, prefix="/api/settings",   tags=["Settings"])
app.include_router(timetable.router,       prefix="/api/timetable",  tags=["Timetable"])

# ── Startup — create tables + seed ────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    print("=== STARTUP ===")
    print(f"DATABASE_URL set: {'yes' if os.getenv('DATABASE_URL') else 'NO — using SQLite'}")
    try:
        from db.database import Base, engine
        Base.metadata.create_all(bind=engine)
        print("Tables created ✅")
    except Exception as e:
        print(f"Table creation failed: {e}")

    try:
        import seed
        seed.main()
        print("Seed complete ✅")
    except Exception as e:
        print(f"Seed failed (non-fatal): {e}")

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

# ── Health check ──────────────────────────────────────────────────────────────
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
    return {"message": "Smart Attendance API is running", "docs": "/docs", "health": "/api/health"}
