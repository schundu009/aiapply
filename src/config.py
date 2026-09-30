"""Configuration management."""
import json
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_json_settings():
    """Load settings from data/settings.json if it exists."""
    settings_path = Path("data/settings.json")
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_model: str = Field(default="claude-sonnet-4-20250514")
    openai_api_key: str = Field(default="", description="OpenAI API key")
    openai_model: str = Field(default="gpt-4o", description="OpenAI model")
    notion_api_key: Optional[str] = Field(default=None)
    notion_resume_database_id: Optional[str] = Field(default=None)
    output_dir: Path = Field(default=Path("output"))
    jobs_file: Path = Field(default=Path("jobs.txt"))
    base_resume_file: Optional[Path] = Field(default=None)
    headless: bool = Field(default=False)
    auto_submit: bool = Field(default=False)
    submit_delay_seconds: int = Field(default=5)
    # Email notification settings
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from_email: str = Field(default="")
    email_notifications_enabled: bool = Field(default=False)
    # Database settings (shared with jobportal on Railway)
    database_url: str = Field(default="", description="PostgreSQL connection URL")
    redis_url: str = Field(default="", description="Redis connection URL")

    @property
    def is_postgres(self) -> bool:
        return bool(self.database_url) and self.database_url.startswith(("postgres://", "postgresql"))

    @property
    def use_database(self) -> bool:
        """Whether to use database storage instead of JSON files."""
        return self.is_postgres

    def __init__(self, **kwargs):
        # Load from JSON settings first, then override with kwargs/env
        json_settings = _load_json_settings()
        # Map JSON keys to pydantic field names
        if 'anthropic_key' in json_settings:
            json_settings['anthropic_api_key'] = json_settings.pop('anthropic_key')
        # Merge JSON settings with kwargs (kwargs take precedence)
        merged = {**json_settings, **kwargs}
        super().__init__(**merged)


settings = Settings()
