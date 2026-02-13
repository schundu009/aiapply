import json, anthropic
from .config import settings
from .models import BaseResume, CoverLetter, JobDescription, TailoredResume


def _format_experience(experience: list[dict]) -> str:
    """Format parsed experience into a readable string for the prompt."""
    if not experience:
        return "No structured experience data available."
    parts = []
    for job in experience:
        title = job.get("job_title", "")
        company = job.get("company", "")
        dates = job.get("dates", "")
        location = job.get("location", "")
        bullets = job.get("bullets", [])

        # Build header lines
        lines = []
        if title:
            lines.append(title)
        company_date = company
        if dates:
            company_date += f" | {dates}"
        if company_date:
            lines.append(company_date)
        if bullets:
            lines.extend(f"• {b}" for b in bullets)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _format_education(education: list[dict]) -> str:
    """Format parsed education into a readable string."""
    if not education:
        return "Not provided."
    return "\n".join(e.get("text", "") for e in education)


class ResumeGenerator:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model

    async def generate_tailored_resume(self, job: JobDescription, base: BaseResume,
                                        personal_info: dict = None) -> TailoredResume:
        experience_text = _format_experience(base.experience)
        education_text = _format_education(base.education)
        certs_text = ", ".join(base.certifications) if base.certifications else "Not provided."

        personal_context = ""
        if personal_info:
            personal_context = f"""
CANDIDATE PERSONAL INFO:
- Current Title: {personal_info.get('current_title', 'N/A')}
- Years of Experience: {personal_info.get('years_experience', 'N/A')}
- Desired Salary: {personal_info.get('desired_salary', 'N/A')}
- Work Authorization: {personal_info.get('work_authorization', 'N/A')}
- Visa Sponsorship Needed: {personal_info.get('sponsorship', 'N/A')}
- Willing to Relocate: {personal_info.get('willing_to_relocate', 'N/A')}
- Preferred Locations: {personal_info.get('preferred_locations', 'N/A')}
- Start Date: {personal_info.get('start_date', 'N/A')}
"""

        prompt = f"""You are an expert resume writer. Generate a highly tailored, ATS-optimized resume for the following job posting.

IMPORTANT RULES:
1. The resume MUST be specifically tailored to {job.company_name} and the {job.job_title} role.
2. Use the candidate's REAL experience below - do NOT invent experience or companies.
3. Rewrite bullet points to emphasize skills and achievements that match THIS specific job's requirements.
4. Include the candidate's actual company names, job titles, and date ranges from their experience.
5. Highlight achievements with quantifiable metrics where available.
6. Mirror the keywords and technologies from the job posting where the candidate has genuine experience.

=== TARGET COMPANY AND ROLE ===
Company: {job.company_name}
Job Title: {job.job_title}
Job Keywords: {", ".join(job.keywords)}

=== FULL JOB DESCRIPTION ===
{job.raw_text[:8000]}

=== CANDIDATE INFORMATION ===
Name: {base.full_name}
Location: {base.location}
Email: {base.email}
Phone: {base.phone}
LinkedIn: {base.linkedin or 'N/A'}
{personal_context}
=== CANDIDATE'S PROFESSIONAL SUMMARY ===
{base.summary}

=== CANDIDATE'S ACTUAL WORK EXPERIENCE ===
{experience_text}

=== CANDIDATE'S SKILLS ===
{", ".join(base.skills)}

=== CANDIDATE'S EDUCATION ===
{education_text}

=== CANDIDATE'S CERTIFICATIONS ===
{certs_text}

=== OUTPUT INSTRUCTIONS ===
Generate a JSON object with these fields:
- "summary": A compelling 3-4 sentence professional summary tailored to {job.company_name}'s {job.job_title} role. Reference specific requirements from the job posting.
- "skills_section": Technical skills organized by category, prioritizing skills mentioned in the job posting. Format as "Category: skill1, skill2, skill3" with newlines between categories.
- "experience_section": The candidate's REAL work experience, rewritten to emphasize relevance to this role. MUST include actual company names, titles, locations, and dates. Format each job as:
  "Job Title\\nCompany Name, Location | Dates\\n• Achievement bullet 1\\n• Achievement bullet 2..."
- "achievements_section": 4-6 key achievements from the candidate's experience that best demonstrate fit for this role. Use quantifiable metrics.
- "education_section": Education and certifications from the candidate's actual background.
- "full_text": The complete resume as formatted plain text (this will be saved as resume.txt).
- "keywords_used": Array of job-posting keywords that appear in the tailored resume.
- "match_score": Honest assessment (0-100) of how well this candidate matches the role.

Return ONLY valid JSON, no markdown code blocks."""

        msg = self.client.messages.create(model=self.model, max_tokens=4096,
            messages=[{"role": "user", "content": prompt}])
        txt = msg.content[0].text
        if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
        elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
        try:
            d = json.loads(txt.strip())
        except:
            d = {"summary": "", "skills_section": "", "experience_section": "",
                 "achievements_section": "", "education_section": "", "full_text": txt,
                 "keywords_used": job.keywords[:10], "match_score": 70}

        # Normalize list fields to newline-joined strings (Claude sometimes returns lists)
        for field in ("summary", "skills_section", "experience_section",
                       "achievements_section", "education_section", "full_text"):
            if isinstance(d.get(field), list):
                d[field] = "\n".join(str(item) for item in d[field])

        return TailoredResume(job_url=job.url, company_name=job.company_name, job_title=job.job_title, **d)

    async def generate_cover_letter(self, job: JobDescription, base: BaseResume,
                                     resume: TailoredResume, personal_info: dict = None) -> CoverLetter:
        experience_text = _format_experience(base.experience)

        prompt = f"""You are an expert cover letter writer. Write a compelling, personalized cover letter for the following application.

IMPORTANT RULES:
1. Address the letter to {job.company_name} specifically - mention the company by name multiple times.
2. Reference the specific {job.job_title} role and its key requirements.
3. Draw from the candidate's REAL experience to demonstrate fit - use actual company names and achievements.
4. Show genuine knowledge of what {job.company_name} does and why the candidate wants to work there.
5. Be specific, not generic. Every paragraph should reference either the company or the candidate's real experience.

=== TARGET COMPANY AND ROLE ===
Company: {job.company_name}
Job Title: {job.job_title}

=== JOB DESCRIPTION ===
{job.raw_text[:6000]}

=== CANDIDATE ===
Name: {base.full_name}
Location: {base.location}
Current Summary: {base.summary}

=== CANDIDATE'S REAL EXPERIENCE ===
{experience_text}

=== TAILORED RESUME SUMMARY (for consistency) ===
{resume.summary}

=== KEY ACHIEVEMENTS TO HIGHLIGHT ===
{resume.achievements_section}

Write 3-4 paragraphs. Be specific about:
- Why {job.company_name} specifically (not just any company)
- How the candidate's experience at their previous companies directly relates to this role's requirements
- Specific technical skills and achievements that match the job posting
- Genuine enthusiasm backed by concrete examples

Output plain text only, no markdown formatting."""

        msg = self.client.messages.create(model=self.model, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        content = msg.content[0].text.strip()
        return CoverLetter(job_url=job.url, company_name=job.company_name,
            job_title=job.job_title, content=content, word_count=len(content.split()))
