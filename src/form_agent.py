"""LLM-powered form analysis and intelligent field mapping agent."""

import json
import re
from typing import Dict, List, Optional, Any
from .config import settings


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
        """Map a form field to the appropriate value from personal info."""
        label_lower = field_label.lower()

        # Direct mappings
        mappings = {
            # Name fields
            'first name': personal_info.get('first_name', ''),
            'first': personal_info.get('first_name', ''),
            'given name': personal_info.get('first_name', ''),
            'last name': personal_info.get('last_name', ''),
            'last': personal_info.get('last_name', ''),
            'family name': personal_info.get('last_name', ''),
            'surname': personal_info.get('last_name', ''),
            'full name': f"{personal_info.get('first_name', '')} {personal_info.get('last_name', '')}".strip(),
            'name': f"{personal_info.get('first_name', '')} {personal_info.get('last_name', '')}".strip(),
            'preferred name': personal_info.get('preferred_name', personal_info.get('first_name', '')),

            # Contact
            'email': personal_info.get('email', ''),
            'e-mail': personal_info.get('email', ''),
            'email address': personal_info.get('email', ''),
            'confirm email': personal_info.get('email', ''),
            'confirm your email': personal_info.get('email', ''),
            'phone': personal_info.get('phone', ''),
            'phone number': personal_info.get('phone', ''),
            'mobile': personal_info.get('phone', ''),
            'mobile phone': personal_info.get('phone', ''),
            'cell': personal_info.get('phone', ''),
            'telephone': personal_info.get('phone', ''),

            # Address
            'address': personal_info.get('address', ''),
            'street': personal_info.get('address', ''),
            'street address': personal_info.get('address', ''),
            'address line 1': personal_info.get('address', ''),
            'address line 2': personal_info.get('address_line_2', ''),
            'city': personal_info.get('city', ''),
            'state': personal_info.get('state', ''),
            'province': personal_info.get('state', ''),
            'zip': personal_info.get('zip_code', ''),
            'zip code': personal_info.get('zip_code', ''),
            'postal code': personal_info.get('zip_code', ''),
            'zipcode': personal_info.get('zip_code', ''),
            'country': personal_info.get('country', 'United States'),

            # Professional
            'linkedin': personal_info.get('linkedin', ''),
            'linkedin url': personal_info.get('linkedin', ''),
            'linkedin profile': personal_info.get('linkedin', ''),
            'github': personal_info.get('github', ''),
            'github url': personal_info.get('github', ''),
            'portfolio': personal_info.get('portfolio', ''),
            'website': personal_info.get('website', ''),
            'personal website': personal_info.get('website', ''),

            # Work
            'current company': personal_info.get('current_company', ''),
            'current employer': personal_info.get('current_company', ''),
            'current title': personal_info.get('current_title', ''),
            'current position': personal_info.get('current_title', ''),
            'years of experience': personal_info.get('years_experience', ''),
            'experience': personal_info.get('years_experience', ''),

            # Authorization
            'work authorization': personal_info.get('work_authorization', 'US Citizen'),
            'authorized to work': personal_info.get('legally_authorized', 'Yes'),
            'legally authorized': personal_info.get('legally_authorized', 'Yes'),
            'sponsorship': personal_info.get('requires_sponsorship', 'No'),
            'require sponsorship': personal_info.get('requires_sponsorship', 'No'),
            'visa sponsorship': personal_info.get('requires_sponsorship', 'No'),

            # Preferences
            'salary': personal_info.get('expected_salary_min', ''),
            'salary expectation': personal_info.get('expected_salary_min', ''),
            'expected salary': personal_info.get('expected_salary_min', ''),
            'desired salary': personal_info.get('expected_salary_min', ''),
            'willing to relocate': personal_info.get('willing_to_relocate', 'Yes'),
            'relocate': personal_info.get('willing_to_relocate', 'Yes'),
            'remote': personal_info.get('remote_preference', 'Flexible'),
            'start date': personal_info.get('available_start', 'Immediately'),
            'available': personal_info.get('available_start', 'Immediately'),
            'notice period': personal_info.get('notice_period', '2 weeks'),

            # Education
            'education': personal_info.get('highest_education', "Bachelor's Degree"),
            'highest education': personal_info.get('highest_education', "Bachelor's Degree"),
            'degree': personal_info.get('highest_education', "Bachelor's Degree"),

            # Source
            'how did you hear': personal_info.get('how_did_you_hear', 'LinkedIn'),
            'source': personal_info.get('how_did_you_hear', 'LinkedIn'),
            'referral': personal_info.get('referral_name', ''),

            # Demographics (EEOC)
            'gender': personal_info.get('gender', 'Decline to self-identify'),
            'race': personal_info.get('race_ethnicity', 'Decline to self-identify'),
            'ethnicity': personal_info.get('race_ethnicity', 'Decline to self-identify'),
            'veteran': personal_info.get('veteran_status', 'Decline to self-identify'),
            'disability': personal_info.get('disability_status', 'Decline to self-identify'),

            # Consent
            'privacy': personal_info.get('privacy_agreement', 'Yes'),
            'privacy agreement': personal_info.get('privacy_agreement', 'Yes'),
            'privacy policy': personal_info.get('privacy_agreement', 'Yes'),
            'consent': personal_info.get('data_processing_consent', 'Yes'),
            'background check': personal_info.get('background_check_consent', 'Yes'),
            'agree': 'Yes',
            'i agree': 'Yes',
            'accept': 'Yes',
        }

        # Check for direct match
        for key, value in mappings.items():
            if key in label_lower:
                return value

        # Check for partial matches
        if 'name' in label_lower and 'first' in label_lower:
            return personal_info.get('first_name', '')
        if 'name' in label_lower and 'last' in label_lower:
            return personal_info.get('last_name', '')
        if 'email' in label_lower:
            return personal_info.get('email', '')
        if 'phone' in label_lower or 'tel' in label_lower:
            return personal_info.get('phone', '')

        return None

    async def get_field_value_with_llm(
        self,
        field_label: str,
        field_type: str,
        field_options: List[str],
        personal_info: Dict,
        resume_text: str
    ) -> Optional[str]:
        """Use LLM to determine the best value for a complex field."""
        prompt = f"""Given this job application form field, determine the best value to fill in.

Field Label: {field_label}
Field Type: {field_type}
{f"Options: {field_options}" if field_options else ""}

Personal Info:
{json.dumps(personal_info, indent=2)}

Resume Summary:
{resume_text[:2000]}

Return ONLY the value to fill in, nothing else. If it's a select field, return the exact option text.
If you can't determine a value, return "SKIP"."""

        try:
            value = self._call_llm(prompt, max_tokens=200).strip()
            if value == "SKIP":
                return None
            return value
        except Exception:
            return None

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
    ) -> str:
        """Generate an answer for a custom application question using LLM."""
        prompt = f"""Answer this job application question professionally and concisely.

Question: {question}

Personal Info:
- Name: {personal_info.get('first_name', '')} {personal_info.get('last_name', '')}
- Current Role: {personal_info.get('current_title', '')} at {personal_info.get('current_company', '')}
- Experience: {personal_info.get('years_experience', '')} years

Resume:
{resume_text[:3000]}

Job Description:
{job_description[:2000]}

Write a professional, concise answer (2-3 sentences max). Be specific and relevant to the role."""

        try:
            return self._call_llm(prompt, max_tokens=300).strip()
        except Exception as e:
            return f"Please see my resume for details regarding {question.lower()}"
