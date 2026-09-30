"""LLM-powered form analysis and intelligent field mapping agent."""

import json
import re
from typing import Dict, List, Optional, Any
from .config import settings
from .profile_store import strip_credentials

# Field types the LLM may draft answers for.
LLM_FREE_TEXT_TYPES = frozenset({"text", "textarea"})

# Profile fields that may be shared with the LLM. Contact details, addresses,
# credentials and every factual/legal/EEO field are deliberately excluded.
LLM_SAFE_PROFILE_FIELDS = (
    "first_name", "last_name", "preferred_name", "current_title",
    "current_company", "years_experience", "highest_education",
    "linkedin", "github", "portfolio", "website",
)


def llm_safe_profile(personal_info: Optional[Dict]) -> Dict[str, str]:
    info = strip_credentials(personal_info or {})
    return {k: str(info[k]).strip() for k in LLM_SAFE_PROFILE_FIELDS if info.get(k)}


class FormAgent:
    """Uses LLM to intelligently analyze and fill job application forms."""

    def __init__(self, provider: str = "auto"):
        """
        Initialize FormAgent with specified LLM provider.

        Args:
            provider: 'anthropic', 'openai', or 'auto' (tries anthropic first, then openai)
        """
        self.provider = provider
        self.client = None
        self.model = None
        self._initialize_client()

    def _initialize_client(self):
        """Initialize the LLM client based on available API keys."""
        if self.provider == "auto":
            # Try Anthropic first, then OpenAI
            if settings.anthropic_api_key and settings.anthropic_api_key != "your_key_here":
                self._init_anthropic()
            elif settings.openai_api_key and settings.openai_api_key != "your_key_here":
                self._init_openai()
            else:
                raise ValueError("No valid API key found. Please set ANTHROPIC_API_KEY or OPENAI_API_KEY.")
        elif self.provider == "anthropic":
            self._init_anthropic()
        elif self.provider == "openai":
            self._init_openai()
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    def _init_anthropic(self):
        """Initialize Anthropic client."""
        try:
            from anthropic import Anthropic
        except ImportError:
            raise ValueError("anthropic package not installed. Run: pip install anthropic")
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.anthropic_model
        self.provider = "anthropic"

    def _init_openai(self):
        """Initialize OpenAI client."""
        try:
            from openai import OpenAI
        except ImportError:
            raise ValueError("openai package not installed. Run: pip install openai")
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = getattr(settings, 'openai_model', 'gpt-4o')
        self.provider = "openai"

    def _call_llm(self, prompt: str, max_tokens: int = 2000) -> str:
        """Call the LLM with the given prompt."""
        if self.provider == "anthropic":
            response = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text
        elif self.provider == "openai":
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.choices[0].message.content
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    async def analyze_form_fields(self, page_html: str, page_text: str) -> Dict[str, Any]:
        """Analyze form fields on a page and identify what data is needed."""
        prompt = f"""Analyze this job application form and identify all input fields that need to be filled.

Page text:
{page_text[:8000]}

For each field, identify:
1. Field name/label
2. Field type (text, email, phone, select, checkbox, file, textarea)
3. Whether it's required
4. What personal info category it maps to (first_name, last_name, email, phone, address, city, state, zip_code, country, linkedin, github, work_authorization, etc.)

Return a JSON array of fields:
```json
[
  {{"label": "First Name", "type": "text", "required": true, "maps_to": "first_name"}},
  {{"label": "Email", "type": "email", "required": true, "maps_to": "email"}}
]
```

Only return the JSON array, no other text."""

        try:
            text = self._call_llm(prompt, max_tokens=2000)
            # Extract JSON from response
            json_match = re.search(r'\[[\s\S]*\]', text)
            if json_match:
                return {"fields": json.loads(json_match.group())}
            return {"fields": []}
        except Exception as e:
            return {"error": str(e), "fields": []}

    def map_field_to_value(self, field_label: str, field_type: str, personal_info: Dict) -> Optional[str]:
        """Map a form field to a value the user saved in their profile.

        There are no fallback answers: if the user has not saved a value the
        field is left blank (and reported as missing when required).
        """
        label_lower = field_label.lower()
        info = strip_credentials(personal_info)

        def v(key: str) -> str:
            value = info.get(key, '')
            return str(value).strip() if value is not None else ''

        # Factual / legal / consent / EEO questions: saved profile values only.
        # Checked first so e.g. "Have you previously worked here?" never falls
        # through to a generic "work" or "name" match.
        factual_key = self._factual_profile_key(label_lower)
        if factual_key is not None:
            return v(factual_key) or None

        full_name = f"{v('first_name')} {v('last_name')}".strip()
        mappings = {
            # Name fields
            'first name': v('first_name'),
            'given name': v('first_name'),
            'last name': v('last_name'),
            'family name': v('last_name'),
            'surname': v('last_name'),
            'full name': full_name,
            'preferred name': v('preferred_name') or v('first_name'),

            # Contact
            'email': v('email'),
            'e-mail': v('email'),
            'phone': v('phone'),
            'mobile': v('phone'),
            'cell': v('phone'),
            'telephone': v('phone'),

            # Address
            'address line 2': v('address_line_2'),
            'address': v('address'),
            'street': v('address'),
            'city': v('city'),
            'state': v('state'),
            'province': v('state'),
            'zip': v('zip_code'),
            'postal code': v('zip_code'),
            'country': v('country'),

            # Professional links
            'linkedin': v('linkedin'),
            'github': v('github'),
            'portfolio': v('portfolio'),
            'website': v('website'),

            # Work
            'current company': v('current_company'),
            'current employer': v('current_company'),
            'current title': v('current_title'),
            'current position': v('current_title'),
            'years of experience': v('years_experience'),

            # Preferences the user saved
            'salary': v('expected_salary_min') or v('desired_salary'),
            'start date': v('available_start'),
            'notice period': v('notice_period'),
            'highest education': v('highest_education'),
            'degree': v('highest_education'),
            'referral': v('referral_name'),
        }

        for key, value in mappings.items():
            if key in label_lower:
                return value or None

        if 'name' in label_lower and 'first' in label_lower:
            return v('first_name') or None
        if 'name' in label_lower and 'last' in label_lower:
            return v('last_name') or None
        if label_lower.strip() in ('name', 'your name'):
            return full_name or None

        return None

    # ------------------------------------------------------------------
    # Factual-question detection
    # ------------------------------------------------------------------

    # (keyword, profile key) - first match wins.
    _FACTUAL_PROFILE_KEYS = (
        ('sponsor', 'requires_sponsorship'),
        ('visa', 'requires_sponsorship'),
        ('legally authorized', 'legally_authorized'),
        ('authorized to work', 'legally_authorized'),
        ('eligible to work', 'legally_authorized'),
        ('work authorization', 'work_authorization'),
        ('citizenship', 'work_authorization'),
        ('relocat', 'willing_to_relocate'),
        ('background check', 'background_check_consent'),
        ('privacy', 'privacy_agreement'),
        ('data processing', 'data_processing_consent'),
        ('how did you hear', 'how_did_you_hear'),
        ('hear about', 'how_did_you_hear'),
        ('previously applied', 'previously_applied'),
        ('applied before', 'previously_applied'),
        ('previously employed', 'previously_employed'),
        ('previously worked', 'previously_employed'),
        ('worked for', 'previously_employed'),
        ('government', 'government_employee'),
        ('non-compete', 'non_compete'),
        ('non compete', 'non_compete'),
        ('gender', 'gender'),
        ('race', 'race_ethnicity'),
        ('ethnic', 'race_ethnicity'),
        ('hispanic', 'race_ethnicity'),
        ('veteran', 'veteran_status'),
        ('disabilit', 'disability_status'),
    )

    # Questions that are statements of fact, legal attestations, consents or
    # EEO self-identification. The LLM must never answer these.
    _FACTUAL_KEYWORDS = (
        'authoriz', 'sponsor', 'visa', 'citizen', 'legally', 'eligib', 'relocat',
        'background', 'consent', 'agree', 'accept', 'acknowledg', 'certif', 'attest',
        'privacy', 'terms', 'policy', 'gender', 'race', 'ethnic', 'hispanic',
        'veteran', 'disabilit', 'pronoun', 'orientation', 'transgender',
        'date of birth', 'birth', 'convict', 'criminal', 'felony', 'clearance',
        'how did you hear', 'hear about', 'source', 'referr', 'previously', 'worked for',
        'employed by', 'related to', 'relative', 'government', 'compete', 'salary',
        'compensation', 'start date', 'notice', 'signature', 'ssn',
        'social security', 'drug', 'travel', 'shift', 'on-site', 'onsite', 'remote',
        'commute', 'located', 'location', 'reside', 'address',
    )

    def _factual_profile_key(self, label_lower: str) -> Optional[str]:
        for keyword, key in self._FACTUAL_PROFILE_KEYS:
            if keyword in label_lower:
                return key
        return None

    _FACTUAL_WORDS_RE = re.compile(r"\b(age|sex|pay|sign|18|21|over 18)\b")

    def is_factual_question(self, field_label: str) -> bool:
        label_lower = (field_label or '').lower()
        return (any(k in label_lower for k in self._FACTUAL_KEYWORDS)
                or bool(self._FACTUAL_WORDS_RE.search(label_lower)))

    async def get_field_value_with_llm(
        self,
        field_label: str,
        field_type: str,
        field_options: List[str],
        personal_info: Dict,
        resume_text: str,
        job_description: str = "",
    ) -> Optional[str]:
        """Draft an answer for a free-text question (text/textarea only).

        Factual, legal, consent, EEO and choice (select/radio/checkbox)
        questions are never sent to the LLM; they stay blank for the user.
        Only non-sensitive profile fields and the resume are shared.
        """
        if (field_type or 'text').lower() not in LLM_FREE_TEXT_TYPES:
            return None
        if field_options:
            return None
        if self.is_factual_question(field_label):
            return None
        return await self.generate_answer_for_question(
            field_label, personal_info, resume_text, job_description
        )

    def identify_missing_fields(
        self,
        required_fields: List[Dict],
        personal_info: Dict
    ) -> List[Dict]:
        """Identify which required fields cannot be filled with current personal info."""
        missing = []
        for field in required_fields:
            if not field.get('required'):
                continue

            value = self.map_field_to_value(
                field.get('label', ''),
                field.get('type', 'text'),
                personal_info
            )

            if not value:
                missing.append({
                    'label': field.get('label'),
                    'type': field.get('type'),
                    'suggested_key': field.get('maps_to', field.get('label', '').lower().replace(' ', '_'))
                })

        return missing

    async def generate_answer_for_question(
        self,
        question: str,
        personal_info: Dict,
        resume_text: str,
        job_description: str
    ) -> Optional[str]:
        """Draft a short answer to a free-text application question."""
        if self.is_factual_question(question):
            return None
        profile = llm_safe_profile(personal_info)
        profile_lines = "\n".join(f"- {k.replace('_', ' ').title()}: {val}" for k, val in profile.items()) or "- (none)"
        prompt = f"""Draft a short answer to this free-text job application question.

Question: {question}

Candidate background:
{profile_lines}

Resume:
{resume_text[:3000]}

Job Description:
{(job_description or '')[:2000]}

Rules:
- Base the answer only on the resume and background above; do not invent facts.
- Do not make statements about work authorization, visa or sponsorship status,
  relocation, availability, salary, consent, or any demographic information.
- 2-3 sentences max. If the question cannot be answered from the resume, reply exactly SKIP."""

        try:
            answer = self._call_llm(prompt, max_tokens=300).strip()
        except Exception:
            return None
        if not answer or answer.upper() == "SKIP":
            return None
        return answer
