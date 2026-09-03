"""Database engine — Supabase PostgreSQL compatible."""
import os
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

def get_database_url():
    url = os.getenv("DATABASE_URL", "sqlite:///./attendance.db")
    # Fix old-style postgres:// URLs
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # Remove channel_binding — not supported by psycopg2
    if "channel_binding" in url:
        import re
        url = re.sub(r'[&?]channel_binding=[^&]*', '', url)
        url = re.sub(r'[&?]$', '', url)
    return url

DATABASE_URL = get_database_url()

def create_db_engine():
    url = get_database_url()
    if "sqlite" in url:
        return create_engine(url, connect_args={"check_same_thread": False})
    else:
        # Supabase / PostgreSQL
        # Remove sslmode from URL if present (we set it via connect_args)
        # Supabase pooler (port 6543) needs sslmode=require but NOT in connect_args
        # Supabase direct (port 5432) needs sslmode=require in connect_args
        if "sslmode" in url:
            # Already in URL — don't add to connect_args
            return create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=280,
                pool_size=3,
                max_overflow=5,
            )
        else:
            return create_engine(
                url,
                pool_pre_ping=True,
                pool_recycle=280,
                pool_size=3,
                max_overflow=5,
                connect_args={"sslmode": "require", "connect_timeout": 10},
            )

engine = create_db_engine()

# Set search_path to public schema — prevents collision with Supabase's auth.users table
from sqlalchemy import event, MetaData
@event.listens_for(engine, "connect")
def set_search_path(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET search_path TO public")
        cursor.close()
    except Exception:
        pass

# Use MetaData with schema="public" so SQLAlchemy resolves ForeignKeys
# correctly at import time — fixes "could not find table sessions" error
metadata = MetaData(schema="public")
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base(metadata=metadata)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
