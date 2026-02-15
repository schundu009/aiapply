"""Data models."""
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field, HttpUrl

class ATSPlatform(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    ASHBY = "ashby"
    ICIMS = "icims"
    TALEO = "taleo"
    SMARTRECRUITERS = "smartrecruiters"
    JOBVITE = "jobvite"
    BAMBOOHR = "bamboohr"
    BREEZYHR = "breezyhr"
    JAZZ = "jazz"
    EIGHTFOLD = "eightfold"
    UNKNOWN = "unknown"

class JobDescription(BaseModel):
    url: HttpUrl
    ats_platform: ATSPlatform
    company_name: str
    job_title: str
    location: Optional[str] = None
    salary_range: Optional[str] = None
    job_type: Optional[str] = None  # "Full-time", "Part-time", "Contract", etc.
    seniority_level: Optional[str] = None  # "Entry", "Mid", "Senior", "Staff", "Principal", etc.
    experience_required: Optional[str] = None  # e.g. "5+ years"
    remote_type: Optional[str] = None  # "Remote", "Hybrid", "On-site"
    posting_date: Optional[str] = None
    is_likely_active: Optional[bool] = True
    freshness_warning: Optional[str] = None
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    raw_text: str

class BaseResume(BaseModel):
    full_name: str
    email: str
    phone: str
    location: str
    linkedin: Optional[str] = None
    summary: str
    skills: list[str] = Field(default_factory=list)
    experience: list[dict] = Field(default_factory=list)
    education: list[dict] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

class TailoredResume(BaseModel):
    job_url: HttpUrl
    company_name: str
    job_title: str
    summary: str
    skills_section: str
    experience_section: str
    achievements_section: str
    education_section: str
    full_text: str
    keywords_used: list[str] = Field(default_factory=list)
    match_score: float = Field(ge=0, le=100)

class CoverLetter(BaseModel):
    job_url: HttpUrl
    company_name: str
    job_title: str
    content: str
    word_count: int

class JobViability(BaseModel):
    overall_recommendation: str = "Good match"  # "Good match" / "Apply with caution" / "Not recommended"
    match_score: float = Field(default=50.0, ge=0, le=100)
    viability_notes: list[str] = Field(default_factory=list)
    salary_match: Optional[str] = None
    location_match: Optional[str] = None
    sponsorship_match: Optional[str] = None
    freshness_assessment: Optional[str] = None

class ApplicationStatus(str, Enum):
    PENDING = "pending"
    JD_FETCHED = "jd_fetched"
    RESUME_GENERATED = "resume_generated"
    PDF_GENERATED = "pdf_generated"
    FORM_OPENED = "form_opened"
    FORM_FILLED = "form_filled"
    SUBMITTED = "submitted"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"

class Application(BaseModel):
    id: str
    job_url: HttpUrl
    status: ApplicationStatus = ApplicationStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.now)
    job_description: Optional[JobDescription] = None
    job_viability: Optional[JobViability] = None
    tailored_resume: Optional[TailoredResume] = None
    cover_letter: Optional[CoverLetter] = None
    output_dir: Optional[Path] = None
    resume_filename: Optional[str] = None
    cover_letter_filename: Optional[str] = None
    error_message: Optional[str] = None
    # Tracking fields for auto-apply and follow-up
    applied_at: Optional[datetime] = None
    notes: Optional[str] = None
    email_sent: bool = False
    cached: bool = False  # True if documents were loaded from cache
    next_steps: list[str] = Field(default_factory=list)
    follow_up_date: Optional[str] = None
