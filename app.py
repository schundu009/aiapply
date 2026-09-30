#!/usr/bin/env python3
"""AutoApply - Enterprise Job Application Platform

Enterprise-grade features:
- Structured logging with request tracing
- Rate limiting for API endpoints
- Standardized API responses
- Health checks with dependency status
- Input validation and sanitization
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, flash, Response, g
from werkzeug.utils import secure_filename
import threading
import queue
import time
from pypdf import PdfReader
from docx import Document

from src.auth import install_auth
from src.browser_automation import BrowserAutomation
from src.config import settings
from src.profile_store import (
    load_profile as _load_profile,
    save_profile as _save_profile,
    scrub_profile_files,
    strip_credentials,
)
try:
    from src.database import is_database_available, get_db_session, ApplicationDB, UploadedDocumentDB, SettingsDB
except Exception as e:
    print(f"Warning: Database module failed to load: {e}")
    def is_database_available(): return False
    def get_db_session(): raise RuntimeError("Database not available")
    ApplicationDB = None
    UploadedDocumentDB = None
    SettingsDB = None
from src.email_notifier import email_notifier
from src.job_analyzer import JobAnalyzer
from src.job_fetcher import JobFetcher
from src.models import Application, ApplicationStatus
from src.notion_client import parse_resume_from_text
from src.pdf_generator import PDFGenerator
from src.resume_generator import ResumeGenerator

# ============================================
# Logging Configuration
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('autoapply')

# ============================================
# Flask App Configuration
# ============================================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))
app.config['UPLOAD_FOLDER'] = Path('uploads')
app.config['UPLOAD_FOLDER'].mkdir(exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max

# Every route (pages, API, SSE, static files, downloads) requires a Cariara
# admin token; only /healthz and the login page are public. Registered first so
# it runs before any other before_request hook.
install_auth(app)

# ============================================
# Rate Limiting (Simple in-memory implementation)
# ============================================
rate_limit_store = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 30  # max requests per window


def check_rate_limit(identifier: str) -> bool:
    """Check if request should be rate limited. Returns True if allowed."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW

    # Clean old entries
    rate_limit_store[identifier] = [t for t in rate_limit_store[identifier] if t > window_start]

    if len(rate_limit_store[identifier]) >= RATE_LIMIT_MAX_REQUESTS:
        return False

    rate_limit_store[identifier].append(now)
    return True


# ============================================
# API Response Helpers
# ============================================
def api_success(data=None, message=None, status_code=200):
    """Standardized success response."""
    response = {'success': True}
    if data is not None:
        response['data'] = data
    if message:
        response['message'] = message
    return jsonify(response), status_code


def api_error(error, status_code=400, details=None):
    """Standardized error response."""
    response = {'success': False, 'error': str(error)}
    if details:
        response['details'] = details
    return jsonify(response), status_code


# ============================================
# Input Validation Helpers
# ============================================
def validate_url(url: str) -> tuple[bool, str]:
    """Validate a job URL. Returns (is_valid, error_message)."""
    if not url:
        return False, "URL is required"

    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False, "Invalid URL format"
        if parsed.scheme not in ('http', 'https'):
            return False, "URL must use HTTP or HTTPS"
        return True, ""
    except Exception as e:
        return False, f"URL parsing error: {e}"


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage."""
    # Remove path separators and null bytes
    filename = re.sub(r'[/\\:\x00]', '_', filename)
    # Limit length
    if len(filename) > 200:
        name, ext = os.path.splitext(filename)
        filename = name[:195] + ext
    return secure_filename(filename)


# ============================================
# Health Check Endpoint (Enhanced)
# ============================================
@app.route('/health')
def health_check():
    """Enhanced health check with dependency status."""
    health = {
        'status': 'healthy',
        'service': 'autoapply',
        'timestamp': datetime.utcnow().isoformat(),
        'dependencies': {}
    }

    # Check database
    try:
        db_available = is_database_available()
        health['dependencies']['database'] = 'connected' if db_available else 'disconnected'
    except Exception as e:
        health['dependencies']['database'] = f'error: {str(e)}'

    # Check uploads directory
    try:
        uploads_dir = app.config['UPLOAD_FOLDER']
        health['dependencies']['uploads'] = 'accessible' if uploads_dir.exists() else 'missing'
    except Exception as e:
        health['dependencies']['uploads'] = f'error: {str(e)}'

    # Check output directory
    try:
        output_dir = Path('output')
        health['dependencies']['output'] = 'accessible' if output_dir.exists() else 'will_create'
    except Exception as e:
        health['dependencies']['output'] = f'error: {str(e)}'

    # Determine overall status
    critical_deps = ['database']
    for dep in critical_deps:
        if 'error' in health['dependencies'].get(dep, ''):
            health['status'] = 'degraded'

    status_code = 200 if health['status'] == 'healthy' else 503
    return jsonify(health), status_code


@app.route('/health/ready')
def readiness_check():
    """Kubernetes-style readiness probe."""
    # Check if we can accept traffic
    try:
        # Verify critical components are available
        if not app.config['UPLOAD_FOLDER'].exists():
            return api_error("Uploads directory not available", 503)
        return api_success({'ready': True})
    except Exception as e:
        return api_error(f"Not ready: {e}", 503)


@app.route('/health/live')
def liveness_check():
    """Kubernetes-style liveness probe."""
    return api_success({'alive': True})


# ============================================
# Request Logging and Tracing
# ============================================
@app.before_request
def before_request_handler():
    """Set up request context and logging."""
    g.request_start = time.time()
    g.request_id = hashlib.md5(f"{time.time()}{request.path}".encode()).hexdigest()[:8]

    # Skip logging for health checks
    if request.path.startswith('/health'):
        return

    # Rate limiting for API endpoints
    if request.path.startswith('/api/'):
        client_id = request.headers.get('X-Forwarded-For', request.remote_addr)
        if not check_rate_limit(client_id):
            logger.warning(f"[{g.request_id}] Rate limit exceeded for {client_id}")
            return api_error("Rate limit exceeded. Please try again later.", 429)

    logger.info(f"[{g.request_id}] {request.method} {request.path}")


@app.after_request
def after_request_handler(response):
    """Log response and add headers."""
    # Skip for health checks
    if request.path.startswith('/health'):
        return response

    # Calculate request duration
    duration = (time.time() - getattr(g, 'request_start', time.time())) * 1000
    request_id = getattr(g, 'request_id', 'unknown')

    # Add request ID to response headers
    response.headers['X-Request-ID'] = request_id

    # Log response
    logger.info(f"[{request_id}] Response {response.status_code} ({duration:.1f}ms)")

    return response

# Global storage for SSE progress updates
# Key: app_id, Value: {'queue': Queue, 'result': dict, 'complete': bool}
apply_progress_store = {}
apply_progress_lock = threading.Lock()


# ============================================
# Error Handlers (Standardized)
# ============================================
@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all exceptions and return JSON for API routes."""
    import traceback
    request_id = getattr(g, 'request_id', 'unknown')

    # Log the error
    logger.error(f"[{request_id}] Exception: {e}")
    traceback.print_exc()

    # Check if this is an API route
    if request.path.startswith('/api/'):
        return api_error(str(e), 500, {'request_id': request_id})

    # For non-API routes, re-raise to use default Flask error handling
    raise e


@app.errorhandler(500)
def handle_500(e):
    """Handle 500 errors - return JSON for API routes."""
    request_id = getattr(g, 'request_id', 'unknown')
    if request.path.startswith('/api/'):
        return api_error('Internal server error', 500, {'request_id': request_id})
    raise e


@app.errorhandler(404)
def handle_404(e):
    """Handle 404 errors."""
    if request.path.startswith('/api/'):
        return api_error('Resource not found', 404)
    return render_template('base.html'), 404


@app.errorhandler(429)
def handle_429(e):
    """Handle rate limit errors."""
    return api_error('Rate limit exceeded. Please try again later.', 429)


# ============================================
# Cleanup Utilities
# ============================================
def cleanup_old_output_files(max_age_days: int = 7):
    """Remove output files older than max_age_days."""
    output_dir = Path('output')
    if not output_dir.exists():
        return 0

    cutoff_time = datetime.now() - timedelta(days=max_age_days)
    removed_count = 0

    try:
        for item in output_dir.iterdir():
            if item.is_dir():
                # Check modification time
                mtime = datetime.fromtimestamp(item.stat().st_mtime)
                if mtime < cutoff_time:
                    shutil.rmtree(item, ignore_errors=True)
                    removed_count += 1
                    logger.info(f"Cleaned up old output directory: {item.name}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

    return removed_count


@app.route('/api/admin/cleanup', methods=['POST'])
def trigger_cleanup():
    """Admin endpoint to trigger cleanup of old files."""
    data = request.get_json(silent=True) or {}
    max_age_days = data.get('max_age_days', 7)

    removed = cleanup_old_output_files(max_age_days)
    return api_success({'removed_directories': removed, 'max_age_days': max_age_days})


# In-memory storage backed by disk persistence
applications = []
uploaded_resumes = {}
uploaded_cover_letters = {}
default_resume_filename = None  # tracks which uploaded resume is active

UPLOAD_DATA_FILE = Path('uploads/upload_data.json')
APPLICATIONS_DATA_FILE = Path('data/applications.json')
SETTINGS_FILE = Path('data/settings.json')


def _save_upload_data():
    """Persist upload metadata to disk or database."""
    if is_database_available():
        try:
            with get_db_session() as db:
                # Save resumes
                for filename, data in uploaded_resumes.items():
                    existing = db.query(UploadedDocumentDB).filter_by(
                        filename=filename, doc_type='resume'
                    ).first()
                    if existing:
                        existing.text_content = data.get('text', '')
                        existing.is_default = (filename == default_resume_filename)
                    else:
                        doc = UploadedDocumentDB(
                            filename=filename,
                            doc_type='resume',
                            text_content=data.get('text', ''),
                            is_default=(filename == default_resume_filename)
                        )
                        db.add(doc)
                # Save cover letters
                for filename, data in uploaded_cover_letters.items():
                    existing = db.query(UploadedDocumentDB).filter_by(
                        filename=filename, doc_type='cover_letter'
                    ).first()
                    if existing:
                        existing.text_content = data.get('text', '')
                    else:
                        doc = UploadedDocumentDB(
                            filename=filename,
                            doc_type='cover_letter',
                            text_content=data.get('text', '')
                        )
                        db.add(doc)
            return
        except Exception as e:
            print(f"Database save failed, falling back to file: {e}")

    # Fallback to file storage
    data = {
        'resumes': uploaded_resumes,
        'cover_letters': uploaded_cover_letters,
        'default_resume': default_resume_filename,
    }
    UPLOAD_DATA_FILE.parent.mkdir(exist_ok=True)
    UPLOAD_DATA_FILE.write_text(json.dumps(data, indent=2))


def _load_upload_data():
    """Load persisted upload data from database or disk."""
    global uploaded_resumes, uploaded_cover_letters, default_resume_filename

    if is_database_available():
        try:
            with get_db_session() as db:
                # Load resumes
                resumes = db.query(UploadedDocumentDB).filter_by(doc_type='resume').all()
                for doc in resumes:
                    uploaded_resumes[doc.filename] = {
                        'text': doc.text_content or '',
                        'uploaded': doc.uploaded_at.isoformat() if doc.uploaded_at else ''
                    }
                    if doc.is_default:
                        default_resume_filename = doc.filename
                # Load cover letters
                covers = db.query(UploadedDocumentDB).filter_by(doc_type='cover_letter').all()
                for doc in covers:
                    uploaded_cover_letters[doc.filename] = {
                        'text': doc.text_content or '',
                        'uploaded': doc.uploaded_at.isoformat() if doc.uploaded_at else ''
                    }
            return
        except Exception as e:
            print(f"Database load failed, falling back to file: {e}")

    # Fallback to file storage
    if UPLOAD_DATA_FILE.exists():
        try:
            data = json.loads(UPLOAD_DATA_FILE.read_text())
            uploaded_resumes = data.get('resumes', {})
            uploaded_cover_letters = data.get('cover_letters', {})
            default_resume_filename = data.get('default_resume')
        except Exception:
            pass


def _save_applications_data():
    """Persist applications to database or disk."""
    if is_database_available():
        try:
            with get_db_session() as db:
                for app in applications:
                    existing = db.query(ApplicationDB).filter_by(id=app.id).first()
                    if existing:
                        # Update existing
                        existing.status = app.status.value if hasattr(app.status, 'value') else str(app.status)
                        existing.error_message = app.error_message
                        existing.notes = app.notes
                        existing.follow_up_date = app.follow_up_date
                        existing.email_sent = app.email_sent
                        existing.applied_at = app.applied_at
                        if app.job_description:
                            existing.company_name = app.job_description.company_name
                            existing.job_title = app.job_description.job_title
                            existing.location = app.job_description.location
                            existing.job_description_json = app.job_description.model_dump(mode='json') if hasattr(app.job_description, 'model_dump') else None
                        if app.tailored_resume:
                            existing.tailored_resume_json = app.tailored_resume.model_dump(mode='json') if hasattr(app.tailored_resume, 'model_dump') else None
                        if app.cover_letter:
                            existing.cover_letter_json = app.cover_letter.model_dump(mode='json') if hasattr(app.cover_letter, 'model_dump') else None
                        existing.output_dir = str(app.output_dir) if app.output_dir else None
                        existing.resume_filename = app.resume_filename
                        existing.cover_letter_filename = app.cover_letter_filename
                    else:
                        # Create new
                        app_db = ApplicationDB(
                            id=app.id,
                            job_url=str(app.job_url),
                            status=app.status.value if hasattr(app.status, 'value') else str(app.status),
                            company_name=app.job_description.company_name if app.job_description else None,
                            job_title=app.job_description.job_title if app.job_description else None,
                            location=app.job_description.location if app.job_description else None,
                            job_description_json=app.job_description.model_dump(mode='json') if app.job_description and hasattr(app.job_description, 'model_dump') else None,
                            tailored_resume_json=app.tailored_resume.model_dump(mode='json') if app.tailored_resume and hasattr(app.tailored_resume, 'model_dump') else None,
                            cover_letter_json=app.cover_letter.model_dump(mode='json') if app.cover_letter and hasattr(app.cover_letter, 'model_dump') else None,
                            output_dir=str(app.output_dir) if app.output_dir else None,
                            resume_filename=app.resume_filename,
                            cover_letter_filename=app.cover_letter_filename,
                            error_message=app.error_message,
                            email_sent=app.email_sent,
                            applied_at=app.applied_at
                        )
                        db.add(app_db)
            return
        except Exception as e:
            print(f"Database save failed, falling back to file: {e}")

    # Fallback to file storage
    APPLICATIONS_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = []
    for app in applications:
        app_dict = app.model_dump(mode='json')
        if app_dict.get('output_dir'):
            app_dict['output_dir'] = str(app_dict['output_dir'])
        data.append(app_dict)
    APPLICATIONS_DATA_FILE.write_text(json.dumps(data, indent=2, default=str))


def _load_applications_data():
    """Load persisted applications from database or disk."""
    global applications

    if is_database_available():
        try:
            with get_db_session() as db:
                app_records = db.query(ApplicationDB).order_by(ApplicationDB.created_at.desc()).all()
                for app_db in app_records:
                    app_dict = {
                        'id': app_db.id,
                        'job_url': app_db.job_url,
                        'status': app_db.status,
                        'error_message': app_db.error_message,
                        'notes': app_db.notes,
                        'follow_up_date': app_db.follow_up_date,
                        'email_sent': app_db.email_sent,
                        'applied_at': app_db.applied_at,
                        'output_dir': Path(app_db.output_dir) if app_db.output_dir else None,
                        'resume_filename': app_db.resume_filename,
                        'cover_letter_filename': app_db.cover_letter_filename,
                    }
                    # Reconstruct complex objects from JSON if needed
                    applications.append(Application(**app_dict))
            return
        except Exception as e:
            print(f"Database load failed, falling back to file: {e}")

    # Fallback to file storage
    if APPLICATIONS_DATA_FILE.exists():
        try:
            data = json.loads(APPLICATIONS_DATA_FILE.read_text())
            for app_dict in data:
                if app_dict.get('output_dir'):
                    app_dict['output_dir'] = Path(app_dict['output_dir'])
                applications.append(Application(**app_dict))
        except Exception as e:
            print(f"Warning: Could not load applications: {e}")


def _load_settings():
    """Load settings from file."""
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_settings(data):
    """Save settings to file and update environment."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Load existing settings and merge
    existing = _load_settings()
    existing.update(data)

    SETTINGS_FILE.write_text(json.dumps(existing, indent=2))

    # Update .env file for API keys
    env_path = Path('.env')
    env_content = []

    if env_path.exists():
        env_content = env_path.read_text().splitlines()

    # Update or add keys
    key_mapping = {
        'anthropic_key': 'ANTHROPIC_API_KEY',
        'openai_key': 'OPENAI_API_KEY',
        'google_key': 'GOOGLE_API_KEY',
        'smtp_host': 'SMTP_HOST',
        'smtp_port': 'SMTP_PORT',
        'smtp_user': 'SMTP_USER',
        'smtp_password': 'SMTP_PASSWORD'
    }

    for data_key, env_key in key_mapping.items():
        if data_key in data and data[data_key]:
            # Update environment variable
            os.environ[env_key] = data[data_key]

            # Update .env file
            found = False
            for i, line in enumerate(env_content):
                if line.startswith(f'{env_key}='):
                    env_content[i] = f'{env_key}={data[data_key]}'
                    found = True
                    break
            if not found:
                env_content.append(f'{env_key}={data[data_key]}')

    env_path.write_text('\n'.join(env_content) + '\n')
    return existing


def _mask_key(key):
    """Mask API key for display."""
    if not key or len(key) < 10:
        return ''
    return key[:8] + '...' + key[-4:]


print("[Init] Loading data...", flush=True)
try:
    _load_upload_data()
    print("[Init] Upload data loaded", flush=True)
except Exception as e:
    print(f"[Init] Failed to load upload data: {e}", flush=True)

try:
    _load_applications_data()
    print("[Init] Applications data loaded", flush=True)
except Exception as e:
    print(f"[Init] Failed to load applications data: {e}", flush=True)
print("[Init] App ready!", flush=True)


def load_personal_info():
    """Load the saved profile. Credential fields are never returned."""
    return _load_profile()


def save_personal_info(info):
    """Persist the profile after stripping any credential fields."""
    return _save_profile(info)


# Remove passwords left behind by older versions of this app.
try:
    _scrubbed = scrub_profile_files()
    if _scrubbed:
        print(f"[Init] Removed stored credentials from: {', '.join(_scrubbed)}", flush=True)
except Exception as e:
    print(f"[Init] Credential scrub failed: {e}", flush=True)

# Older builds saved logged-in browser sessions (cookies) here; remove them.
try:
    _legacy_session = Path('data/browser_state.json')
    if _legacy_session.exists():
        _legacy_session.unlink()
        print("[Init] Removed saved browser session state", flush=True)
except Exception as e:
    print(f"[Init] Could not remove saved browser session: {e}", flush=True)


def extract_text_from_pdf(file_bytes):
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join([page.extract_text() or '' for page in reader.pages]).strip()


def extract_text_from_docx(file_bytes):
    doc = Document(io.BytesIO(file_bytes))
    return "\n".join([para.text for para in doc.paragraphs]).strip()


def extract_text_from_file(file):
    file_bytes = file.read()
    filename = file.filename.lower()
    if filename.endswith('.pdf'):
        return extract_text_from_pdf(file_bytes)
    elif filename.endswith('.docx'):
        return extract_text_from_docx(file_bytes)
    return file_bytes.decode('utf-8', errors='ignore')


def get_resume_text(session_id='default'):
    global default_resume_filename, _external_resume_text

    # Priority 1: Check for externally provided resume (from jobportal)
    if session_id in _external_resume_text:
        return _external_resume_text[session_id]['text']

    # Priority 2: Use selected default uploaded resume if available
    if default_resume_filename and default_resume_filename in uploaded_resumes:
        return uploaded_resumes[default_resume_filename]['text']

    # Priority 3: Fall back to resume.txt
    resume_path = Path("resume.txt")
    if resume_path.exists():
        return resume_path.read_text()

    return ""


# Initialize external resume storage
_external_resume_text = {}


def gen_id(url):
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(url.encode()).hexdigest()[:8]}"


async def process_job_async(url, resume_text, personal_info=None):
    fetcher = JobFetcher()
    analyzer = JobAnalyzer()
    generator = ResumeGenerator()
    pdf_gen = PDFGenerator()
    base = parse_resume_from_text(resume_text)
    app_record = Application(id=gen_id(url), job_url=url)

    try:
        # Step 1: Fetch raw job posting
        job = await fetcher.fetch_job(url)
        app_record.job_description = job
        app_record.status = ApplicationStatus.JD_FETCHED

        # Step 2: AI metadata extraction (enriches job with location, salary, etc.)
        job = await analyzer.extract_metadata(job)
        app_record.job_description = job

        # Step 3: Deterministic viability assessment
        if personal_info:
            viability = analyzer.assess_viability(job, personal_info)
            app_record.job_viability = viability

        # Step 4: Generate tailored resume
        resume = await generator.generate_tailored_resume(job, base, personal_info=personal_info)
        app_record.tailored_resume = resume
        app_record.status = ApplicationStatus.RESUME_GENERATED

        # Step 5: Generate cover letter
        cover = await generator.generate_cover_letter(job, base, resume, personal_info=personal_info)
        app_record.cover_letter = cover

        # Step 6: Generate PDFs
        safe = lambda s: "".join(c if c.isalnum() else "_" for c in s)[:30]
        out_dir = Path.cwd() / "output" / f"{safe(job.company_name)}_{safe(job.job_title)}_{app_record.id}"
        out_dir.mkdir(parents=True, exist_ok=True)
        app_record.output_dir = out_dir  # Store as absolute path

        # Build meaningful file names: FirstName_LastName_Company_Resume.pdf
        name_part = base.full_name.replace(" ", "_")
        company_part = safe(job.company_name)
        resume_filename = f"{name_part}_{company_part}_Resume.pdf"
        cover_filename = f"{name_part}_{company_part}_Cover_Letter.pdf"

        app_record.resume_filename = resume_filename
        app_record.cover_letter_filename = cover_filename

        resume_path = out_dir / resume_filename
        cover_path = out_dir / cover_filename
        pdf_gen.generate_resume_pdf(resume, base, resume_path)
        pdf_gen.generate_cover_letter_pdf(cover, base, cover_path)
        (out_dir / "resume.txt").write_text(resume.full_text)
        (out_dir / "cover_letter.txt").write_text(cover.content)

        # Verify PDFs were created
        if not resume_path.exists():
            raise Exception(f"Failed to create resume PDF at {resume_path}")
        print(f"[Process] Resume PDF created: {resume_path} ({resume_path.stat().st_size} bytes)", flush=True)

        app_record.status = ApplicationStatus.PDF_GENERATED

    except Exception as e:
        app_record.status = ApplicationStatus.FAILED
        app_record.error_message = str(e)
    finally:
        await fetcher.close()

    return app_record


@app.route('/')
def index():
    # Docs Ready = documents generated but not yet applied
    docs_ready_statuses = {ApplicationStatus.PDF_GENERATED}
    # Applied = form filled or submitted
    applied_statuses = {ApplicationStatus.FORM_OPENED, ApplicationStatus.FORM_FILLED, ApplicationStatus.SUBMITTED}
    stats = {
        'total': len(applications),
        'docs_ready': sum(1 for a in applications if a.status in docs_ready_statuses),
        'applied': sum(1 for a in applications if a.status in applied_statuses),
        'failed': sum(1 for a in applications if a.status in {ApplicationStatus.FAILED, ApplicationStatus.MANUAL_REQUIRED})
    }
    return render_template('index.html', stats=stats, active_page='applications')


@app.route('/documents')
def documents():
    resumes = list(uploaded_resumes.keys())
    cover_letters = list(uploaded_cover_letters.keys())
    return render_template('documents.html',
                         resumes=resumes,
                         cover_letters=cover_letters,
                         default_resume=default_resume_filename,
                         active_page='documents')


@app.route('/api/documents/status')
def documents_status():
    """API endpoint to check document upload status."""
    resume_ready = bool(get_resume_text())
    return jsonify({
        'resume_uploaded': bool(uploaded_resumes),
        'resume_ready': resume_ready,
        'default_resume': default_resume_filename,
        'resume_count': len(uploaded_resumes),
        'cover_letter_count': len(uploaded_cover_letters)
    })


# Temporary storage for externally provided resumes (from jobportal)
_external_resume_text = {}


@app.route('/api/resume/external', methods=['POST'])
def set_external_resume():
    """Accept resume text from external source (e.g., jobportal).

    This allows the jobportal to pass resume text to autoapply
    without requiring users to upload separately to both services.
    """
    global _external_resume_text
    data = request.get_json()
    resume_text = data.get('resume_text', '')
    session_id = data.get('session_id', 'default')

    if not resume_text:
        return jsonify({'error': 'No resume text provided'}), 400

    _external_resume_text[session_id] = {
        'text': resume_text,
        'timestamp': datetime.now().isoformat(),
        'source': 'jobportal'
    }

    return jsonify({
        'success': True,
        'session_id': session_id,
        'message': 'Resume text stored for this session'
    })


@app.route('/api/resume/external/<session_id>', methods=['GET'])
def get_external_resume(session_id):
    """Get externally provided resume text."""
    if session_id not in _external_resume_text:
        return jsonify({'error': 'No external resume found for this session'}), 404

    return jsonify({
        'resume_text': _external_resume_text[session_id]['text'],
        'source': _external_resume_text[session_id]['source']
    })


@app.route('/profile')
def profile():
    info = load_personal_info()
    return render_template('profile.html', info=info, active_page='profile')


@app.route('/history')
def history():
    return render_template('history.html', applications=applications, active_page='history')


@app.route('/settings')
def settings_page():
    stored = _load_settings()
    # Mask keys for display
    display_settings = {
        'anthropic_key': stored.get('anthropic_key', ''),
        'openai_key': stored.get('openai_key', ''),
        'google_key': stored.get('google_key', ''),
        'smtp_host': stored.get('smtp_host', ''),
        'smtp_port': stored.get('smtp_port', '587'),
        'smtp_user': stored.get('smtp_user', ''),
        'smtp_password': stored.get('smtp_password', ''),
    }
    return render_template('settings.html', settings=display_settings, active_page='settings')


def find_existing_application(url: str):
    """Find an existing application for the same URL with generated documents."""
    url_normalized = url.strip().lower().rstrip('/')
    for app in applications:
        app_url = str(app.job_url).lower().rstrip('/')
        if app_url == url_normalized:
            # Check if documents exist
            if (app.output_dir and
                app.resume_filename and
                app.status in {ApplicationStatus.PDF_GENERATED, ApplicationStatus.FORM_FILLED,
                              ApplicationStatus.FORM_OPENED, ApplicationStatus.SUBMITTED}):
                # Verify files still exist
                resume_path = Path(app.output_dir) / app.resume_filename
                if resume_path.exists():
                    return app
    return None


@app.route('/api/process', methods=['POST'])
def process_jobs():
    data = request.get_json()
    urls = data.get('urls', [])
    force_regenerate = data.get('force_regenerate', False)

    if not urls:
        return jsonify({'error': 'No URLs provided'}), 400

    resume_text = get_resume_text()
    if not resume_text:
        return jsonify({'error': 'No resume uploaded'}), 400

    personal_info = load_personal_info()

    results = []
    for url in urls:
        try:
            # Check for existing application with same URL (unless force regenerate)
            existing_app = None if force_regenerate else find_existing_application(url.strip())

            if existing_app:
                # Use cached application
                app_record = existing_app
                app_record.cached = True  # Mark as cached for UI
            else:
                # Generate new documents
                app_record = asyncio.run(process_job_async(url.strip(), resume_text, personal_info=personal_info))
                app_record.cached = False
                applications.insert(0, app_record)
                _save_applications_data()

            jd = app_record.job_description
            viab = app_record.job_viability
            # Use stored filenames from the app_record
            resume_file = app_record.resume_filename or "resume.pdf"
            cover_file = app_record.cover_letter_filename or "cover_letter.pdf"

            # Generate skills comparison matrix
            skills_comparison = None
            if jd:
                # Parse resume to get base skills
                base_resume = parse_resume_from_text(resume_text)
                analyzer = JobAnalyzer()
                skills_comparison = analyzer.generate_skills_comparison(jd, base_resume.skills)

            result = {
                'id': app_record.id,
                'url': url,
                'status': app_record.status.value,
                'company': jd.company_name if jd else None,
                'title': jd.job_title if jd else None,
                'location': jd.location if jd else None,
                'salary': jd.salary_range if jd else None,
                'job_type': jd.job_type if jd else None,
                'seniority': jd.seniority_level if jd else None,
                'remote_type': jd.remote_type if jd else None,
                'posting_date': jd.posting_date if jd else None,
                'is_likely_active': jd.is_likely_active if jd else None,
                'freshness_warning': jd.freshness_warning if jd else None,
                'experience_required': jd.experience_required if jd else None,
                'technologies': jd.technologies if jd else [],
                'required_skills': jd.required_skills if jd else [],
                'preferred_skills': jd.preferred_skills if jd else [],
                'match_score': app_record.tailored_resume.match_score if app_record.tailored_resume else None,
                'viability_recommendation': viab.overall_recommendation if viab else None,
                'viability_score': viab.match_score if viab else None,
                'viability_notes': viab.viability_notes if viab else [],
                'error': app_record.error_message,
                'output_dir': str(app_record.output_dir) if app_record.output_dir else None,
                'resume_file': resume_file,
                'cover_file': cover_file,
                # Inline display text for resume and cover letter
                'resume_text': app_record.tailored_resume.full_text if app_record.tailored_resume else None,
                'cover_letter_text': app_record.cover_letter.content if app_record.cover_letter else None,
                # Cached indicator
                'cached': getattr(app_record, 'cached', False),
                # Skills comparison matrix
                'skills_comparison': skills_comparison
            }
            results.append(result)
        except Exception as e:
            results.append({'url': url, 'status': 'failed', 'error': str(e)})

    return jsonify({'results': results})


ALLOWED_EXTENSIONS = {'.pdf', '.docx'}


def _is_allowed_file(filename):
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


@app.route('/api/upload/resume', methods=['POST'])
def upload_resume():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not _is_allowed_file(file.filename):
        return jsonify({'error': 'Only PDF and DOCX files are allowed'}), 400

    try:
        text = extract_text_from_file(file)
        filename = secure_filename(file.filename)
        uploaded_resumes[filename] = {'text': text, 'uploaded': datetime.now().isoformat()}
        _save_upload_data()
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload/cover-letter', methods=['POST'])
def upload_cover_letter():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not _is_allowed_file(file.filename):
        return jsonify({'error': 'Only PDF and DOCX files are allowed'}), 400

    try:
        text = extract_text_from_file(file)
        filename = secure_filename(file.filename)
        uploaded_cover_letters[filename] = {'text': text, 'uploaded': datetime.now().isoformat()}
        _save_upload_data()
        return jsonify({'success': True, 'filename': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/preview/resume/<filename>')
def preview_resume(filename):
    if filename in uploaded_resumes:
        return jsonify({'text': uploaded_resumes[filename]['text']})
    return jsonify({'error': 'File not found'}), 404


@app.route('/api/resume/set-default', methods=['POST'])
def set_default_resume():
    global default_resume_filename
    data = request.get_json()
    filename = data.get('filename')
    if filename and filename in uploaded_resumes:
        default_resume_filename = filename
        # Also write to resume.txt so it persists
        Path("resume.txt").write_text(uploaded_resumes[filename]['text'])
        _save_upload_data()
        return jsonify({'success': True, 'default': filename})
    return jsonify({'error': 'Resume not found'}), 404


@app.route('/api/resume/update-text', methods=['POST'])
def update_resume_text():
    global default_resume_filename
    data = request.get_json()
    filename = data.get('filename')
    text = data.get('text', '')
    if filename and filename in uploaded_resumes:
        uploaded_resumes[filename]['text'] = text
        # If this is the default resume, also update resume.txt
        if filename == default_resume_filename:
            Path("resume.txt").write_text(text)
        _save_upload_data()
        return jsonify({'success': True})
    return jsonify({'error': 'Resume not found'}), 404


@app.route('/api/resume/experience/<filename>')
def get_resume_experience(filename):
    """Return parsed experience from an uploaded resume."""
    if filename not in uploaded_resumes:
        return jsonify({'error': 'File not found'}), 404
    parsed = parse_resume_from_text(uploaded_resumes[filename]['text'])
    return jsonify({'experience': parsed.experience})


@app.route('/api/resume/experience/<filename>', methods=['POST'])
def save_resume_experience(filename):
    """Save edited experience back into the resume text."""
    global default_resume_filename
    if filename not in uploaded_resumes:
        return jsonify({'error': 'File not found'}), 404

    data = request.get_json()
    experience = data.get('experience', [])

    # Rebuild the experience section text from the structured data
    exp_lines = []
    for job in experience:
        if job.get('job_title'):
            exp_lines.append(job['job_title'])
        company_line = job.get('company', '')
        if job.get('location'):
            company_line += f", {job['location']}"
        if job.get('dates'):
            company_line += f" | {job['dates']}"
        if company_line:
            exp_lines.append(company_line)
        for bullet in job.get('bullets', []):
            if bullet.strip():
                exp_lines.append(f"• {bullet.strip()}")
        exp_lines.append('')  # blank line between jobs

    # Replace the experience section in the raw text
    text = uploaded_resumes[filename]['text']
    lines = text.split('\n')
    new_lines = []
    in_experience = False
    experience_inserted = False
    next_section_found = False

    for line in lines:
        stripped = line.strip().lower()
        # Detect experience section header
        if not in_experience and not experience_inserted:
            is_exp_header = any(kw in stripped for kw in
                ['professional experience', 'work experience', 'experience', 'employment history'])
            if is_exp_header and len(stripped) < 60:
                in_experience = True
                new_lines.append(line)  # keep the header
                # Insert new experience content
                for el in exp_lines:
                    new_lines.append(el)
                experience_inserted = True
                continue

        if in_experience and not next_section_found:
            # Skip old experience lines until we hit the next section
            is_next_section = any(kw in stripped for kw in
                ['education', 'certifications', 'certificates', 'skills',
                 'technical expertise', 'projects', 'awards', 'publications'])
            if is_next_section and len(stripped) < 60:
                next_section_found = True
                in_experience = False
                new_lines.append(line)
            # else: skip old experience line
            continue

        new_lines.append(line)

    updated_text = '\n'.join(new_lines)
    uploaded_resumes[filename]['text'] = updated_text

    # Sync to resume.txt if this is the active resume
    if filename == default_resume_filename:
        Path("resume.txt").write_text(updated_text)
    _save_upload_data()

    return jsonify({'success': True})


@app.route('/api/profile', methods=['POST'])
def save_profile():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid profile data'}), 400
    save_personal_info(data)
    return jsonify({'success': True})


@app.route('/api/profile/add-fields', methods=['POST'])
def add_profile_fields():
    """Add missing fields to personal info without overwriting existing data."""
    data = request.get_json()
    fields = data.get('fields', {})

    if not fields:
        return jsonify({'success': False, 'error': 'No fields provided'}), 400

    # Load existing info and add new fields (credential fields are never accepted)
    current_info = load_personal_info()
    for key, value in strip_credentials(fields).items():
        if value and not current_info.get(key):  # Only add if not already set
            current_info[key] = value

    save_personal_info(current_info)
    return jsonify({'success': True, 'updated_fields': list(fields.keys())})


@app.route('/api/delete/resume/<filename>', methods=['DELETE'])
def delete_resume(filename):
    global default_resume_filename
    if filename in uploaded_resumes:
        del uploaded_resumes[filename]
        if default_resume_filename == filename:
            default_resume_filename = None
        _save_upload_data()
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404


@app.route('/api/delete/cover-letter/<filename>', methods=['DELETE'])
def delete_cover_letter(filename):
    if filename in uploaded_cover_letters:
        del uploaded_cover_letters[filename]
        _save_upload_data()
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404


@app.route('/download/<path:filepath>')
def download_file(filepath):
    # Only files generated under output/ may be downloaded (no path traversal).
    # Links embed the absolute output_dir, whose leading slash may be merged away.
    output_root = (Path.cwd() / 'output').resolve()
    for candidate in (Path(filepath), Path.cwd() / filepath, Path('/' + filepath.lstrip('/'))):
        try:
            full_path = candidate.resolve()
            full_path.relative_to(output_root)
        except (ValueError, OSError):
            continue
        if full_path.is_file():
            return send_file(full_path, as_attachment=True)
    return "File not found", 404


def _run_auto_apply_background(app_id: str, app_record, resume_pdf_path: Path,
                                cover_letter_path: Path, auto_submit: bool,
                                headless: bool, base_resume, personal_info: dict,
                                resume_text: str, job_description_text: str):
    """Background worker for auto-apply with progress updates via SSE."""
    progress_queue = None
    with apply_progress_lock:
        if app_id in apply_progress_store:
            progress_queue = apply_progress_store[app_id]['queue']

    def progress_callback(progress_data):
        """Callback to push progress updates to the queue."""
        if progress_queue:
            try:
                progress_queue.put_nowait(progress_data)
            except:
                pass

    async def run_auto_apply():
        automation = BrowserAutomation(headless=headless, personal_info=personal_info)
        try:
            result = await automation.auto_apply(
                application=app_record,
                base_resume=base_resume,
                resume_pdf_path=resume_pdf_path,
                cover_letter_pdf_path=cover_letter_path,
                auto_submit=auto_submit,
                resume_text=resume_text,
                job_description_text=job_description_text,
                progress_callback=progress_callback
            )
            return result
        finally:
            if headless:
                try:
                    await automation.stop()
                except Exception:
                    pass
            else:
                print("[AutoApply] Browser left open for user review. Close manually when done.")

    try:
        result = asyncio.run(run_auto_apply())

        # Save updated application status
        _save_applications_data()

        # Auto-send email notification ONLY if application was truly submitted
        if result.success and result.status == 'submitted':
            try:
                if email_notifier.is_configured():
                    email_success, email_msg = email_notifier.send_application_receipt(
                        app_record, personal_info, resume_pdf_path, cover_letter_path
                    )
                    if email_success:
                        app_record.email_sent = True
                        _save_applications_data()
                        result.message += " Email notification sent."
            except Exception as email_err:
                result.message += f" (Email failed: {str(email_err)})"

        # Store final result
        with apply_progress_lock:
            if app_id in apply_progress_store:
                apply_progress_store[app_id]['result'] = result.to_dict()
                apply_progress_store[app_id]['complete'] = True
                # Push final result to queue
                if progress_queue:
                    progress_queue.put_nowait({'type': 'complete', 'result': result.to_dict()})

    except Exception as e:
        import traceback
        traceback.print_exc()
        error_result = {
            'success': False,
            'error': str(e),
            'progress': {'stages': {}, 'logs': [str(e)]}
        }
        with apply_progress_lock:
            if app_id in apply_progress_store:
                apply_progress_store[app_id]['result'] = error_result
                apply_progress_store[app_id]['complete'] = True
                if progress_queue:
                    progress_queue.put_nowait({'type': 'error', 'result': error_result})


@app.route('/api/apply/<app_id>', methods=['POST'])
def apply_to_job(app_id):
    """Trigger browser automation to auto-apply for a specific application."""
    from flask import make_response

    def json_response(data, status=200):
        """Helper to ensure JSON response with proper headers."""
        response = make_response(jsonify(data), status)
        response.headers['Content-Type'] = 'application/json'
        return response

    try:
        # Find the application
        app_record = None
        for app in applications:
            if app.id == app_id:
                app_record = app
                break

        if not app_record:
            return json_response({'success': False, 'error': 'Application not found'}, 404)

        if not app_record.output_dir or not app_record.resume_filename:
            return json_response({'success': False, 'error': 'No resume generated for this application'}, 400)

        # Resolve output directory - handle both relative and absolute paths
        output_dir = Path(app_record.output_dir)
        if not output_dir.is_absolute():
            # Try relative to current working directory first
            output_dir = Path.cwd() / output_dir

        resume_pdf_path = output_dir / app_record.resume_filename

        # Debug: log the path being checked
        print(f"[AutoApply] Checking resume PDF at: {resume_pdf_path}", flush=True)
        print(f"[AutoApply] Output dir exists: {output_dir.exists()}", flush=True)
        print(f"[AutoApply] Resume PDF exists: {resume_pdf_path.exists()}", flush=True)

        if not resume_pdf_path.exists():
            # List files in output dir for debugging
            if output_dir.exists():
                files = list(output_dir.iterdir())
                print(f"[AutoApply] Files in output dir: {files}", flush=True)
            return json_response({
                'success': False,
                'error': f'Resume PDF not found at: {resume_pdf_path}',
                'debug': {
                    'output_dir': str(output_dir),
                    'resume_filename': app_record.resume_filename,
                    'output_dir_exists': output_dir.exists()
                }
            }, 400)

        cover_letter_path = None
        if app_record.cover_letter_filename:
            cover_letter_path = output_dir / app_record.cover_letter_filename
            if not cover_letter_path.exists():
                print(f"[AutoApply] Cover letter not found at: {cover_letter_path}", flush=True)
                cover_letter_path = None

        # Get flags from request - default to fully automated
        try:
            data = request.get_json(silent=True) or {}
        except Exception:
            data = {}
        # Never submit unless the caller explicitly opts in; the default stops at review.
        auto_submit = data.get('auto_submit', False) is True
        headless = data.get('headless', True) is not False
        if not headless and sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
            headless = True  # no display on the server

        # Parse resume to get base info for form filling
        resume_text = get_resume_text()
        if not resume_text:
            return json_response({'success': False, 'error': 'No resume text available'}, 400)

        base_resume = parse_resume_from_text(resume_text)
        personal_info = load_personal_info()

        job_description_text = ""
        if app_record.job_description:
            job_description_text = app_record.job_description.raw_text or ""

        # Initialize progress store for this application
        with apply_progress_lock:
            apply_progress_store[app_id] = {
                'queue': queue.Queue(),
                'result': None,
                'complete': False,
                'started_at': time.time()
            }

        # Start background thread for auto-apply
        thread = threading.Thread(
            target=_run_auto_apply_background,
            args=(app_id, app_record, resume_pdf_path, cover_letter_path,
                  auto_submit, headless, base_resume, personal_info,
                  resume_text, job_description_text),
            daemon=True
        )
        thread.start()

        # Return immediately with started status
        return json_response({
            'success': True,
            'status': 'started',
            'message': 'Auto-apply started. Connect to /api/apply/{}/progress for updates.'.format(app_id),
            'progress_url': '/api/apply/{}/progress'.format(app_id)
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return json_response({
            'success': False,
            'error': str(e),
            'progress': {'stages': {}, 'logs': [str(e)]}
        }, 500)


@app.route('/api/apply/<app_id>/progress')
def apply_progress_stream(app_id):
    """SSE endpoint for streaming auto-apply progress updates."""
    def generate():
        # Check if this application has an active progress stream
        with apply_progress_lock:
            if app_id not in apply_progress_store:
                yield 'data: {}\n\n'.format(json.dumps({
                    'type': 'error',
                    'message': 'No active auto-apply for this application. Start one first.'
                }))
                return

            progress_queue = apply_progress_store[app_id]['queue']

        # Stream progress updates
        timeout_count = 0
        max_timeout = 300  # 5 minutes max
        while timeout_count < max_timeout:
            try:
                # Wait for a progress update (1 second timeout)
                update = progress_queue.get(timeout=1)

                # Check if this is the final result
                if isinstance(update, dict) and update.get('type') in ('complete', 'error'):
                    yield 'data: {}\n\n'.format(json.dumps(update))
                    break
                else:
                    yield 'data: {}\n\n'.format(json.dumps({'type': 'progress', 'data': update}))
                timeout_count = 0  # Reset timeout on successful update

            except queue.Empty:
                timeout_count += 1
                # Send heartbeat to keep connection alive
                yield 'data: {}\n\n'.format(json.dumps({'type': 'heartbeat'}))

                # Check if task completed while we were waiting
                with apply_progress_lock:
                    if app_id in apply_progress_store and apply_progress_store[app_id]['complete']:
                        result = apply_progress_store[app_id]['result']
                        yield 'data: {}\n\n'.format(json.dumps({'type': 'complete', 'result': result}))
                        break

        # Cleanup - mark stream as closed instead of removing queue
        with apply_progress_lock:
            if app_id in apply_progress_store:
                apply_progress_store[app_id]['stream_closed'] = True

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/email/<app_id>', methods=['POST'])
def send_email_receipt(app_id):
    """Send email receipt for an application."""
    # Find the application
    app_record = None
    for app in applications:
        if app.id == app_id:
            app_record = app
            break

    if not app_record:
        return jsonify({'success': False, 'error': 'Application not found'}), 404

    personal_info = load_personal_info()

    # Get PDF paths
    resume_pdf_path = None
    cover_letter_pdf_path = None
    if app_record.output_dir:
        if app_record.resume_filename:
            resume_pdf_path = Path(app_record.output_dir) / app_record.resume_filename
        if app_record.cover_letter_filename:
            cover_letter_pdf_path = Path(app_record.output_dir) / app_record.cover_letter_filename

    success, message = email_notifier.send_application_receipt(
        application=app_record,
        personal_info=personal_info,
        resume_pdf_path=resume_pdf_path,
        cover_letter_pdf_path=cover_letter_pdf_path
    )

    if success:
        app_record.email_sent = True
        _save_applications_data()

    return jsonify({'success': success, 'message': message})


@app.route('/api/application/<app_id>/status', methods=['PUT'])
def update_application_status(app_id):
    """Update application status manually."""
    data = request.get_json()
    new_status = data.get('status')

    if not new_status:
        return jsonify({'success': False, 'error': 'No status provided'}), 400

    # Find the application
    for app in applications:
        if app.id == app_id:
            try:
                app.status = ApplicationStatus(new_status)
                if new_status == 'submitted' and not app.applied_at:
                    app.applied_at = datetime.now()
                _save_applications_data()
                return jsonify({'success': True})
            except ValueError:
                return jsonify({'success': False, 'error': f'Invalid status: {new_status}'}), 400

    return jsonify({'success': False, 'error': 'Application not found'}), 404


@app.route('/api/application/<app_id>', methods=['DELETE'])
def delete_application(app_id):
    """Delete a single application."""
    global applications

    # Find and remove the application
    for i, app in enumerate(applications):
        if app.id == app_id:
            # Optionally delete output files
            if app.output_dir:
                import shutil
                output_path = Path(app.output_dir)
                if output_path.exists():
                    try:
                        shutil.rmtree(output_path)
                    except Exception:
                        pass  # Ignore file deletion errors

            applications.pop(i)
            _save_applications_data()
            return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'Application not found'}), 404


@app.route('/api/application/<app_id>/notes', methods=['PUT'])
def update_application_notes(app_id):
    """Update application notes."""
    data = request.get_json()
    notes = data.get('notes', '')

    # Find the application
    for app in applications:
        if app.id == app_id:
            app.notes = notes
            _save_applications_data()
            return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'Application not found'}), 404


@app.route('/api/application/<app_id>/follow-up', methods=['PUT'])
def update_application_followup(app_id):
    """Update application follow-up date."""
    data = request.get_json()
    follow_up_date = data.get('follow_up_date', '')

    # Find the application
    for app in applications:
        if app.id == app_id:
            app.follow_up_date = follow_up_date
            _save_applications_data()
            return jsonify({'success': True})

    return jsonify({'success': False, 'error': 'Application not found'}), 404


@app.route('/api/application/<app_id>/next-steps')
def get_next_steps(app_id):
    """Generate AI-powered next steps for an application."""
    # Find the application
    app_record = None
    for app in applications:
        if app.id == app_id:
            app_record = app
            break

    if not app_record:
        return jsonify({'success': False, 'error': 'Application not found'}), 404

    try:
        analyzer = JobAnalyzer()
        next_steps = asyncio.run(analyzer.generate_next_steps(app_record))
        app_record.next_steps = next_steps
        _save_applications_data()
        return jsonify({'success': True, 'next_steps': next_steps})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/clear-history', methods=['POST'])
def clear_history():
    applications.clear()
    _save_applications_data()
    return jsonify({'success': True})


@app.route('/api/settings', methods=['POST'])
def save_api_settings():
    """Save LLM API keys."""
    data = request.get_json()
    try:
        settings_data = {}
        if data.get('anthropic_key'):
            settings_data['anthropic_key'] = data['anthropic_key']
        if data.get('openai_key'):
            settings_data['openai_key'] = data['openai_key']
        if data.get('google_key'):
            settings_data['google_key'] = data['google_key']

        _save_settings(settings_data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/settings/email', methods=['POST'])
def save_email_settings():
    """Save email/SMTP settings."""
    data = request.get_json()
    try:
        settings_data = {
            'smtp_host': data.get('smtp_host', ''),
            'smtp_port': data.get('smtp_port', '587'),
            'smtp_user': data.get('smtp_user', ''),
            'smtp_password': data.get('smtp_password', ''),
        }
        _save_settings(settings_data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5001, host='0.0.0.0')
