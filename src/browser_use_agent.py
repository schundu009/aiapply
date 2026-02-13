"""
Browser-Use Agent for AI-powered form filling.

This module provides an alternative to selector-based form filling by using
an LLM to understand and interact with web pages naturally.

Install: pip install browser-use

Usage:
    agent = BrowserUseFormFiller(anthropic_key="sk-ant-...")
    result = await agent.apply_to_job(job_url, resume_data)
"""

import asyncio
from pathlib import Path
from typing import Optional
import json

from .config import settings


class BrowserUseFormFiller:
    """Uses browser-use library for LLM-powered form filling."""

    def __init__(self, anthropic_key: str = None, openai_key: str = None):
        self.anthropic_key = anthropic_key
        self.openai_key = openai_key
        self._agent = None
        self._browser = None

    async def _init_browser_use(self):
        """Initialize browser-use components."""
        try:
            from browser_use import Agent, Browser
            from langchain_anthropic import ChatAnthropic

            self._browser = Browser()

            # Use Anthropic Claude as the LLM
            llm = ChatAnthropic(
                model=settings.anthropic_model,
                api_key=self.anthropic_key,
                max_tokens=4096,
            )

            return llm
        except ImportError as e:
            print(f"[BrowserUse] Import error: {e}")
            print("[BrowserUse] Install with: pip install browser-use langchain-anthropic")
            return None

    async def apply_to_job(
        self,
        job_url: str,
        resume: dict,
        resume_pdf_path: Optional[str] = None,
        headless: bool = False,
    ) -> dict:
        """
        Apply to a job using AI-powered form filling.

        Args:
            job_url: URL of the job posting
            resume: Dictionary with user info (first_name, last_name, email, phone, etc.)
            resume_pdf_path: Path to resume PDF for upload
            headless: Run browser in headless mode

        Returns:
            dict with success status and details
        """
        try:
            from browser_use import Agent, Browser

            llm = await self._init_browser_use()
            if not llm:
                return {"success": False, "error": "Failed to initialize browser-use"}

            # Create the task description for the LLM
            task = self._create_task_prompt(job_url, resume, resume_pdf_path)

            print(f"[BrowserUse] Starting job application to: {job_url}")
            print(f"[BrowserUse] Task: {task[:200]}...")

            # Create browser with appropriate settings
            browser = Browser(
                config={
                    "headless": headless,
                    "disable_security": True,  # Allow file uploads
                }
            )

            # Create agent with the task
            agent = Agent(
                task=task,
                llm=llm,
                browser=browser,
                max_actions_per_step=10,
            )

            # Run the agent
            history = await agent.run(max_steps=50)

            # Check result
            success = self._check_success(history)

            return {
                "success": success,
                "steps": len(history) if history else 0,
                "message": "Application completed" if success else "Application may have failed",
            }

        except Exception as e:
            print(f"[BrowserUse] Error: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def _create_task_prompt(
        self, job_url: str, resume: dict, resume_pdf_path: Optional[str]
    ) -> str:
        """Create a detailed task prompt for the LLM agent."""

        # Format resume data
        name = f"{resume.get('first_name', '')} {resume.get('last_name', '')}".strip()
        email = resume.get("email", "")
        phone = resume.get("phone", "")
        linkedin = resume.get("linkedin", "")

        task = f"""
You are helping to apply for a job. Here's what you need to do:

1. Go to this job posting URL: {job_url}

2. Find and click the "Apply" button to get to the application form.
   - Look for buttons labeled "Apply", "Apply Now", "Apply for this job"
   - On Workday sites, look for the apply button

3. If a login page appears:
   - Click "Sign in with email" if available
   - Enter email: {resume.get('workday_email', email)}
   - Enter password: {resume.get('workday_password', '')}
   - Click "Sign In" button

4. Fill out the application form with this information:
   - First Name: {resume.get('first_name', '')}
   - Last Name: {resume.get('last_name', '')}
   - Email: {email}
   - Phone: {phone}
   - LinkedIn: {linkedin}
   - City: {resume.get('city', '')}
   - State: {resume.get('state', '')}
   - Country: United States

5. For work authorization questions:
   - Are you authorized to work in the US? Yes
   - Do you require sponsorship? {resume.get('requires_sponsorship', 'No')}

6. Upload resume if there's a file upload field:
   - Resume file: {resume_pdf_path if resume_pdf_path else 'Skip if not provided'}

7. Navigate through all form steps:
   - Click "Save and Continue", "Next", or "Continue" buttons
   - Fill in any required fields on each step
   - Look for validation errors and fix them

8. On the Review page, verify all information is correct.

9. Click "Submit Application" or "Submit" to complete.

IMPORTANT:
- Do NOT click "Create Account" or "Sign Up" - only use existing account login
- Wait for pages to load between steps
- Look for and handle any validation errors
- Skip optional fields if not in the provided data
"""
        return task

    def _check_success(self, history) -> bool:
        """Check if the application was submitted successfully."""
        if not history:
            return False

        # Look for success indicators in the last few actions
        try:
            last_actions = history[-5:] if len(history) >= 5 else history
            for action in last_actions:
                if hasattr(action, 'result') and action.result:
                    result_text = str(action.result).lower()
                    if any(phrase in result_text for phrase in [
                        "thank you",
                        "submitted",
                        "application complete",
                        "received your application",
                    ]):
                        return True
        except:
            pass

        return False


async def main():
    """Test the browser-use agent."""
    import os

    # Load settings
    settings_path = Path(__file__).parent.parent / "data" / "settings.json"
    personal_info_path = Path(__file__).parent.parent / "data" / "personal_info.json"

    if not settings_path.exists():
        print("Settings file not found")
        return

    with open(settings_path) as f:
        settings = json.load(f)

    with open(personal_info_path) as f:
        personal_info = json.load(f)

    agent = BrowserUseFormFiller(
        anthropic_key=settings.get("anthropic_key")
    )

    # Test with a sample job URL
    test_url = input("Enter job URL to test: ").strip()
    if not test_url:
        print("No URL provided")
        return

    result = await agent.apply_to_job(
        job_url=test_url,
        resume=personal_info,
        headless=False,
    )

    print(f"\nResult: {result}")


if __name__ == "__main__":
    asyncio.run(main())
