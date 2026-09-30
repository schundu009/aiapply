"""Profile (personal_info) persistence without credentials.

AutoApply never stores or uses passwords for job sites, Google, LinkedIn or
any ATS. This module strips every credential-like field on read and on write,
and scrubs legacy files that still contain them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("autoapply.profile")

# Legacy files that may hold profile data (the app used the repo root; some
# deployments used data/).
PROFILE_PATH = Path("personal_info.json")
LEGACY_PROFILE_PATHS = (Path("personal_info.json"), Path("data/personal_info.json"))

# Platforms the old build stored per-site logins for. "<platform>_email" keys
# were login identities only (the real contact email is "email").
_LOGIN_PLATFORMS = (
    "google", "linkedin", "workday", "greenhouse", "lever", "ashby", "icims",
    "taleo", "smartrecruiters", "jobvite", "bamboohr",
)
_LOGIN_EMAIL_KEYS = frozenset(f"{p}_email" for p in _LOGIN_PLATFORMS)

_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd|code)?|pwd|secret|token|credential|api[_-]?key|otp|mfa|2fa|session|cookie)",
    re.IGNORECASE,
)


def is_sensitive_key(key: str) -> bool:
    key = str(key)
    return key in _LOGIN_EMAIL_KEYS or bool(_SENSITIVE_KEY_RE.search(key))


def strip_credentials(info: dict | None) -> dict:
    """Return a copy of ``info`` without any credential-like fields."""
    if not isinstance(info, dict):
        return {}
    return {k: v for k, v in info.items() if not is_sensitive_key(k)}


# Fields whose answers are factual/legal statements about the candidate. They
# are only ever filled from values the user saved; there are no defaults.
FACTUAL_FIELDS = (
    "work_authorization", "requires_sponsorship", "legally_authorized",
    "willing_to_relocate", "background_check_consent", "data_processing_consent",
    "privacy_agreement", "how_did_you_hear", "previously_applied",
    "previously_employed", "government_employee", "non_compete",
    "gender", "race_ethnicity", "veteran_status", "disability_status",
)


def default_profile() -> dict:
    """Blank profile. Every factual/legal field starts empty on purpose."""
    return {
        'first_name': '', 'last_name': '', 'preferred_name': '',
        'email': '', 'phone': '', 'pronouns': '',
        'address': '', 'address_line_2': '', 'city': '', 'state': '',
        'zip_code': '', 'country': '',
        'linkedin': '', 'github': '', 'portfolio': '', 'website': '', 'twitter': '',
        'current_company': '', 'current_title': '', 'current_employer': '',
        'relocation_locations': '',
        'remote_preference': '',
        'willing_to_travel': '',
        'desired_salary': '',
        'salary_currency': '',
        'expected_salary_min': '',
        'expected_salary_max': '',
        'available_start': '',
        'notice_period': '',
        'years_experience': '',
        'highest_education': '',
        'referral_name': '',
        **{field: '' for field in FACTUAL_FIELDS},
    }


def load_profile(path: Path = PROFILE_PATH) -> dict:
    info = default_profile()
    if path.exists():
        try:
            stored = json.loads(path.read_text())
            if isinstance(stored, dict):
                info.update(strip_credentials(stored))
        except (OSError, ValueError):
            logger.warning("Could not read %s", path)
    return info


def save_profile(info: dict, path: Path = PROFILE_PATH) -> dict:
    clean = strip_credentials(info)
    # Only keep simple scalar values from the form.
    clean = {str(k): ("" if v is None else v) for k, v in clean.items()
             if isinstance(v, (str, int, float, bool)) or v is None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2))
    return clean


def scrub_profile_files(paths: Iterable[Path] = LEGACY_PROFILE_PATHS) -> list[str]:
    """Rewrite any profile file that still holds credentials. Returns paths scrubbed."""
    scrubbed = []
    for path in paths:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        clean = strip_credentials(data)
        if len(clean) != len(data):
            path.write_text(json.dumps(clean, indent=2))
            scrubbed.append(str(path))
            logger.warning("Removed %d credential field(s) from %s", len(data) - len(clean), path)
    return scrubbed
