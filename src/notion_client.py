import re
from typing import Optional
from .models import BaseResume

SECTION_HEADERS = {
    "summary": ["professional summary", "summary", "objective", "profile"],
    "skills": ["technical expertise", "skills", "core competencies", "technical skills", "expertise"],
    "experience": ["professional experience", "experience", "work experience", "employment history"],
    "education": ["education"],
    "certifications": ["certifications", "certificates", "licenses"],
}

def _detect_section(line: str) -> Optional[str]:
    """Detect which section a header line belongs to."""
    lower = line.lower().strip()
    lower = re.sub(r'^[#=\-*]+\s*', '', lower)
    lower = re.sub(r'\s*[#=\-*]+$', '', lower)
    for section, keywords in SECTION_HEADERS.items():
        for kw in keywords:
            if kw in lower and len(lower) < 60:
                return section
    return None

def _has_date_range(line: str) -> bool:
    """Check if line contains a date range like 'Month Year - Month Year'."""
    return bool(re.search(
        r'(\w+\s+\d{4})\s*[-–]\s*(\w+\s+\d{4}|Present|Current)',
        line, re.I
    ))

def _is_company_date_line(line: str) -> bool:
    """Check if a line is a company/dates line (has dates and/or pipe separator)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("•", "-", "*", "–")):
        return False
    return _has_date_range(stripped)

def _looks_like_job_title(line: str) -> bool:
    """Check if a line looks like a job title (not a bullet, not a date line, reasonable length)."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("•", "-", "*", "–")):
        return False
    if _has_date_range(stripped):
        return False
    if len(stripped) > 80 or len(stripped) < 5:
        return False
    # Reject lines starting with common resume action verbs (these are bullet points)
    action_verbs = r'^(Led|Designed|Built|Architected|Developed|Implemented|Managed|Created|Deployed|Reduced|Achieved|Delivered|Engineered|Established|Modernized|Automated|Optimized|Configured|Maintained|Migrated|Spearheaded|Drove|Secured|Integrated|Streamlined|Oversaw|Directed|Transformed|Coordinated|Executed|Introduced|Pioneered|Provided|Supported|Mentored|Conducted|Facilitated|Collaborated|Ensured|Improved|Enhanced|Scaled|Progressive|Key)\b'
    if re.match(action_verbs, stripped, re.I):
        return False
    # Job titles often contain these role words
    title_words = r'\b(engineer|architect|developer|manager|director|lead|senior|principal|specialist|administrator|consultant|analyst|devops|sre|software|staff|vp|head)\b'
    if re.search(title_words, stripped, re.I):
        return True
    return False


def parse_resume_from_text(text: str) -> BaseResume:
    lines = text.strip().split("\n")
    name = lines[0].strip() if lines else "Unknown"

    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    email = email.group() if email else ""
    phone = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
    phone = phone.group() if phone else ""
    loc = re.search(r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})", text)
    loc = loc.group() if loc else ""
    linkedin = re.search(r"linkedin\.com/in/[\w-]+", text, re.I)
    linkedin = linkedin.group() if linkedin else ""

    summary = ""
    skills = []
    experience = []
    education = []
    certifications = []

    current_section = None
    current_job = None
    current_bullets = []
    pending_title = None  # Buffer for job title that appears before company/date line

    for line in lines[1:]:  # Skip the name line
        stripped = line.strip()

        # Detect section headers
        detected = _detect_section(stripped)
        if detected:
            # Save any pending job before switching sections
            if current_section == "experience" and current_job:
                current_job["bullets"] = current_bullets
                experience.append(current_job)
                current_job = None
                current_bullets = []
                pending_title = None
            current_section = detected
            continue

        if not stripped:
            continue

        if current_section == "summary":
            summary += stripped + " "

        elif current_section == "skills":
            clean = re.sub(r'^[•\-*]\s*', '', stripped)
            if ":" in clean:
                parts = clean.split(":", 1)
                skill_items = [s.strip() for s in re.split(r"[,;|]", parts[1]) if s.strip()]
                skills.extend(skill_items)
            else:
                skill_items = [s.strip() for s in re.split(r"[,;|]", clean) if s.strip()]
                skills.extend(skill_items)

        elif current_section == "experience":
            if stripped.startswith(("•", "-", "*", "–")):
                # Explicit bullet point
                bullet = re.sub(r'^[•\-*–]\s*', '', stripped)
                current_bullets.append(bullet)
                # If there was a pending title, it was actually a bullet too
                if pending_title:
                    current_bullets.insert(len(current_bullets) - 1, pending_title)
                    pending_title = None
            elif _is_company_date_line(stripped):
                # This is a company/dates line - save previous job first
                if current_job:
                    current_job["bullets"] = current_bullets
                    experience.append(current_job)
                    current_bullets = []

                # Extract dates
                date_match = re.search(
                    r'(\w+\s+\d{4})\s*[-–]\s*(\w+\s+\d{4}|Present|Current)',
                    stripped, re.I
                )
                dates = date_match.group(0) if date_match else ""

                # Extract location — support both "City, ST" and "• City, ST •" formats
                location = ""
                loc_match = re.search(r'[,•]\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*,\s*[A-Z]{2})', stripped)
                if loc_match:
                    location = loc_match.group(1)
                elif re.search(r'\bRemote\b', stripped, re.I):
                    location = "Remote"

                # Extract company (everything before the pipe/dates/bullet separators)
                company_part = stripped
                if "|" in company_part:
                    company_part = company_part.split("|")[0].strip()
                elif "•" in company_part:
                    company_part = company_part.split("•")[0].strip()
                elif dates:
                    company_part = company_part.replace(dates, "").strip().rstrip("|").strip()
                # Clean trailing separators
                company_part = company_part.rstrip("•|, ").strip()

                current_job = {
                    "job_title": pending_title or "",
                    "company": company_part,
                    "dates": dates,
                    "location": location,
                    "raw": stripped,
                    "bullets": [],
                }
                pending_title = None
            elif _looks_like_job_title(stripped):
                # This might be a job title line (appears before company/date line)
                # If there was an unconsumed pending title, it was actually a bullet
                if pending_title and current_job:
                    current_bullets.append(pending_title)
                pending_title = stripped
            elif current_job is not None:
                # Non-prefixed bullet/achievement line (common in DOCX exports)
                if pending_title:
                    current_bullets.append(pending_title)
                    pending_title = None
                current_bullets.append(stripped)
            else:
                # Before first job — treat as a pending title for the next position
                if pending_title and current_job is None:
                    pass  # overwrite; only the last pre-company line is the title
                pending_title = stripped

        elif current_section == "education":
            education.append({"text": stripped})

        elif current_section == "certifications":
            clean = re.sub(r'^[•\-*]\s*', '', stripped)
            certs = [c.strip() for c in re.split(r'[|]', clean) if c.strip()]
            certifications.extend(certs)

    # Save last job if still pending
    if current_section == "experience" and current_job:
        current_job["bullets"] = current_bullets
        experience.append(current_job)

    return BaseResume(
        full_name=name, email=email, phone=phone, location=loc,
        linkedin=linkedin, summary=summary.strip(), skills=skills[:50],
        experience=experience, education=education, certifications=certifications
    )
