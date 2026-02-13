"""Job analysis: AI metadata extraction and deterministic viability assessment."""
import json
import re
from datetime import datetime, timedelta

import anthropic

from .config import settings
from .models import Application, JobDescription, JobViability


class JobAnalyzer:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def extract_metadata(self, job: JobDescription) -> JobDescription:
        """Use AI to extract structured metadata from raw job text."""
        # Include any pre-populated fields so AI can refine them
        pre_populated = []
        if job.location:
            pre_populated.append(f"- Location (pre-extracted): {job.location}")
        if job.salary_range:
            pre_populated.append(f"- Salary (pre-extracted): {job.salary_range}")
        if job.posting_date:
            pre_populated.append(f"- Posting Date (pre-extracted): {job.posting_date}")
        if job.job_type:
            pre_populated.append(f"- Job Type (pre-extracted): {job.job_type}")
        pre_str = "\n".join(pre_populated) if pre_populated else "None"

        today = datetime.now().strftime("%Y-%m-%d")
        prompt = f"""Analyze this job posting and extract structured metadata. Return ONLY valid JSON.

Today's date is {today}.

JOB POSTING TEXT:
{job.raw_text[:12000]}

CURRENT PARSED VALUES (may be wrong from HTML scraping — correct if needed):
- Company Name: {job.company_name}
- Job Title: {job.job_title}

PRE-EXTRACTED STRUCTURED DATA (from JSON-LD / API — generally reliable but refine if needed):
{pre_str}

Extract and return this JSON object:
{{
  "company_name": "Corrected company name (clean up codes like '2100 NVIDIA USA' to just 'NVIDIA')",
  "job_title": "Corrected job title if the scraped one is wrong, otherwise keep as-is",
  "location": "City, State format (e.g. 'Santa Clara, CA'). Clean up raw formats like 'US, CA, Santa Clara'",
  "salary_range": "Full salary text as stated in posting, including all levels if multiple. e.g. '$184,000 - $287,500 (L4) / $224,000 - $356,500 (L5)'. null if not mentioned.",
  "job_type": "Full-time / Part-time / Contract / Internship",
  "seniority_level": "Entry / Mid / Senior / Staff / Principal / Lead / Director / VP",
  "experience_required": "e.g. '8+ years' or null if not mentioned",
  "remote_type": "Remote / Hybrid / On-site. Infer from context if not explicit.",
  "posting_date": "ISO date string if mentioned, null otherwise",
  "is_likely_active": true,
  "freshness_warning": "Warning string if job seems old/closed/suspicious, null if looks fine",
  "responsibilities": ["key responsibility 1", "key responsibility 2", ...],
  "required_skills": ["skill 1", "skill 2", ...],
  "preferred_skills": ["nice-to-have skill 1", ...],
  "tech_stack": ["technology 1", "technology 2", ...],
  "education": "Required education level, e.g. 'B.Sc. or M.Sc. in CS or equivalent'",
  "benefits_highlights": ["Notable benefit 1", "Notable benefit 2", ...]
}}

Rules:
- Extract ALL information explicitly stated or strongly implied in the posting.
- For salary, capture the COMPLETE range text including all levels/bands if multiple are listed.
- For location, reformat to clean 'City, State' format (e.g. 'US, CA, Santa Clara' → 'Santa Clara, CA').
- For tech_stack, list ALL specific technologies, tools, frameworks, and platforms mentioned.
- For seniority, infer from title and experience requirements if not explicit.
- Set is_likely_active to false if there are signs the posting is old/closed.
- Return ONLY the JSON object, no markdown fences."""

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = msg.content[0].text
        if "```json" in txt:
            txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt:
            txt = txt.split("```")[1].split("```")[0]

        try:
            d = json.loads(txt.strip())
        except (json.JSONDecodeError, ValueError):
            # AI response wasn't valid JSON — return job unchanged
            return job

        # Update job fields from AI extraction (AI overrides pre-populated values
        # since it can clean/refine them, e.g. fixing location format)
        if d.get("company_name"):
            job.company_name = d["company_name"]
        if d.get("job_title"):
            job.job_title = d["job_title"]
        if d.get("location"):
            job.location = d["location"]
        if d.get("salary_range"):
            job.salary_range = d["salary_range"]
        if d.get("job_type"):
            job.job_type = d["job_type"]
        if d.get("seniority_level"):
            job.seniority_level = d["seniority_level"]
        if d.get("experience_required"):
            job.experience_required = d["experience_required"]
        if d.get("remote_type"):
            job.remote_type = d["remote_type"]
        if d.get("posting_date"):
            job.posting_date = d["posting_date"]
        if d.get("is_likely_active") is not None:
            job.is_likely_active = d["is_likely_active"]
        if d.get("freshness_warning"):
            job.freshness_warning = d["freshness_warning"]
        if d.get("responsibilities"):
            job.responsibilities = d["responsibilities"]
        if d.get("required_skills"):
            job.required_skills = d["required_skills"]
        if d.get("preferred_skills"):
            job.preferred_skills = d["preferred_skills"]

        # Store additional extracted fields in keywords/technologies
        if d.get("tech_stack"):
            existing = set(job.technologies or [])
            existing.update(d["tech_stack"])
            job.technologies = list(existing)
            job.keywords = list(existing)

        return job

    def assess_viability(self, job: JobDescription, personal_info: dict) -> JobViability:
        """Deterministic viability assessment comparing job metadata to personal preferences."""
        notes = []
        score = 70.0  # baseline
        salary_match = None
        location_match = None
        sponsorship_match = None
        freshness_assessment = None

        # --- Salary check ---
        salary_min = personal_info.get("expected_salary_min", "")
        salary_max = personal_info.get("expected_salary_max", "")
        if job.salary_range and salary_min:
            job_numbers = re.findall(r"[\d,]+", job.salary_range.replace(",", ""))
            job_amounts = [int(n) for n in job_numbers if n.isdigit() and int(n) > 10000]
            try:
                user_min = int(salary_min)
            except (ValueError, TypeError):
                user_min = 0
            try:
                user_max = int(salary_max) if salary_max else user_min
            except (ValueError, TypeError):
                user_max = user_min

            if job_amounts:
                job_high = max(job_amounts)
                job_low = min(job_amounts)
                if job_high < user_min * 0.8:
                    salary_match = f"Below range (job: ${job_low:,}-${job_high:,}, you want ${user_min:,}-${user_max:,})"
                    notes.append(f"Salary may be below your range: job offers up to ${job_high:,}, you expect ${user_min:,}+")
                    score -= 15
                elif job_low >= user_min:
                    salary_match = "Within range"
                    score += 5
                else:
                    salary_match = "Overlapping range"
                    score += 2
        elif not job.salary_range:
            salary_match = "Not listed"
            notes.append("Salary not listed in posting")

        # --- Location / Remote check ---
        remote_pref = personal_info.get("remote_preference", "").lower()
        user_locations = personal_info.get("relocation_locations", "").lower()
        user_city = personal_info.get("city", "").lower()
        user_state = personal_info.get("state", "").lower()
        willing_to_relocate = personal_info.get("willing_to_relocate", "No").lower() == "yes"

        if job.remote_type:
            rt = job.remote_type.lower()
            if "remote" in rt:
                location_match = "Remote — matches any location"
                score += 5
            elif "hybrid" in rt or "on-site" in rt or "onsite" in rt:
                # Check if job location is near user
                job_loc = (job.location or "").lower()
                if job_loc:
                    loc_nearby = any(loc in job_loc for loc in [user_city, user_state] if loc)
                    reloc_match = any(loc in job_loc for loc in user_locations.split() if loc)
                    if loc_nearby:
                        location_match = f"Near you ({job.location})"
                        score += 5
                    elif reloc_match and willing_to_relocate:
                        location_match = f"In preferred relocation area ({job.location})"
                        score += 2
                    elif willing_to_relocate:
                        location_match = f"Relocation required ({job.location})"
                        notes.append(f"Job is {job.remote_type} in {job.location} — relocation may be needed")
                    else:
                        location_match = f"Location mismatch ({job.location})"
                        notes.append(f"Job requires {job.remote_type} in {job.location}, you prefer {remote_pref}")
                        score -= 10
                else:
                    location_match = f"{job.remote_type} — location not specified"
            if remote_pref == "remote" and "remote" not in rt:
                notes.append(f"You prefer remote; this job is {job.remote_type}")
                score -= 5
        elif job.location:
            location_match = job.location
        else:
            location_match = "Not specified"

        # --- Sponsorship check ---
        needs_sponsorship = personal_info.get("requires_sponsorship", "No").lower()
        if "yes" in needs_sponsorship:
            # Check if raw text mentions sponsorship
            raw_lower = job.raw_text.lower()
            no_sponsor_phrases = [
                "unable to sponsor", "not sponsor", "cannot sponsor",
                "no sponsorship", "without sponsorship", "not able to sponsor",
                "will not sponsor", "do not sponsor",
            ]
            if any(phrase in raw_lower for phrase in no_sponsor_phrases):
                sponsorship_match = "Company does not sponsor visas"
                notes.append("Job posting states they do not provide visa sponsorship")
                score -= 25
            else:
                sponsorship_match = "No sponsorship restriction mentioned"
        else:
            sponsorship_match = "Not needed"

        # --- Freshness check ---
        if job.freshness_warning:
            freshness_assessment = job.freshness_warning
            notes.append(f"Freshness concern: {job.freshness_warning}")
            score -= 5
        elif job.is_likely_active is False:
            freshness_assessment = "Job may no longer be active"
            notes.append("This job posting may no longer be active")
            score -= 10
        elif job.posting_date:
            try:
                post_dt = datetime.fromisoformat(job.posting_date.replace("Z", "+00:00"))
                age = datetime.now(post_dt.tzinfo) - post_dt if post_dt.tzinfo else datetime.now() - post_dt
                if age > timedelta(days=60):
                    freshness_assessment = f"Posted {age.days} days ago — may be stale"
                    notes.append(f"Job was posted {age.days} days ago")
                    score -= 5
                elif age > timedelta(days=30):
                    freshness_assessment = f"Posted {age.days} days ago"
                else:
                    freshness_assessment = "Recent posting"
                    score += 3
            except (ValueError, TypeError):
                freshness_assessment = "Posting date unclear"
        else:
            freshness_assessment = "Posting date unknown"

        # Clamp score
        score = max(0.0, min(100.0, score))

        # Determine recommendation
        if score >= 65:
            recommendation = "Good match"
        elif score >= 40:
            recommendation = "Apply with caution"
        else:
            recommendation = "Not recommended"

        return JobViability(
            overall_recommendation=recommendation,
            match_score=round(score, 1),
            viability_notes=notes,
            salary_match=salary_match,
            location_match=location_match,
            sponsorship_match=sponsorship_match,
            freshness_assessment=freshness_assessment,
        )

    async def generate_next_steps(self, application: Application) -> list[str]:
        """Generate AI-powered next steps for a job application."""
        jd = application.job_description
        viab = application.job_viability

        # Build context
        context_parts = []
        if jd:
            context_parts.append(f"Company: {jd.company_name}")
            context_parts.append(f"Position: {jd.job_title}")
            if jd.location:
                context_parts.append(f"Location: {jd.location}")
            if jd.technologies:
                context_parts.append(f"Tech Stack: {', '.join(jd.technologies[:10])}")
            if jd.seniority_level:
                context_parts.append(f"Level: {jd.seniority_level}")

        context_parts.append(f"Application Status: {application.status.value}")

        if viab:
            context_parts.append(f"Viability: {viab.overall_recommendation} ({viab.match_score:.0f}%)")
            if viab.viability_notes:
                context_parts.append(f"Notes: {'; '.join(viab.viability_notes[:3])}")

        context = "\n".join(context_parts)

        prompt = f"""Based on this job application, generate 3-5 specific, actionable next steps the candidate should take.

APPLICATION CONTEXT:
{context}

Generate practical next steps such as:
- Follow-up timing and method
- Research tasks (company culture, interview process, team)
- Preparation tasks (technical skills, interview practice)
- Networking suggestions (connecting with employees)
- Portfolio/project ideas related to the role

Return ONLY a JSON array of strings, each string being one actionable next step.
Keep each step concise (1-2 sentences max).
Make steps specific to this company and role, not generic advice.

Example format:
["Follow up via email in 1 week if no response", "Research interview process on Glassdoor", "Practice system design for distributed systems"]"""

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        txt = msg.content[0].text.strip()

        # Parse JSON array
        if "```json" in txt:
            txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt:
            txt = txt.split("```")[1].split("```")[0]

        try:
            steps = json.loads(txt.strip())
            if isinstance(steps, list):
                return [str(s) for s in steps[:5]]
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: return generic steps
        company = jd.company_name if jd else "the company"
        return [
            f"Follow up via email in 1 week if no response from {company}",
            f"Research {company}'s interview process on Glassdoor",
            "Review your resume and prepare to discuss key experiences",
            "Practice common behavioral interview questions",
            f"Connect with {company} employees on LinkedIn for insights"
        ]
