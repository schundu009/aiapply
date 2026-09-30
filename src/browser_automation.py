"""Browser automation for job application form filling and submission."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from .config import settings
from .models import Application, ApplicationStatus, ATSPlatform, BaseResume
from .form_agent import FormAgent
from .profile_store import strip_credentials


class AutoApplyProgress:
    """Tracks progress of auto-apply stages."""
    STAGES = [
        ('browser', 'Starting browser'),
        ('navigate', 'Navigating to job page'),
        ('apply', 'Clicking Apply button'),
        ('modal', 'Handling application options'),
        ('login', 'Sign-in check'),
        ('form', 'Navigating form steps'),
        ('fill', 'Filling form fields'),
        ('upload', 'Uploading resume'),
        ('submit', 'Submitting application'),
    ]

    def __init__(self, callback=None):
        self.callback = callback
        self.stages = {s[0]: {'status': 'pending', 'message': s[1], 'error': None} for s in self.STAGES}
        self.current_stage = None
        self.logs = []

    def start_stage(self, stage: str, message: str = None):
        """Mark a stage as in progress."""
        self.current_stage = stage
        if stage in self.stages:
            self.stages[stage]['status'] = 'active'
            if message:
                self.stages[stage]['message'] = message
        self._emit()

    def complete_stage(self, stage: str, message: str = None):
        """Mark a stage as completed."""
        if stage in self.stages:
            self.stages[stage]['status'] = 'completed'
            if message:
                self.stages[stage]['message'] = message
        self._emit()

    def fail_stage(self, stage: str, error: str):
        """Mark a stage as failed."""
        if stage in self.stages:
            self.stages[stage]['status'] = 'error'
            self.stages[stage]['error'] = error
        self._emit()

    def skip_stage(self, stage: str, message: str = None):
        """Mark a stage as skipped."""
        if stage in self.stages:
            self.stages[stage]['status'] = 'skipped'
            if message:
                self.stages[stage]['message'] = message
        self._emit()

    def log(self, message: str):
        """Add a log message."""
        self.logs.append(message)
        self._emit()

    def _emit(self):
        """Emit progress update via callback."""
        if self.callback:
            self.callback(self.to_dict())

    def to_dict(self):
        return {
            'current_stage': self.current_stage,
            'stages': self.stages,
            'logs': self.logs
        }


class AutoApplyResult:
    """Result of an auto-apply operation."""
    def __init__(
        self,
        success: bool,
        status: str,
        message: str,
        captcha_detected: bool = False,
        screenshot_path: Optional[str] = None,
        missing_fields: Optional[List[Dict]] = None,
        fields_filled: int = 0,
        retry_count: int = 0,
        progress: Optional[Dict] = None
    ):
        self.success = success
        self.status = status
        self.message = message
        self.captcha_detected = captcha_detected
        self.screenshot_path = screenshot_path
        self.missing_fields = missing_fields or []
        self.fields_filled = fields_filled
        self.retry_count = retry_count
        self.progress = progress or {}

    def to_dict(self):
        return {
            'success': self.success,
            'status': self.status,
            'message': self.message,
            'captcha_detected': self.captcha_detected,
            'screenshot_path': self.screenshot_path,
            'missing_fields': self.missing_fields,
            'fields_filled': self.fields_filled,
            'retry_count': self.retry_count,
            'progress': self.progress
        }


class BrowserAutomation:
    """Automates job application form filling and submission using Playwright."""

    def __init__(self, headless: bool = True, personal_info: Optional[Dict] = None):
        self.headless = headless if headless is not None else True  # Default to headless
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.playwright = None
        self.personal_info = strip_credentials(personal_info or {})
        self.form_agent: Optional[FormAgent] = None
        self.missing_fields: List[Dict] = []
        self.max_retries = 3
        # Set when a site requires signing in; the user must do that manually.
        self.signin_required = False

    def _init_form_agent(self):
        """Initialize the form agent if not already initialized."""
        if self.form_agent is None:
            try:
                self.form_agent = FormAgent(provider="auto")
            except Exception as e:
                print(f"Warning: Could not initialize FormAgent: {e}")
                self.form_agent = None

    async def start(self):
        """Start a standard Playwright Chromium browser.

        No fingerprint spoofing, automation-flag hiding, location spoofing or
        artificial "human-like" slowdowns: sites see an ordinary automated
        browser and any CAPTCHA is left for the user to complete.
        """
        self.playwright = await async_playwright().start()
        width, height = 1440, 900
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[f'--window-size={width},{height}'] if not self.headless else [],
        )
        self.context = await self.browser.new_context(
            viewport={"width": width, "height": height},
        )

    async def stop(self):
        """Stop the browser."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def handle_login_if_required(self, page: Page) -> bool:
        """Detect a sign-in wall. Returns True when the user must sign in manually.

        AutoApply never logs in, never creates accounts and never stores
        passwords; it only reports that manual sign-in is needed.
        """
        try:
            login_indicators = [
                "form[action*='login']",
                "form[action*='signin']",
                "[data-automation-id='loginForm']",
                "input[type='password']:visible",
            ]
            for selector in login_indicators:
                if await page.locator(selector).count() > 0:
                    return True
            return False
        except Exception:
            return False

    async def intelligent_form_fill(
        self,
        page: Page,
        resume: "BaseResume",
        resume_text: str = "",
        job_description: str = ""
    ) -> Dict[str, Any]:
        """Use LLM to intelligently analyze and fill form fields."""
        self._init_form_agent()

        if not self.form_agent:
            return {"success": False, "missing_fields": [], "error": "FormAgent not initialized"}

        try:
            # Wait for page to load
            await asyncio.sleep(1)

            # Get page content for analysis
            page_text = await page.inner_text("body")
            page_html = await page.content()

            # Analyze form fields using LLM
            analysis = await self.form_agent.analyze_form_fields(page_html, page_text[:8000])

            if "error" in analysis:
                return {"success": False, "missing_fields": [], "error": analysis["error"]}

            fields = analysis.get("fields", [])
            filled_count = 0
            missing_fields = []

            # Build personal_info from resume and stored info
            personal_info = {
                'first_name': resume.full_name.split()[0] if resume.full_name else '',
                'last_name': resume.full_name.split()[-1] if len(resume.full_name.split()) > 1 else '',
                'email': resume.email,
                'phone': resume.phone,
                'linkedin': resume.linkedin or '',
                'location': resume.location or '',
                **strip_credentials(self.personal_info)
            }

            for field in fields:
                label = field.get('label', '')
                field_type = field.get('type', 'text')
                maps_to = field.get('maps_to', '')

                # Get value from personal info
                value = self.form_agent.map_field_to_value(label, field_type, personal_info)

                if not value and maps_to:
                    value = personal_info.get(maps_to, '')

                if not value and field.get('required'):
                    # Try using LLM to generate a value
                    options = field.get('options', [])
                    # Only free-text questions are drafted by the LLM; factual,
                    # legal, consent and EEO questions stay blank for the user.
                    value = await self.form_agent.get_field_value_with_llm(
                        label, field_type, options, personal_info, resume_text,
                        job_description=job_description,
                    )

                if value:
                    # Try to fill the field
                    filled = await self._fill_field_by_label(page, label, value, field_type)
                    if filled:
                        filled_count += 1
                elif field.get('required'):
                    missing_fields.append({
                        'label': label,
                        'type': field_type,
                        'maps_to': maps_to or label.lower().replace(' ', '_')
                    })

            self.missing_fields = missing_fields

            return {
                "success": filled_count > 0,
                "fields_filled": filled_count,
                "total_fields": len(fields),
                "missing_fields": missing_fields
            }

        except Exception as e:
            return {"success": False, "missing_fields": [], "error": str(e)}

    async def _fill_field_by_label(self, page: Page, label: str, value: str, field_type: str) -> bool:
        """Fill a form field by finding it via its label."""
        if not value:
            return False

        try:
            # Strategy 1: Find label with 'for' attribute
            labels = await page.query_selector_all("label")
            for lbl in labels:
                text = await lbl.inner_text()
                if label.lower() in text.lower():
                    for_id = await lbl.get_attribute("for")
                    if for_id:
                        inp = page.locator(f"[id='{for_id}']")
                        if await inp.count() > 0 and await inp.first.is_visible():
                            if field_type == "select":
                                await inp.first.select_option(label=value)
                            elif field_type == "checkbox":
                                if value.lower() in ['yes', 'true', '1']:
                                    await inp.first.check()
                            else:
                                await inp.first.fill(value)
                            return True

            # Strategy 2: Try placeholder matching
            inp = page.locator(f"input[placeholder*='{label}' i], textarea[placeholder*='{label}' i]")
            if await inp.count() > 0 and await inp.first.is_visible():
                await inp.first.fill(value)
                return True

            # Strategy 3: Try name/id attribute matching
            label_slug = label.lower().replace(' ', '_').replace('-', '_')
            inp = page.locator(f"input[name*='{label_slug}' i], input[id*='{label_slug}' i]")
            if await inp.count() > 0 and await inp.first.is_visible():
                await inp.first.fill(value)
                return True

            return False

        except Exception:
            return False

    def get_missing_fields(self) -> List[Dict]:
        """Return the list of missing required fields."""
        return self.missing_fields

    def update_personal_info(self, new_info: Dict):
        """Update personal info for retrying form fill (credentials are dropped)."""
        self.personal_info.update(strip_credentials(new_info))

    async def auto_apply(
        self,
        application: Application,
        base_resume: BaseResume,
        resume_pdf_path: Path,
        cover_letter_pdf_path: Optional[Path] = None,
        auto_submit: bool = False,
        resume_text: str = "",
        job_description_text: str = "",
        progress_callback=None,
    ) -> AutoApplyResult:
        """
        Non-blocking auto-apply method for web context.
        Returns structured result instead of using print/input.
        Uses intelligent LLM-powered form filling with retry logic.

        Args:
            progress_callback: Optional callback to receive progress updates
        """
        # Initialize progress tracker
        progress = AutoApplyProgress(callback=progress_callback)
        screenshot_path = None
        retry_count = 0
        fields_filled = 0

        # Stage 1: Start browser
        progress.start_stage('browser', 'Starting browser...')
        try:
            if not self.browser:
                await self.start()
            progress.complete_stage('browser', 'Browser started')
            progress.log('Browser launched successfully')
        except Exception as e:
            progress.fail_stage('browser', str(e))
            return AutoApplyResult(
                success=False,
                status="error",
                message=f"Failed to start browser: {str(e)}",
                progress=progress.to_dict()
            )

        page = await self.context.new_page()

        # Stage 2: Navigate to job page
        job_url_str = str(application.job_url)
        progress.start_stage('navigate', f'Navigating to {job_url_str[:50]}...')
        try:
            await page.goto(str(application.job_url), wait_until="networkidle", timeout=30000)
            application.status = ApplicationStatus.FORM_OPENED
            progress.complete_stage('navigate', 'Page loaded successfully')
            progress.log(f'Loaded: {page.url}')
        except Exception as e:
            progress.fail_stage('navigate', str(e))
            return AutoApplyResult(
                success=False,
                status="error",
                message=f"Failed to navigate to job page: {str(e)}",
                progress=progress.to_dict()
            )

        # Detect ATS platform first
        ats_platform = application.job_description.ats_platform if application.job_description else ATSPlatform.UNKNOWN
        progress.log(f'Detected ATS platform: {ats_platform.value}')
        handler = self._get_ats_handler(ats_platform)

        # For Workday, the handler manages apply/modal/login/form steps internally
        # For other platforms, we handle login here
        if ats_platform != ATSPlatform.WORKDAY:
            # Stage 3: Detect a sign-in wall (non-Workday). Never log in automatically.
            progress.start_stage('login', 'Checking for login requirements...')
            if await self.handle_login_if_required(page):
                progress.fail_stage('login', 'Sign-in required: please sign in manually')
                return await self._signin_required_result(page, application, progress)
            progress.skip_stage('login', 'No login required')
            progress.log('No login needed - public application form')

            # Skip Workday-specific stages for other platforms
            progress.skip_stage('apply', 'Not needed for this platform')
            progress.skip_stage('modal', 'Not needed for this platform')
            progress.skip_stage('form', 'Not needed for this platform')
        else:
            # For Workday, mark these stages as pending - handler will update them
            progress.start_stage('apply', 'Clicking Apply button...')
            progress.log('Starting Workday application flow...')

        # Stage 6: Fill form fields
        progress.start_stage('fill', 'Filling form fields...')
        filled = False
        missing_fields = []
        try:
            # Store progress for handler access
            self.current_progress = progress

            # First try: Standard form filling
            progress.log('Attempting standard form fill...')
            filled = await handler(page, base_resume, resume_pdf_path, cover_letter_pdf_path)

            if self.signin_required:
                return await self._signin_required_result(page, application, progress)

            if filled:
                progress.log('Standard form fill successful')
                fields_filled += 1
            else:
                # Try intelligent LLM-based filling
                progress.log('Standard fill incomplete, trying LLM analysis...')
                self._init_form_agent()
                if self.form_agent:
                    intel_result = await self.intelligent_form_fill(
                        page, base_resume, resume_text, job_description_text
                    )
                    filled = intel_result.get("success", False)
                    fields_filled = intel_result.get("fields_filled", 0)
                    missing_fields = intel_result.get("missing_fields", [])
                    progress.log(f'LLM filled {fields_filled} fields')

                    # Retry logic for missing fields
                    while missing_fields and retry_count < self.max_retries:
                        retry_count += 1
                        progress.log(f'Retry {retry_count}/{self.max_retries} for missing fields...')

                        if retry_count >= self.max_retries:
                            progress.fail_stage('fill', f'Missing {len(missing_fields)} required fields')
                            # Skip upload and submit stages since form fill failed
                            progress.skip_stage('upload', 'Form not filled')
                            progress.skip_stage('submit', 'Form not filled')
                            return AutoApplyResult(
                                success=False,
                                status="missing_fields",
                                message=f"Could not fill {len(missing_fields)} required fields after {retry_count} attempts.",
                                missing_fields=missing_fields,
                                fields_filled=fields_filled,
                                retry_count=retry_count,
                                progress=progress.to_dict()
                            )

                        await asyncio.sleep(1)
                        intel_result = await self.intelligent_form_fill(
                            page, base_resume, resume_text, job_description_text
                        )
                        filled = intel_result.get("success", False)
                        fields_filled = intel_result.get("fields_filled", 0)
                        missing_fields = intel_result.get("missing_fields", [])

            if filled:
                progress.complete_stage('fill', f'Filled {fields_filled} fields')
            else:
                progress.fail_stage('fill', 'Could not fill form fields')

        except Exception as e:
            progress.fail_stage('fill', str(e))
            progress.log(f'Form fill error: {str(e)}')
            # Skip upload and submit stages since form fill failed
            progress.skip_stage('upload', 'Form not filled')
            progress.skip_stage('submit', 'Form not filled')
            return AutoApplyResult(
                success=False,
                status="error",
                message=f"Error filling form: {str(e)}",
                progress=progress.to_dict()
            )

        # Stage 7: Upload resume (only if form was filled)
        if filled:
            progress.start_stage('upload', 'Uploading resume...')
            try:
                # Resume upload is usually handled in the form fill stage
                # This is a verification step
                progress.complete_stage('upload', 'Resume uploaded')
                progress.log(f'Resume: {resume_pdf_path.name}')
                if cover_letter_pdf_path:
                    progress.log(f'Cover letter: {cover_letter_pdf_path.name}')
            except Exception as e:
                progress.fail_stage('upload', str(e))
                progress.log(f'Upload error: {str(e)}')

        # Stage 8: Submit application (only if form was filled)
        if filled:
            application.status = ApplicationStatus.FORM_FILLED
            screenshot_path = await self._take_screenshot(page, application.id, "filled")

            # CAPTCHA detector: never attempt to solve one. Stop at review and
            # ask the user to complete the CAPTCHA and submit manually.
            if await self.check_for_captcha(page):
                progress.skip_stage('submit', 'CAPTCHA present - finish manually')
                application.notes = "CAPTCHA present. Complete it and submit manually."
                return AutoApplyResult(
                    success=True,
                    status="form_filled",
                    message="Form filled. A CAPTCHA is present - please complete it and submit manually.",
                    captcha_detected=True,
                    screenshot_path=screenshot_path,
                    fields_filled=fields_filled,
                    progress=progress.to_dict()
                )

            if auto_submit:
                progress.start_stage('submit', 'Submitting application...')
                try:
                    # Wait briefly for review
                    await asyncio.sleep(settings.submit_delay_seconds)
                    progress.log('Checking for CAPTCHA before submit...')

                    # Check for CAPTCHA again before submit
                    if await self.check_for_captcha(page):
                        progress.fail_stage('submit', 'CAPTCHA detected before submission')
                        return AutoApplyResult(
                            success=True,
                            status="form_filled",
                            message="Form filled. CAPTCHA detected before submission. Please submit manually.",
                            captcha_detected=True,
                            screenshot_path=screenshot_path,
                            fields_filled=fields_filled,
                            progress=progress.to_dict()
                        )

                    # Attempt submission
                    progress.log('Clicking submit button...')
                    submitted = await self._submit_application(page, ats_platform)

                    if submitted:
                        application.status = ApplicationStatus.SUBMITTED
                        application.applied_at = datetime.now()
                        screenshot_path = await self._take_screenshot(page, application.id, "submitted")
                        progress.complete_stage('submit', 'Application submitted!')
                        progress.log('Application submitted successfully!')
                        return AutoApplyResult(
                            success=True,
                            status="submitted",
                            message="Application submitted successfully!",
                            screenshot_path=screenshot_path,
                            fields_filled=fields_filled,
                            progress=progress.to_dict()
                        )
                    else:
                        application.status = ApplicationStatus.MANUAL_REQUIRED
                        application.notes = "Auto-submit failed. Please submit manually."
                        progress.fail_stage('submit', 'Submit button not found or click failed')
                        progress.log('Could not find or click submit button')
                        return AutoApplyResult(
                            success=True,
                            status="manual_required",
                            message="Form filled but could not auto-submit. Please review and submit manually.",
                            screenshot_path=screenshot_path,
                            fields_filled=fields_filled,
                            progress=progress.to_dict()
                        )
                except Exception as e:
                    progress.fail_stage('submit', str(e))
                    progress.log(f'Submit error: {str(e)}')
                    return AutoApplyResult(
                        success=True,
                        status="manual_required",
                        message=f"Form filled but submission failed: {str(e)}",
                        screenshot_path=screenshot_path,
                        fields_filled=fields_filled,
                        progress=progress.to_dict()
                    )
            else:
                progress.skip_stage('submit', 'Manual submission required')
                application.status = ApplicationStatus.FORM_FILLED
                application.notes = "Form filled. Manual submission required."
                return AutoApplyResult(
                    success=True,
                    status="form_filled",
                    message="Form filled successfully. Please review and submit manually.",
                    screenshot_path=screenshot_path,
                    fields_filled=fields_filled,
                    progress=progress.to_dict()
                )
        else:
            # Form fill failed - skip upload and submit stages
            progress.skip_stage('upload', 'Form not filled')
            progress.skip_stage('submit', 'Form not filled')
            application.status = ApplicationStatus.MANUAL_REQUIRED
            application.notes = "Could not fill form automatically"
            screenshot_path = await self._take_screenshot(page, application.id, "failed")
            return AutoApplyResult(
                success=False,
                status="fill_failed",
                message="Could not auto-fill the form. Please complete manually.",
                screenshot_path=screenshot_path,
                progress=progress.to_dict()
            )

        # This should not be reached, but just in case
        return AutoApplyResult(
            success=False,
            status="unknown",
            message="Unknown state reached",
            progress=progress.to_dict()
        )

    async def _signin_required_result(self, page: Page, application: Application, progress) -> "AutoApplyResult":
        """Stop the run because the site needs the user to sign in themselves."""
        application.status = ApplicationStatus.MANUAL_REQUIRED
        application.notes = "Sign-in required. Please sign in manually and finish the application."
        progress.skip_stage('fill', 'Sign-in required')
        progress.skip_stage('upload', 'Sign-in required')
        progress.skip_stage('submit', 'Sign-in required')
        screenshot_path = None
        try:
            screenshot_path = await self._take_screenshot(page, application.id, "signin_required")
        except Exception:
            pass
        return AutoApplyResult(
            success=False,
            status="signin_required",
            message="This site requires you to sign in. Please sign in manually and finish the application.",
            screenshot_path=screenshot_path,
            progress=progress.to_dict()
        )

    async def apply_to_job(
        self,
        application: Application,
        base_resume: BaseResume,
        resume_pdf_path: Path,
        cover_letter_pdf_path: Optional[Path] = None,
        auto_submit: bool = False,
    ) -> Application:
        """
        Legacy method - now wraps auto_apply for backwards compatibility.
        """
        result = await self.auto_apply(
            application, base_resume, resume_pdf_path, cover_letter_pdf_path, auto_submit
        )
        return application

    async def _take_screenshot(self, page: Page, app_id: str, stage: str) -> str:
        """Take a screenshot and return the path."""
        screenshots_dir = Path("output") / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{app_id}_{stage}_{timestamp}.png"
        filepath = screenshots_dir / filename
        await page.screenshot(path=str(filepath), full_page=True)
        return str(filepath)

    def _get_ats_handler(self, platform: ATSPlatform):
        """Get the appropriate form handler for the ATS platform."""
        handlers = {
            ATSPlatform.GREENHOUSE: self._fill_greenhouse_form,
            ATSPlatform.LEVER: self._fill_lever_form,
            ATSPlatform.ASHBY: self._fill_ashby_form,
            ATSPlatform.WORKDAY: self._fill_workday_form,
            # These platforms use the generic handler which is comprehensive
            ATSPlatform.ICIMS: self._fill_generic_form,
            ATSPlatform.TALEO: self._fill_generic_form,
            ATSPlatform.SMARTRECRUITERS: self._fill_generic_form,
            ATSPlatform.JOBVITE: self._fill_generic_form,
            ATSPlatform.BAMBOOHR: self._fill_generic_form,
            ATSPlatform.BREEZYHR: self._fill_generic_form,
            ATSPlatform.JAZZ: self._fill_generic_form,
        }
        return handlers.get(platform, self._fill_generic_form)

    async def _fill_greenhouse_form(
        self,
        page: Page,
        resume: BaseResume,
        resume_pdf: Path,
        cover_letter_pdf: Optional[Path],
    ) -> bool:
        """Fill Greenhouse application form - boards.greenhouse.io"""
        try:
            name_parts = resume.full_name.split()
            first_name = name_parts[0]
            last_name = name_parts[-1] if len(name_parts) > 1 else ""

            # Step 1: Click Apply button if on job description page
            apply_btn = page.locator("a:has-text('Apply'), button:has-text('Apply'), a:has-text('Apply Now')")
            if await apply_btn.count() > 0 and await apply_btn.first.is_visible():
                await apply_btn.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)

            # Step 2: Fill application form fields
            filled_count = 0

            # Name fields - Greenhouse typically uses these IDs
            field_mappings = [
                (["#first_name", "input[name='first_name']", "input[autocomplete='given-name']"], first_name),
                (["#last_name", "input[name='last_name']", "input[autocomplete='family-name']"], last_name),
                (["#email", "input[name='email']", "input[type='email']"], resume.email),
                (["#phone", "input[name='phone']", "input[type='tel']"], resume.phone),
                (["input[name*='linkedin']", "input[placeholder*='LinkedIn']"], resume.linkedin or ""),
            ]

            for selectors, value in field_mappings:
                if not value:
                    continue
                for selector in selectors:
                    if await self._fill_field(page, selector, value):
                        filled_count += 1
                        break

            # Step 3: Upload resume
            resume_selectors = [
                "input[type='file'][name*='resume']",
                "input[type='file'][data-field='resume']",
                "input[type='file']#resume",
                "input[type='file']"
            ]
            for selector in resume_selectors:
                try:
                    resume_input = page.locator(selector)
                    if await resume_input.count() > 0:
                        await resume_input.first.set_input_files(str(resume_pdf))
                        filled_count += 1
                        break
                except Exception:
                    continue

            # Step 4: Upload cover letter if provided
            if cover_letter_pdf:
                cover_selectors = [
                    "input[type='file'][name*='cover']",
                    "input[type='file'][data-field='cover_letter']"
                ]
                for selector in cover_selectors:
                    try:
                        cover_input = page.locator(selector)
                        if await cover_input.count() > 0:
                            await cover_input.first.set_input_files(str(cover_letter_pdf))
                            break
                    except Exception:
                        continue

            return filled_count > 0

        except Exception:
            return False

    async def _fill_lever_form(
        self,
        page: Page,
        resume: BaseResume,
        resume_pdf: Path,
        cover_letter_pdf: Optional[Path],
    ) -> bool:
        """Fill Lever application form - jobs.lever.co"""
        try:
            # Step 1: Click Apply button
            apply_btn = page.locator("a.postings-btn, a:has-text('Apply'), button:has-text('Apply')")
            if await apply_btn.count() > 0 and await apply_btn.first.is_visible():
                await apply_btn.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)

            filled_count = 0

            # Step 2: Fill form fields - Lever uses specific input names
            field_mappings = [
                (["input[name='name']", "input[placeholder*='Full name']"], resume.full_name),
                (["input[name='email']", "input[type='email']"], resume.email),
                (["input[name='phone']", "input[type='tel']"], resume.phone),
                (["input[name*='linkedin']", "input[name='urls[LinkedIn]']", "input[placeholder*='LinkedIn']"], resume.linkedin or ""),
                (["input[name*='website']", "input[name='urls[Portfolio]']"], ""),
            ]

            for selectors, value in field_mappings:
                if not value:
                    continue
                for selector in selectors:
                    if await self._fill_field(page, selector, value):
                        filled_count += 1
                        break

            # Step 3: Upload resume - Lever uses specific file input
            resume_input = page.locator("input[type='file'][name='resume'], input[type='file']")
            if await resume_input.count() > 0:
                await resume_input.first.set_input_files(str(resume_pdf))
                filled_count += 1

            # Custom questions are left for the user to answer.

            return filled_count > 0

        except Exception:
            return False

    async def _fill_ashby_form(
        self,
        page: Page,
        resume: BaseResume,
        resume_pdf: Path,
        cover_letter_pdf: Optional[Path],
    ) -> bool:
        """Fill Ashby application form - jobs.ashbyhq.com"""
        try:
            name_parts = resume.full_name.split()
            first_name = name_parts[0]
            last_name = name_parts[-1] if len(name_parts) > 1 else ""

            # Step 1: Click Apply button
            apply_btn = page.locator("button:has-text('Apply'), a:has-text('Apply'), [data-testid='apply-button']")
            if await apply_btn.count() > 0 and await apply_btn.first.is_visible():
                await apply_btn.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)

            filled_count = 0

            # Step 2: Fill fields - Ashby uses various input patterns
            # Try label-based filling first
            await self._fill_by_label_text(page, "First", first_name)
            await self._fill_by_label_text(page, "Last", last_name)
            await self._fill_by_label_text(page, "Email", resume.email)
            await self._fill_by_label_text(page, "Phone", resume.phone)
            if resume.linkedin:
                await self._fill_by_label_text(page, "LinkedIn", resume.linkedin)

            # Also try direct selectors
            field_mappings = [
                (["input[name*='firstName']", "input[placeholder*='First']"], first_name),
                (["input[name*='lastName']", "input[placeholder*='Last']"], last_name),
                (["input[name*='email']", "input[type='email']"], resume.email),
                (["input[name*='phone']", "input[type='tel']"], resume.phone),
                (["input[name*='linkedin']", "input[placeholder*='LinkedIn']"], resume.linkedin or ""),
            ]

            for selectors, value in field_mappings:
                if not value:
                    continue
                for selector in selectors:
                    if await self._fill_field(page, selector, value):
                        filled_count += 1
                        break

            # Step 3: Upload resume
            resume_input = page.locator("input[type='file']")
            if await resume_input.count() > 0:
                await resume_input.first.set_input_files(str(resume_pdf))
                filled_count += 1

            return filled_count > 0

        except Exception:
            return False

    async def _fill_workday_form(
        self,
        page: Page,
        resume: BaseResume,
        resume_pdf: Path,
        cover_letter_pdf: Optional[Path],
    ) -> bool:
        """Fill Workday application form - *.myworkdayjobs.com

        Workday Application Flow:
        1. Job Description Page -> Click "Apply" button
        2. "Start Your Application" Modal -> Click "Use My Last Application" or "Autofill with Resume"
        3. Sign In Page (if not logged in) -> STOP: the user must sign in manually
        4. Multi-step Application Form -> Fill each step and click "Next"
           - My Information
           - My Experience
           - Application Questions
           - Voluntary Disclosures
           - Self Identify
           - Review
        5. Final Submit -> Click "Submit" button
        """
        print(f"[Workday] Handler called - starting Workday form fill", flush=True)

        # Get progress tracker if available
        progress = getattr(self, 'current_progress', None)

        try:
            # Safely extract name parts
            full_name = resume.full_name if resume and resume.full_name else "Unknown"
            name_parts = full_name.split()
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[-1] if len(name_parts) > 1 else ""

            await asyncio.sleep(2)
            initial_url = page.url
            print(f"[Workday] === Starting Workday Application ===", flush=True)
            print(f"[Workday] Initial URL: {initial_url}", flush=True)

            if progress:
                progress.log(f'Starting Workday application: {initial_url}')

            # ============================================================
            # STEP 1: Click Apply button on job description page
            # ============================================================
            if progress:
                progress.log('Step 1: Looking for Apply button...')

            # Wait for page to be fully loaded
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except:
                pass

            apply_selectors = [
                "[data-automation-id='jobPostingApplyButton']",
                "button:has-text('Apply'):not(:has-text('Applied'))",
                "a:has-text('Apply'):not(:has-text('Applied'))",
                "a.css-1hc8qm6",  # Common Workday Apply link class
            ]

            apply_clicked = False
            for selector in apply_selectors:
                try:
                    apply_btn = page.locator(selector)
                    count = await apply_btn.count()
                    print(f"[Workday] Checking selector '{selector}': found {count} elements", flush=True)

                    if count > 0:
                        # Get the first visible button
                        btn = apply_btn.first

                        # Wait for it to be visible
                        try:
                            await btn.wait_for(state="visible", timeout=5000)
                        except:
                            print(f"[Workday] Button not visible for {selector}", flush=True)
                            continue

                        # Scroll into view
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)

                        print(f"[Workday] Step 1: Clicking Apply button ({selector})", flush=True)
                        if progress:
                            progress.log(f'Found Apply button, clicking...')

                        # Try multiple click methods
                        click_success = False

                        # Method 1: Regular click
                        try:
                            await btn.click(timeout=5000)
                            click_success = True
                            print(f"[Workday] Regular click succeeded", flush=True)
                        except Exception as e:
                            print(f"[Workday] Regular click failed: {e}", flush=True)

                        # Method 2: Force click
                        if not click_success:
                            try:
                                await btn.click(force=True, timeout=5000)
                                click_success = True
                                print(f"[Workday] Force click succeeded", flush=True)
                            except Exception as e:
                                print(f"[Workday] Force click failed: {e}", flush=True)

                        # Method 3: JavaScript click
                        if not click_success:
                            try:
                                await btn.evaluate("el => el.click()")
                                click_success = True
                                print(f"[Workday] JS click succeeded", flush=True)
                            except Exception as e:
                                print(f"[Workday] JS click failed: {e}", flush=True)

                        if not click_success:
                            continue

                        # Wait for response (modal or page change)
                        await asyncio.sleep(2)

                        # Check if URL changed or modal appeared
                        new_url = page.url
                        modal = page.locator("[role='dialog'], [data-automation-id='wd-popup-content']")

                        if new_url != initial_url:
                            print(f"[Workday] URL changed to: {new_url}", flush=True)
                            if progress:
                                progress.log(f'Page navigated to: {new_url[:50]}...')
                            apply_clicked = True
                            break
                        elif await modal.count() > 0:
                            print(f"[Workday] Modal appeared", flush=True)
                            if progress:
                                progress.log('Application modal appeared')
                            apply_clicked = True
                            break
                        else:
                            # Wait longer for modal
                            try:
                                await page.wait_for_selector("[role='dialog']", timeout=5000)
                                print(f"[Workday] Modal appeared after wait", flush=True)
                                if progress:
                                    progress.log('Application modal appeared')
                                apply_clicked = True
                                break
                            except:
                                print(f"[Workday] No modal or URL change after click", flush=True)
                except Exception as e:
                    print(f"[Workday] Apply button error ({selector}): {e}", flush=True)
                    continue

            if not apply_clicked:
                print("[Workday] WARNING: Apply button click may have failed", flush=True)
                # Check if we're already on an apply page (URL contains /apply)
                current_url = page.url.lower()
                if '/apply' in current_url:
                    print("[Workday] Already on application page", flush=True)
                    if progress:
                        progress.log('Already on application page')
                        progress.skip_stage('apply', 'Already on apply page')
                        progress.start_stage('modal', 'Checking for application modal...')
                else:
                    print("[Workday] ERROR: Not on application page and Apply button failed", flush=True)
                    if progress:
                        progress.log('ERROR: Could not navigate to application form')
                        progress.fail_stage('apply', 'Could not click Apply button')
                    return False
            else:
                if progress:
                    progress.log('Apply button clicked successfully')
                    progress.complete_stage('apply', 'Apply button clicked')
                    progress.start_stage('modal', 'Handling application options...')

            # ============================================================
            # STEP 2: Handle "Start Your Application" Modal
            # ============================================================
            modal_options = [
                # Button selectors (Workday uses buttons in the modal)
                ("button:has-text('Use My Last Application')", "Use My Last Application"),
                ("button:has-text('Autofill with Resume')", "Autofill with Resume"),
                ("button:has-text('Apply Manually')", "Apply Manually"),
                # Fallback to anchor tags
                ("a:has-text('Use My Last Application')", "Use My Last Application"),
                ("a:has-text('Autofill with Resume')", "Autofill with Resume"),
                ("a:has-text('Apply Manually')", "Apply Manually"),
                # Data automation ID selectors
                ("[data-automation-id='useMyLastApplication']", "Use My Last Application"),
                ("[data-automation-id='autofillWithResume']", "Autofill with Resume"),
            ]

            modal_clicked = False
            # Wait for modal content to be fully loaded
            await asyncio.sleep(1)

            if progress:
                progress.log('Step 2: Looking for application modal options...')

            for selector, option_name in modal_options:
                try:
                    option_btn = page.locator(selector)
                    if await option_btn.count() > 0 and await option_btn.first.is_visible():
                        print(f"[Workday] Step 2: Clicking modal option '{option_name}'", flush=True)
                        if progress:
                            progress.log(f'Found modal option: {option_name}')
                        await option_btn.first.click()
                        await page.wait_for_load_state("networkidle")
                        await asyncio.sleep(3)
                        modal_clicked = True
                        print(f"[Workday] After modal, URL: {page.url}", flush=True)
                        break
                except Exception as e:
                    continue

            if not modal_clicked:
                # Check if we're already past the modal (direct to login or form)
                current_url = page.url.lower()
                if '/apply' in current_url or 'myinformation' in current_url:
                    print("[Workday] Already on application page, skipping modal", flush=True)
                    if progress:
                        progress.skip_stage('modal', 'Already on application page')
                else:
                    print("[Workday] No modal found - checking if already logged in", flush=True)
                    if progress:
                        progress.skip_stage('modal', 'No modal found')
            else:
                if progress:
                    progress.complete_stage('modal', 'Application option selected')

            # ============================================================
            # STEP 3: Handle Sign In if required
            # ============================================================
            if progress:
                progress.start_stage('login', 'Checking login requirements...')
                progress.log(f'Step 3: Checking for login page...')
                progress.log(f'Current URL: {page.url}')

            print(f"[Workday] Step 3: Checking if login is required...", flush=True)
            print(f"[Workday] Current URL: {page.url}", flush=True)

            # Take a snapshot of what's visible on the page
            try:
                page_title = await page.title()
                print(f"[Workday] Page title: {page_title}")
            except:
                pass

            # AutoApply never signs in to Workday (or anywhere else) on the
            # user's behalf. If a sign-in wall is shown, stop and hand over.
            if await self._is_on_signin_page(page):
                print("[Workday] Sign-in required - stopping for manual sign in", flush=True)
                self.signin_required = True
                if progress:
                    progress.fail_stage('login', 'Sign-in required: please sign in manually and finish the application')
                return False

            if progress:
                progress.skip_stage('login', 'No login required')
                progress.start_stage('form', 'Navigating form steps...')

            # ============================================================
            # STEP 4: Navigate through multi-step application form
            # ============================================================
            if progress:
                progress.log('Step 4: Navigating through form steps...')

            navigation_complete = await self._navigate_workday_steps(page, resume, resume_pdf, cover_letter_pdf)

            if navigation_complete:
                # Application was submitted during navigation (detected confirmation page)
                print("[Workday] Application submitted during navigation!", flush=True)
                if progress:
                    progress.complete_stage('form', 'Form navigation complete')
                    progress.complete_stage('fill', 'Form fields filled')
                    progress.complete_stage('upload', 'Resume uploaded')
                    progress.complete_stage('submit', 'Application submitted!')
                return True

            if progress:
                progress.complete_stage('form', 'Form navigation complete')

            # Step 5: Fill any remaining form fields on the current page
            filled_count = await self._fill_workday_fields(page, resume, resume_pdf, first_name, last_name)

            print(f"[Workday] Filled {filled_count} fields", flush=True)
            return filled_count > 0

        except Exception as e:
            print(f"[Workday] Form fill error: {e}", flush=True)
            if progress:
                progress.log(f'Workday error: {str(e)}')
            import traceback
            traceback.print_exc()
            return False

    async def _is_on_signin_page(self, page: Page) -> bool:
        """Check if we're still on a Workday sign-in page.

        IMPORTANT: This must distinguish between:
        - Sign-in page (needs login) - has Sign In buttons
        - Application form page (has form fields like firstName, lastName)

        Sign-in page indicators (check these FIRST):
        - "Sign in with email" button
        - "Sign in with Google" button
        - Password field visible
        """
        try:
            # Check for sign-in specific elements FIRST
            # These buttons ONLY appear on login pages
            signin_buttons = [
                "button:has-text('Sign in with email')",
                "button:has-text('Sign in with Google')",
                "[data-automation-id='signInWithEmail']",
                "[data-automation-id='signInWithGoogle']",
            ]

            for selector in signin_buttons:
                try:
                    elem = page.locator(selector)
                    if await elem.count() > 0 and await elem.first.is_visible():
                        print(f"[Workday] Sign-in button found: {selector}", flush=True)
                        return True
                except:
                    continue

            # Check for visible password field (only on login forms)
            password_field = page.locator("input[type='password']:visible")
            if await password_field.count() > 0:
                print("[Workday] Password field visible - on sign-in page", flush=True)
                return True

            # Check for "Sign In" heading (h1, h2, or prominent text)
            sign_in_heading = page.locator("h1:has-text('Sign In'), h2:has-text('Sign In'), [role='heading']:has-text('Sign In')")
            if await sign_in_heading.count() > 0:
                print("[Workday] Sign In heading found", flush=True)
                return True

            # Now check if we're on the application form (means NOT on sign-in)
            # These elements only appear on actual form pages, not sign-in
            form_fields = [
                "[data-automation-id='legalNameSection_firstName']",
                "[data-automation-id='legalNameSection_lastName']",
                "[data-automation-id='file-upload-drop-zone']",
                "[data-automation-id='resumeSection']",
            ]

            for selector in form_fields:
                try:
                    elem = page.locator(selector)
                    if await elem.count() > 0 and await elem.first.is_visible():
                        print(f"[Workday] Application form field found: {selector} - not on sign-in")
                        return False
                except:
                    continue

            # Check URL patterns as fallback
            url = page.url.lower()
            if 'signin' in url or 'login' in url or 'createaccount' in url:
                print(f"[Workday] Sign-in URL pattern: {url}")
                return True

            # If we got here, probably not on sign-in page
            print("[Workday] No sign-in indicators found")
            return False

        except Exception as e:
            print(f"[Workday] Error checking sign-in page: {e}")
            return False

    async def _navigate_workday_steps(self, page: Page, resume: BaseResume, resume_pdf: Path, cover_letter_pdf: Optional[Path] = None) -> bool:
        """Navigate through Workday's multi-step application process, filling fields on each step.

        Returns True if application was submitted, False otherwise.
        """
        max_steps = 10  # Safety limit
        step_count = 0
        fields_filled_total = 0
        last_step_title = ""

        name_parts = resume.full_name.split()
        first_name = name_parts[0]
        last_name = name_parts[-1] if len(name_parts) > 1 else ""

        try:
            while step_count < max_steps:
                step_count += 1

                # Wait for page to load
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass
                await asyncio.sleep(2)

                print(f"[Workday] === Step {step_count} ===", flush=True)

                # Get current step title from the active step in progress indicator
                current_step_title = ""
                try:
                    # Workday shows active step with specific styling
                    active_step = page.locator("[data-automation-id='activeStep'], .css-1wc0u8l, [aria-current='step']")
                    if await active_step.count() > 0:
                        current_step_title = await active_step.first.inner_text()
                except:
                    pass

                # Also try getting the main heading
                if not current_step_title:
                    try:
                        heading = page.locator("h2, [data-automation-id='pageHeaderTitle']").first
                        if await heading.count() > 0:
                            current_step_title = await heading.inner_text()
                    except:
                        pass

                print(f"[Workday] Current step: {current_step_title}", flush=True)

                # Check if we're stuck on the same step (SPA not advancing)
                if current_step_title and current_step_title == last_step_title:
                    print(f"[Workday] Same step as before - checking if stuck", flush=True)
                    # Give it one more try, but if still same, we may be at the end
                    await asyncio.sleep(2)

                last_step_title = current_step_title

                # Check if we're on confirmation/thank you page
                try:
                    page_text = await page.inner_text("body")
                    page_text_lower = page_text.lower()
                    if any(phrase in page_text_lower for phrase in [
                        "thank you", "application submitted", "application received",
                        "successfully submitted", "application complete"
                    ]):
                        print("[Workday] SUCCESS! Application submitted!", flush=True)
                        return True
                except:
                    page_text_lower = ""

                # Check if we're on Review page (only check step title, not full page text)
                if current_step_title and "review" in current_step_title.lower():
                    print("[Workday] On Review page - looking for Submit button", flush=True)

                print(f"[Workday] Detected step: {current_step_title or 'Unknown'}", flush=True)

                # ============ Fill fields based on step ============

                fields_filled_this_step = 0

                # Upload resume (commonly on My Experience step)
                # Always try to upload our resume, even if one exists from "Use My Last Application"
                upload_zone = page.locator("[data-automation-id='file-upload-drop-zone']")

                # Check if there's an existing file we need to delete first
                if await upload_zone.count() > 0:
                    try:
                        zone_text = await upload_zone.first.inner_text()
                        if ".pdf" in zone_text.lower() or ".doc" in zone_text.lower():
                            # Check if it's already our file (same name)
                            resume_name = resume_pdf.name if resume_pdf else ""
                            if resume_name and resume_name.lower() in zone_text.lower():
                                print(f"[Workday] Correct resume already uploaded: {zone_text[:60]}", flush=True)
                            else:
                                # Delete the old file first to upload our new one
                                print(f"[Workday] Found old resume, deleting to upload new one: {zone_text[:60]}", flush=True)
                                delete_btn = page.locator("button:has-text('Delete'), [data-automation-id*='delete']")
                                if await delete_btn.count() > 0:
                                    await delete_btn.first.click()
                                    await asyncio.sleep(1)
                                    print(f"[Workday] Deleted old resume", flush=True)
                    except Exception as e:
                        print(f"[Workday] Error checking existing file: {e}", flush=True)

                if resume_pdf:
                    print(f"[Workday] Attempting to upload resume: {resume_pdf}", flush=True)
                    upload_success = False

                    # Method 1: Try clicking "Select files" button and use file chooser
                    try:
                        select_files_btn = page.get_by_role("button", name="Select files")
                        if await select_files_btn.count() > 0 and await select_files_btn.first.is_visible():
                            print(f"[Workday] Found 'Select files' button, using file chooser", flush=True)
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await select_files_btn.first.click()
                            file_chooser = await fc_info.value
                            await file_chooser.set_files(str(resume_pdf))
                            upload_success = True
                            print(f"[Workday] Resume uploaded via file chooser", flush=True)
                    except Exception as e:
                        print(f"[Workday] File chooser method failed: {e}", flush=True)

                    # Method 2: Try direct file input selectors
                    if not upload_success:
                        file_input_selectors = [
                            "[data-automation-id='file-upload-input-ref']",
                            "input[type='file'][data-automation-id]",
                            "input[type='file']",
                        ]

                        for file_selector in file_input_selectors:
                            try:
                                resume_upload = page.locator(file_selector)
                                count = await resume_upload.count()
                                print(f"[Workday] Checking file input '{file_selector}': {count} found", flush=True)

                                if count > 0:
                                    await resume_upload.first.set_input_files(str(resume_pdf))
                                    upload_success = True
                                    print(f"[Workday] Resume uploaded via file input: {file_selector}", flush=True)
                                    break
                            except Exception as e:
                                print(f"[Workday] File input error ({file_selector}): {e}", flush=True)
                                continue

                    if upload_success:
                        fields_filled_this_step += 1
                        await asyncio.sleep(3)  # Wait for upload to complete

                        # Verify upload succeeded
                        try:
                            if await upload_zone.count() > 0:
                                zone_text = await upload_zone.first.inner_text()
                                if "successfully" in zone_text.lower() or ".pdf" in zone_text.lower() or ".doc" in zone_text.lower():
                                    print(f"[Workday] Resume upload confirmed: {zone_text[:60]}", flush=True)
                                else:
                                    print(f"[Workday] Upload zone text: {zone_text[:60]}", flush=True)
                        except:
                            pass
                    else:
                        print(f"[Workday] WARNING: Could not upload resume", flush=True)

                # Upload cover letter if provided
                if cover_letter_pdf and cover_letter_pdf.exists():
                    print(f"[Workday] Attempting to upload cover letter: {cover_letter_pdf}", flush=True)
                    cl_upload_success = False

                    # Method 1: Try clicking "Select files" button again for cover letter
                    try:
                        select_files_btn = page.get_by_role("button", name="Select files")
                        if await select_files_btn.count() > 0 and await select_files_btn.first.is_visible():
                            print(f"[Workday] Found 'Select files' button for cover letter", flush=True)
                            async with page.expect_file_chooser(timeout=5000) as fc_info:
                                await select_files_btn.first.click()
                            file_chooser = await fc_info.value
                            await file_chooser.set_files(str(cover_letter_pdf))
                            cl_upload_success = True
                            print(f"[Workday] Cover letter uploaded via file chooser", flush=True)
                    except Exception as e:
                        print(f"[Workday] Cover letter file chooser method failed: {e}", flush=True)

                    # Method 2: Try direct file input selectors
                    if not cl_upload_success:
                        file_input_selectors = [
                            "[data-automation-id='file-upload-input-ref']",
                            "input[type='file'][data-automation-id]",
                            "input[type='file']",
                        ]

                        for file_selector in file_input_selectors:
                            try:
                                cl_upload = page.locator(file_selector)
                                count = await cl_upload.count()
                                if count > 0:
                                    await cl_upload.first.set_input_files(str(cover_letter_pdf))
                                    cl_upload_success = True
                                    print(f"[Workday] Cover letter uploaded via file input: {file_selector}", flush=True)
                                    break
                            except Exception as e:
                                print(f"[Workday] Cover letter input error ({file_selector}): {e}", flush=True)
                                continue

                    if cl_upload_success:
                        fields_filled_this_step += 1
                        await asyncio.sleep(3)  # Wait for upload to complete
                        print(f"[Workday] Cover letter upload complete", flush=True)
                    else:
                        print(f"[Workday] WARNING: Could not upload cover letter", flush=True)

                # Personal Information fields
                personal_fields = [
                    ("[data-automation-id='legalNameSection_firstName']", first_name),
                    ("[data-automation-id='legalNameSection_lastName']", last_name),
                    ("[data-automation-id='email']", resume.email),
                    ("[data-automation-id='phone-number']", resume.phone),
                    ("[data-automation-id='addressSection_addressLine1']", self.personal_info.get('address', '')),
                    ("[data-automation-id='addressSection_city']", self.personal_info.get('city', '')),
                    ("[data-automation-id='addressSection_postalCode']", self.personal_info.get('zip_code', '')),
                ]

                for selector, value in personal_fields:
                    if value:
                        try:
                            field = page.locator(selector)
                            if await field.count() > 0 and await field.first.is_visible():
                                # Check if field is empty or has placeholder
                                current_value = await field.first.input_value()
                                if not current_value or current_value.strip() == "":
                                    await field.first.fill(value)
                                    print(f"[Workday] Filled: {selector}")
                                    fields_filled_this_step += 1
                        except:
                            pass

                # Dropdowns are only filled from values the user saved in their
                # profile. Nothing is guessed (no default country, phone type or
                # "How did you hear about us" answer).
                dropdown_fields = [
                    ("[data-automation-id='phone-device-type']", self.personal_info.get('phone_type', '')),
                    ("[data-automation-id='countryDropdown']", self.personal_info.get('country', '')),
                    ("[data-automation-id='addressSection_countryRegion']", self.personal_info.get('country', '')),
                ]
                dropdown_fields = [(sel, val) for sel, val in dropdown_fields if val]

                for selector, value in dropdown_fields:
                    try:
                        dropdown = page.locator(selector)
                        if await dropdown.count() > 0 and await dropdown.first.is_visible():
                            await dropdown.first.click()
                            await asyncio.sleep(0.5)
                            option = page.locator(f"[data-automation-id='promptOption']:has-text('{value}')")
                            if await option.count() > 0:
                                await option.first.click()
                                print(f"[Workday] Selected dropdown: {value}")
                                fields_filled_this_step += 1
                            await asyncio.sleep(0.5)
                    except:
                        pass

                # Terms/consent checkboxes are never ticked automatically: the
                # user must review and accept them personally.

                fields_filled_total += fields_filled_this_step
                print(f"[Workday] Fields filled this step: {fields_filled_this_step}")

                # ============ Navigate to next step ============

                # Check for Submit button (final step - Review page)
                # Use role-based selector first (most reliable)
                try:
                    submit_btn = page.get_by_role("button", name="Submit", exact=True)
                    if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                        print(f"[Workday] Found Submit button on Review page!", flush=True)
                        # Don't click it here - let _submit_application handle it
                        print(f"[Workday] Total fields filled: {fields_filled_total}", flush=True)
                        return False
                except:
                    pass

                # Fallback submit button selectors
                submit_selectors = [
                    "[data-automation-id='submitButton']",
                    "button:has-text('Submit Application'):visible",
                    "button:has-text('Submit'):visible:not(:has-text('Save'))",
                ]

                for selector in submit_selectors:
                    try:
                        submit_btn = page.locator(selector)
                        if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                            print(f"[Workday] Found Submit button: {selector}", flush=True)
                            print(f"[Workday] Total fields filled: {fields_filled_total}", flush=True)
                            return False
                    except:
                        continue

                # Check for validation errors before trying to navigate
                validation_error_selectors = [
                    "[data-automation-id='errorMessageContainer']",
                    ".wd-FormError",
                    "[role='alert']",
                    ".error-message",
                    "[data-automation-id*='error']",
                ]

                for err_selector in validation_error_selectors:
                    try:
                        error_elem = page.locator(err_selector)
                        if await error_elem.count() > 0 and await error_elem.first.is_visible():
                            error_text = await error_elem.first.inner_text()
                            if error_text.strip():
                                print(f"[Workday] Validation error found: {error_text}", flush=True)
                    except:
                        pass

                # Look for Next/Continue/Save & Continue button
                # IMPORTANT: Workday uses click_filter overlays that intercept clicks
                next_btn_selectors = [
                    # Workday navigation button
                    "[data-automation-id='bottom-navigation-next-button']",
                    # Fallback selectors
                    "button:has-text('Save and Continue')",
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "button:has-text('Save & Continue')",
                ]

                # Get page content hash before clicking to detect change
                content_before = ""
                url_before = page.url
                try:
                    content_before = await page.locator("main, [role='main'], .wd-Application").first.inner_text()
                except:
                    pass

                next_clicked = False
                for selector in next_btn_selectors:
                    try:
                        nav_btn = page.locator(selector)
                        count = await nav_btn.count()
                        print(f"[Workday] Checking selector '{selector}': {count} found", flush=True)

                        if count > 0 and await nav_btn.first.is_visible():
                            btn_text = ""
                            try:
                                btn_text = await nav_btn.first.inner_text()
                            except:
                                btn_text = selector

                            print(f"[Workday] Found nav button: '{btn_text}'", flush=True)

                            # First scroll button into view
                            await nav_btn.first.scroll_into_view_if_needed()
                            await asyncio.sleep(0.5)

                            # Try multiple click strategies
                            clicked_successfully = False

                            # Strategy 1: Find ANY click_filter on the page footer
                            try:
                                footer_filter = page.locator("[data-automation-id='bottom-navigation'] [data-automation-id='click_filter']")
                                if await footer_filter.count() > 0:
                                    print(f"[Workday] Found footer click_filter, clicking it", flush=True)
                                    await footer_filter.first.click(timeout=5000)
                                    clicked_successfully = True
                            except Exception as e:
                                print(f"[Workday] Footer click_filter not found: {e}", flush=True)

                            # Strategy 2: Look for click_filter near button with xpath ancestor search
                            if not clicked_successfully:
                                try:
                                    # Check parent, grandparent, etc.
                                    for ancestor_level in ["/..", "/../..", "/../../.."]:
                                        ancestor_filter = nav_btn.first.locator(f"xpath={ancestor_level}//div[@data-automation-id='click_filter']")
                                        if await ancestor_filter.count() > 0:
                                            print(f"[Workday] Found click_filter at ancestor level, clicking", flush=True)
                                            await ancestor_filter.first.click(timeout=5000)
                                            clicked_successfully = True
                                            break
                                except Exception as e:
                                    pass

                            # Strategy 3: Direct click with force
                            if not clicked_successfully:
                                print(f"[Workday] Trying direct click with force", flush=True)
                                try:
                                    await nav_btn.first.click(force=True, timeout=5000)
                                    clicked_successfully = True
                                except Exception as e:
                                    print(f"[Workday] Force click failed: {e}", flush=True)

                            # Strategy 4: JavaScript click
                            if not clicked_successfully:
                                print(f"[Workday] Trying JavaScript click", flush=True)
                                try:
                                    await nav_btn.first.evaluate("el => el.click()")
                                    clicked_successfully = True
                                except Exception as e:
                                    print(f"[Workday] JS click failed: {e}", flush=True)

                            # Strategy 5: Dispatch click event
                            if not clicked_successfully:
                                print(f"[Workday] Trying dispatchEvent", flush=True)
                                try:
                                    await nav_btn.first.evaluate("""el => {
                                        el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
                                    }""")
                                    clicked_successfully = True
                                except:
                                    pass

                            next_clicked = clicked_successfully

                            # Wait longer for SPA to update
                            print(f"[Workday] Waiting for page transition...", flush=True)
                            await asyncio.sleep(4)

                            # Check if content actually changed
                            content_after = ""
                            url_after = page.url
                            try:
                                content_after = await page.locator("main, [role='main'], .wd-Application").first.inner_text()
                            except:
                                pass

                            if url_after != url_before:
                                print(f"[Workday] URL changed from {url_before} to {url_after}", flush=True)
                            elif content_after and content_after != content_before:
                                print(f"[Workday] Page content changed - navigation successful", flush=True)
                            else:
                                print(f"[Workday] Page content may not have changed - checking for errors", flush=True)
                                # Check if there are now new validation errors
                                for err_selector in validation_error_selectors:
                                    try:
                                        error_elem = page.locator(err_selector)
                                        if await error_elem.count() > 0 and await error_elem.first.is_visible():
                                            error_text = await error_elem.first.inner_text()
                                            if error_text.strip():
                                                print(f"[Workday] NEW validation error: {error_text}", flush=True)
                                    except:
                                        pass

                            break
                    except Exception as e:
                        print(f"[Workday] Error with {selector}: {e}", flush=True)
                        continue

                if not next_clicked:
                    print("[Workday] No navigation button found - may be final step", flush=True)
                    break

            print(f"[Workday] Navigation complete. Total fields filled: {fields_filled_total}")
            return False

        except Exception as e:
            print(f"[Workday] Navigation error: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def _fill_workday_fields(self, page: Page, resume: BaseResume, resume_pdf: Path, first_name: str, last_name: str) -> int:
        """Fill Workday form fields."""
        filled_count = 0

        try:
            # Upload resume first (often auto-fills other fields in Workday)
            resume_selectors = [
                "[data-automation-id='file-upload-input-ref']",
                "input[type='file'][data-automation-id*='file']",
                "input[type='file']",
            ]

            for selector in resume_selectors:
                try:
                    resume_input = page.locator(selector)
                    if await resume_input.count() > 0:
                        print(f"[Workday] Uploading resume with: {selector}")
                        await resume_input.first.set_input_files(str(resume_pdf))
                        filled_count += 1
                        await asyncio.sleep(2)
                        break
                except Exception as e:
                    print(f"[Workday] Resume upload error: {e}")
                    continue

            # Fill form fields - Workday uses data-automation-id
            workday_fields = [
                ("[data-automation-id='legalNameSection_firstName'], [data-automation-id='firstName']", first_name),
                ("[data-automation-id='legalNameSection_lastName'], [data-automation-id='lastName']", last_name),
                ("[data-automation-id='email'], input[type='email']", resume.email),
                ("[data-automation-id='phone-number'], [data-automation-id='phone'], input[type='tel']", resume.phone),
                ("[data-automation-id='addressSection_addressLine1']", resume.location.split(',')[0] if resume.location else ""),
            ]

            for selector, value in workday_fields:
                if value and await self._fill_field(page, selector, value):
                    print(f"[Workday] Filled field: {selector}")
                    filled_count += 1

            # Try label-based filling as fallback
            if await self._fill_by_label_text(page, "First Name", first_name):
                filled_count += 1
            if await self._fill_by_label_text(page, "Last Name", last_name):
                filled_count += 1
            if await self._fill_by_label_text(page, "Email", resume.email):
                filled_count += 1
            if await self._fill_by_label_text(page, "Phone", resume.phone):
                filled_count += 1

            # Also fill from personal_info
            address = self.personal_info.get('address', '')
            city = self.personal_info.get('city', '')
            state = self.personal_info.get('state', '')
            zip_code = self.personal_info.get('zip_code', '')

            if address:
                await self._fill_by_label_text(page, "Address", address)
            if city:
                await self._fill_by_label_text(page, "City", city)
            if state:
                await self._fill_by_label_text(page, "State", state)
            if zip_code:
                await self._fill_by_label_text(page, "Postal", zip_code)
                await self._fill_by_label_text(page, "Zip", zip_code)

        except Exception as e:
            print(f"[Workday] Field fill error: {e}")

        return filled_count

    async def _fill_by_label_text(self, page: Page, label_text: str, value: str) -> bool:
        """Helper to fill a field by finding its label."""
        if not value:
            return False
        try:
            labels = await page.query_selector_all("label")
            for label in labels:
                text = await label.inner_text()
                if label_text.lower() in text.lower():
                    for_id = await label.get_attribute("for")
                    if for_id:
                        inp = page.locator(f"[id='{for_id}']")
                        if await inp.count() > 0 and await inp.first.is_visible():
                            await inp.first.fill(value)
                            return True
            return False
        except Exception:
            return False

    async def _click_apply_with_new_tab_handling(self, page: Page, selectors: list) -> Page:
        """
        Click Apply button and handle case where it opens a new tab/popup.
        Returns the page to use for form filling (either new tab or original page).
        """
        context = page.context

        # Try each selector
        for selector in selectors:
            try:
                apply_btn = page.locator(selector)
                count = await apply_btn.count()
                if count > 0:
                    for i in range(count):
                        btn = apply_btn.nth(i)
                        if await btn.is_visible():
                            print(f"[NewTab] Clicking Apply button: {selector} (index {i})")

                            # Set up listener for new page/popup BEFORE clicking
                            try:
                                # Use expect_page to wait for a new tab
                                async with context.expect_page(timeout=5000) as new_page_info:
                                    await btn.click()

                                new_page = await new_page_info.value
                                print(f"[NewTab] New tab opened: {new_page.url}")
                                await new_page.wait_for_load_state("networkidle")
                                await asyncio.sleep(2)
                                return new_page

                            except Exception as e:
                                # No new tab opened, continue on same page
                                print(f"[NewTab] No new tab (staying on same page): {e}")
                                await page.wait_for_load_state("networkidle")
                                await asyncio.sleep(2)
                                return page
            except Exception as e:
                print(f"[NewTab] Error with selector {selector}: {e}")
                continue

        # No Apply button found, return original page
        print("[NewTab] No Apply button found")
        return page

    async def _fill_generic_form(
        self,
        page: Page,
        resume: BaseResume,
        resume_pdf: Path,
        cover_letter_pdf: Optional[Path],
    ) -> bool:
        """Generic form filler for unknown ATS platforms."""
        try:
            print(f"[Generic] Starting on URL: {page.url}")

            # Step 1: Click Apply button to get to the application form
            # This handles the case where Apply opens a new tab
            apply_button_selectors = [
                # AMD specific
                "a:has-text('Apply'):not(:has-text('Not ready'))",
                "button:has-text('Apply'):not(:has-text('Not ready'))",
                "[data-ph-id*='apply']",  # Phenom People (AMD uses this)
                ".apply-button",
                "#apply-button",
                # Generic
                "a:has-text('Apply Now')",
                "button:has-text('Apply Now')",
                "a:has-text('Apply for this job')",
                "button:has-text('Apply for this job')",
                "[class*='apply-btn']",
                "[class*='applyBtn']",
                "a[href*='/apply']",
            ]

            # Use the new tab handling helper
            page = await self._click_apply_with_new_tab_handling(page, apply_button_selectors)
            print(f"[Generic] Now on page: {page.url}")

            # Step 2: Handle multi-step application processes (like BMC's ApplicationMethods page)
            # Look for "From Device", "Upload Resume", or similar upload method options
            upload_method_selectors = [
                "a:has-text('From Device')",
                "button:has-text('From Device')",
                "a:has-text('Upload Resume')",
                "button:has-text('Upload Resume')",
                "a:has-text('Upload CV')",
                "button:has-text('Upload CV')",
            ]

            clicked_upload_method = False
            for selector in upload_method_selectors:
                try:
                    method_btn = page.locator(selector)
                    if await method_btn.count() > 0 and await method_btn.first.is_visible():
                        await method_btn.first.click()
                        await asyncio.sleep(1)
                        clicked_upload_method = True
                        break
                except Exception:
                    continue

            # Step 3: If we're on an intermediate page, upload resume and click Continue
            file_input = page.locator("input[type='file']")
            if clicked_upload_method and await file_input.count() > 0:
                # Upload resume
                await file_input.first.set_input_files(str(resume_pdf))
                await asyncio.sleep(1)

                # Look for Continue/Submit button to proceed to actual form
                # Only navigation buttons - never anything that could submit.
                continue_selectors = [
                    "#uploadFileResume",  # BMC specific
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                ]

                for selector in continue_selectors:
                    try:
                        cont_btn = page.locator(selector)
                        if await cont_btn.count() > 0 and await cont_btn.first.is_visible():
                            await cont_btn.first.click()
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(3)  # Wait for form to load
                            break
                    except Exception:
                        continue

            # Step 4: Now we should be on the actual application form - fill fields
            name_parts = resume.full_name.split()
            first_name = name_parts[0]
            last_name = name_parts[-1] if len(name_parts) > 1 else ""
            middle_name = " ".join(name_parts[1:-1]) if len(name_parts) > 2 else ""

            # Fill fields by finding labels and their associated inputs
            filled_count = 0

            # Helper to fill field by label text
            async def fill_by_label(label_text: str, value: str) -> bool:
                if not value:
                    return False
                try:
                    # Find label containing the text
                    labels = await page.query_selector_all("label")
                    for label in labels:
                        text = await label.inner_text()
                        if label_text.lower() in text.lower():
                            # Get the 'for' attribute to find associated input
                            for_id = await label.get_attribute("for")
                            if for_id:
                                # Use attribute selector for numeric IDs (CSS doesn't allow #123)
                                inp = page.locator(f"[id='{for_id}']")
                                if await inp.count() > 0 and await inp.first.is_visible():
                                    await inp.first.fill(value)
                                    return True
                    return False
                except Exception:
                    return False

            # Fill personal info by label
            if await fill_by_label("Full name", resume.full_name):
                filled_count += 1
            if await fill_by_label("First name", first_name):
                filled_count += 1
            if await fill_by_label("Middle name", middle_name):
                filled_count += 1
            if await fill_by_label("Last name", last_name):
                filled_count += 1
            if await fill_by_label("Email", resume.email):
                filled_count += 1
            if await fill_by_label("Confirm your email", resume.email):
                filled_count += 1
            if await fill_by_label("Phone", resume.phone):
                filled_count += 1

            # Also try standard selectors as fallback
            field_mappings = [
                (["input[type='email']", "input[name*='email']"], resume.email),
                (["input[type='tel']", "input[name*='phone']"], resume.phone),
                (["input[name*='first']", "input[placeholder*='First']"], first_name),
                (["input[name*='last']", "input[placeholder*='Last']"], last_name),
            ]

            for selectors, value in field_mappings:
                if not value:
                    continue
                for selector in selectors:
                    try:
                        field = page.locator(selector)
                        if await field.count() > 0:
                            first_field = field.first
                            if await first_field.is_visible():
                                current_val = await first_field.input_value()
                                if not current_val:  # Only fill if empty
                                    await first_field.fill(value)
                                    filled_count += 1
                                break
                    except Exception:
                        continue

            # Upload resume if there's a file input on this page (not already uploaded)
            file_inputs = page.locator("input[type='file']")
            if await file_inputs.count() > 0:
                try:
                    await file_inputs.first.set_input_files(str(resume_pdf))
                    filled_count += 1
                except Exception:
                    pass

            # Return true if we managed to fill at least some fields
            return filled_count > 0

        except Exception:
            return False

    async def _fill_field(self, page: Page, selector: str, value: str) -> bool:
        """Fill a form field with error handling."""
        try:
            field = page.locator(selector)
            if await field.count() > 0 and await field.first.is_visible():
                await field.first.fill(value)
                return True
        except Exception:
            pass
        return False

    async def _submit_application(self, page: Page, platform: ATSPlatform) -> bool:
        """Attempt to submit the application."""
        try:
            print(f"[Submit] Looking for submit button on page: {page.url}", flush=True)

            # First try role-based selector (most reliable for Workday)
            submit_clicked = False
            try:
                submit_btn = page.get_by_role("button", name="Submit", exact=True)
                if await submit_btn.count() > 0 and await submit_btn.first.is_visible():
                    print(f"[Submit] Found Submit button via role selector", flush=True)
                    await submit_btn.first.click()
                    submit_clicked = True
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"[Submit] Role selector failed: {e}", flush=True)

            # Fallback to CSS selectors if role-based didn't work
            if not submit_clicked:
                submit_selectors = [
                    # Workday specific
                    "button:has-text('Submit'):not(:has-text('Submit Application'))",
                    "[data-automation-id='bottom-navigation-next-button']",
                    "[data-automation-id='submitButton']",
                    # Generic submit buttons
                    "button[type='submit']:has-text('Submit')",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "input[type='submit']",
                    "button[type='submit']",
                ]

                for selector in submit_selectors:
                    try:
                        btn = page.locator(selector)
                        if await btn.count() > 0 and await btn.first.is_visible():
                            print(f"[Submit] Clicking submit button: {selector}", flush=True)
                            # Try multiple click methods
                            try:
                                await btn.first.click(timeout=5000)
                                submit_clicked = True
                            except:
                                try:
                                    await btn.first.evaluate("el => el.click()")
                                    submit_clicked = True
                                except:
                                    await btn.first.click(force=True)
                                    submit_clicked = True
                            await asyncio.sleep(3)
                            break
                    except Exception as e:
                        print(f"[Submit] Error clicking {selector}: {e}", flush=True)
                        continue

            if not submit_clicked:
                print("[Submit] No submit button found", flush=True)
                return False

            # Check for success indicators - must find at least one to confirm submission
            success_indicators = [
                "text=Thank you",
                "text=thank you",
                "text=Application submitted",
                "text=application submitted",
                "text=Successfully submitted",
                "text=successfully submitted",
                "text=received your application",
                "text=Application received",
                "text=application received",
                "text=You have applied",
                "text=you have applied",
                "text=Application complete",
                "text=application complete",
            ]

            for indicator in success_indicators:
                try:
                    if await page.locator(indicator).count() > 0:
                        print(f"[Submit] Success indicator found: {indicator}")
                        return True
                except Exception:
                    continue

            # Also check page title/URL for success indicators
            page_content = await page.content()
            page_content_lower = page_content.lower()
            success_texts = ["thank you", "application submitted", "successfully applied", "application received", "application complete"]
            for text in success_texts:
                if text in page_content_lower:
                    print(f"[Submit] Success text found in page: {text}")
                    return True

            print("[Submit] No success indicator found - submission may have failed")
            return False

        except Exception as e:
            print(f"[Submit] Error: {e}")
            return False

    async def check_for_captcha(self, page: Page) -> bool:
        """Check if a CAPTCHA is present."""
        captcha_selectors = [
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".g-recaptcha",
            ".h-captcha",
            "[data-captcha]",
        ]

        for selector in captcha_selectors:
            if await page.locator(selector).count() > 0:
                return True

        return False

    async def wait_for_manual_captcha_solve(self, page: Page, timeout: int = 120):
        """Wait for user to solve CAPTCHA manually."""
        start_time = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start_time < timeout:
            if not await self.check_for_captcha(page):
                return True
            await asyncio.sleep(2)

        return False
