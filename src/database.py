"""Database configuration for AutoApply.

Supports both JSON file storage (local) and PostgreSQL (Railway).
"""

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, JSON, Enum as SQLEnum
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import QueuePool
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import json
import enum

from .config import settings

Base = declarative_base()
engine = None
SessionLocal = None


class ApplicationStatusEnum(enum.Enum):
    PENDING = "pending"
    JD_FETCHED = "jd_fetched"
    RESUME_GENERATED = "resume_generated"
    PDF_GENERATED = "pdf_generated"
    FORM_OPENED = "form_opened"
    FORM_FILLED = "form_filled"
    SUBMITTED = "submitted"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"


class ApplicationDB(Base):
    """SQLAlchemy model for applications stored in PostgreSQL."""
    __tablename__ = "autoapply_applications"

    id = Column(String(100), primary_key=True)
    job_url = Column(String(2048), nullable=False)
    status = Column(String(50), default="pending")
    company_name = Column(String(255))
    job_title = Column(String(255))
    location = Column(String(255))
    job_description_json = Column(JSON)
    tailored_resume_json = Column(JSON)
    cover_letter_json = Column(JSON)
    job_viability_json = Column(JSON)
    output_dir = Column(String(512))
    resume_filename = Column(String(255))
    cover_letter_filename = Column(String(255))
    error_message = Column(Text)
    notes = Column(Text)
    follow_up_date = Column(String(50))
    next_steps = Column(JSON)
    email_sent = Column(Boolean, default=False)
    applied_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_id = Column(Integer)  # Link to jobportal user


class UploadedDocumentDB(Base):
    """SQLAlchemy model for uploaded resumes/cover letters."""
    __tablename__ = "autoapply_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    doc_type = Column(String(50), nullable=False)  # 'resume' or 'cover_letter'
    text_content = Column(Text)
    is_default = Column(Boolean, default=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer)  # Link to jobportal user


class SettingsDB(Base):
    """SQLAlchemy model for settings."""
    __tablename__ = "autoapply_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text)
    user_id = Column(Integer)


def init_database():
    """Initialize database connection if PostgreSQL is configured."""
    global engine, SessionLocal

    if not settings.use_database:
        return False

    try:
        db_url = settings.database_url
        # Handle Railway internal connections
        if 'railway.internal' in db_url and '?' not in db_url:
            db_url = f"{db_url}?sslmode=disable"
        elif 'railway.internal' in db_url and 'sslmode' not in db_url:
            db_url = f"{db_url}&sslmode=disable"

        engine = create_engine(
            db_url,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        # Create tables if they don't exist
        Base.metadata.create_all(bind=engine)
        print(f"Database connected: {db_url.split('@')[-1] if '@' in db_url else 'local'}")
        return True

    except Exception as e:
        print(f"Database initialization failed: {e}")
        return False


@contextmanager
def get_db_session():
    """Get a database session context manager."""
    if SessionLocal is None:
        raise RuntimeError("Database not initialized")

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Lazy initialization - don't connect on import
_db_initialized = False
_db_available = False


def is_database_available():
    """Check if database is available. Initializes on first call."""
    global _db_initialized, _db_available
    if not _db_initialized:
        _db_initialized = True
        _db_available = init_database()
    return _db_available and engine is not None
