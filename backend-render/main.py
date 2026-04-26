"""Smart Attendance System — FastAPI Backend v3"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from db.database import Base, engine
from routers import auth, users, sessions, attendance, courses, settings as settings_router, timetable

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Smart Attendance System API", version="3.0.0")

@app.on_event("startup")
async def startup_event():
    print("Starting up — seeding database...")
    try:
        import seed; seed.main()
        print("Seed complete.")
    except Exception as e:
        print(f"Seed failed (non-fatal): {e}")

# Add your Vercel frontend URL here after deploying
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
ALLOWED_ORIGINS = [
    FRONTEND_URL,
    "https://attendance-system-tbon.onrender.com",
    "https://*.vercel.app",
    "http://localhost:8000",
    "http://localhost:3000",
    "http://127.0.0.1:8000",
    "*",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class PermissiveCSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Allow loading face-api models from external CDNs
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

app.include_router(auth.router,             prefix="/api/auth",       tags=["Auth"])
app.include_router(users.router,            prefix="/api/users",      tags=["Users"])
app.include_router(sessions.router,         prefix="/api/sessions",   tags=["Sessions"])
app.include_router(attendance.router,       prefix="/api/attendance", tags=["Attendance"])
app.include_router(courses.router,          prefix="/api/courses",    tags=["Courses"])
app.include_router(settings_router.router,  prefix="/api/settings",   tags=["Settings"])
app.include_router(timetable.router,        prefix="/api/timetable",  tags=["Timetable"])

# ── In-memory audit log ──────────────────────────────────────────────────────
_audit_log = []

@app.post("/api/audit/log")
async def record_audit(entry: dict, request: Request):
    from datetime import datetime as dt
    _audit_log.insert(0, {**entry, "server_time": dt.utcnow().isoformat(), "ip": request.client.host})
    if len(_audit_log) > 500: _audit_log.pop()
    return {"ok": True}

@app.get("/api/audit/log")
async def get_audit():
    return _audit_log[:200]


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "3.0.0"}

@app.get("/manifest.json")
def serve_manifest():
    import json
    from fastapi.responses import JSONResponse
    manifest = {
        "name": "Smart Attendance — TMU",
        "short_name": "Attendance",
        "description": "Smart Attendance System for Teerthanker Mahaveer University",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1a3c6e",
        "theme_color": "#1a3c6e",
        "orientation": "portrait-primary",
        "icons": [{"src": "/favicon.ico", "sizes": "any", "type": "image/x-icon"}],
        "categories": ["education", "productivity"]
    }
    return JSONResponse(content=manifest, headers={"Content-Type": "application/manifest+json"})

@app.get("/", response_class=HTMLResponse)
def root():
    return HTMLResponse("""
    <h2>Smart Attendance API</h2>
    <p>API is running. Frontend is hosted on Vercel.</p>
    <p><a href='/docs'>View API Docs →</a></p>
    """)
