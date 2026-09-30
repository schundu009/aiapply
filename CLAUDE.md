# AutoApply - Claude Memory & Context

## Project Overview
AutoApply is an AI-powered job application automation platform that:
1. Fetches job descriptions from URLs
2. Generates tailored resumes and cover letters using LLM
3. Auto-fills job application forms using browser automation (Playwright)
4. Tracks application history and sends email notifications

## Architecture

### Core Components
```
autoapply/
├── app.py                 # Flask web server (port 5001)
├── src/
│   ├── browser_automation.py  # Playwright-based form filling
│   ├── form_agent.py          # LLM-powered form field analysis
│   ├── job_fetcher.py         # Fetches job descriptions from URLs
│   ├── job_analyzer.py        # Analyzes job requirements
│   ├── resume_generator.py    # Generates tailored resumes
│   ├── pdf_generator.py       # Creates PDF documents
│   ├── email_notifier.py      # Sends email notifications
│   ├── models.py              # Pydantic data models
│   └── config.py              # Configuration settings
├── templates/                 # Jinja2 HTML templates
├── static/css/style.css       # Styling
├── data/
│   ├── settings.json          # API keys, SMTP config
│   ├── applications.json      # Saved applications
│   └── personal_info.json     # User profile data
└── uploads/                   # Uploaded resumes
```

### Key Data Models (src/models.py)
- `Application`: Main model tracking job applications
- `JobDescription`: Parsed job posting data
- `BaseResume`: User's base resume information
- `TailoredResume`: Customized resume for specific job
- `CoverLetter`: Generated cover letter
- `ATSPlatform`: Enum for ATS platforms (Greenhouse, Lever, Workday, etc.)

## Critical Patterns & Rules

### 0. Security rules (do not regress)
- Every route requires a Cariara admin JWT (`src/auth.py`); only `/healthz`, `/login`, `/logout` are public.
- Never store, request or use passwords for job sites/Google/LinkedIn/ATS; never create accounts or log in.
- No stealth: no AutomationControlled flag, UA spoofing, fake geolocation or human-like delays. Detect CAPTCHAs and stop.
- No hardcoded factual answers (authorization, sponsorship, relocation, consents, EEO, "how did you hear").
- The LLM (form_agent) only drafts free-text answers from non-sensitive profile fields + resume.
- auto_submit defaults to False.

### 1. Browser Automation (browser_automation.py)
**IMPORTANT - DO NOT:**
- Click "Create Account", "Sign In", "Sign Up" buttons - these navigate to login pages
- Try to auto-login with OAuth (Google, LinkedIn)
- Use complex iframe detection/handling
- Add multi-page form navigation EXCEPT for Next/Continue buttons

**DO:**
- Click "Apply" button ONLY to get from job description to application form
- Fill fields on the application form page
- Upload resume using file input selectors
- Use simple, direct selectors
- Keep methods simple and focused

**WORKFLOW:**
1. User provides job description URL
2. Browser navigates to that URL
3. Click "Apply" button to get to application form (Workday: `[data-automation-id='jobPostingApplyButton']`)
4. Fill form fields
5. Upload resume
6. Click submit

### 2. API Endpoints (app.py)
**All /api/ routes MUST return JSON:**
```python
# Use this pattern for all API routes
from flask import make_response, jsonify

def json_response(data, status=200):
    response = make_response(jsonify(data), status)
    response.headers['Content-Type'] = 'application/json'
    return response
```

**Handle JSON body parsing safely:**
```python
try:
    data = request.get_json(silent=True) or {}
except Exception:
    data = {}
```

### 3. Frontend API Calls
**Always include JSON body and check response:**
```javascript
const response = await fetch(`/api/apply/${appId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ headless: false, auto_submit: false })
});

// Check content-type before parsing
const contentType = response.headers.get('content-type');
if (!contentType || !contentType.includes('application/json')) {
    throw new Error('Server returned non-JSON response');
}
const data = await response.json();
```

## ATS Platform Handling

### Workday (*.myworkdayjobs.com)
- Uses `data-automation-id` attributes
- **AUTHENTICATION:** Never automated. No credentials are stored. If a sign-in page appears
  the run stops with status `signin_required` ("sign in manually").
- **WORKFLOW:**
  1. Navigate to job URL
  2. Click Apply button: `[data-automation-id='jobPostingApplyButton']`
  3. Handle modal: "Apply Manually" or "Apply with Resume"
  4. If sign-in page appears: STOP and ask the user to sign in manually
  5. Fill application form fields from the saved profile only
  6. Upload resume
  7. Stop at review (auto_submit defaults to False)
- **MULTI-STEP FLOW:**
  - `_fill_workday_form`: Main entry point
  - `_navigate_workday_steps`: Navigates multi-page forms
  - `_fill_workday_fields`: Fills individual fields
- **BROWSER BEHAVIOR:**
  - When `headless=false`: Browser stays open after automation for user review
  - When `headless=true`: Browser closes automatically after completion
- Common selectors:
  - `[data-automation-id='jobPostingApplyButton']` (initial Apply button)
  - `button:has-text('Apply Manually')` (modal option)
  - `[data-automation-id='signInSubmitButton']` (sign in button)
  - `[data-automation-id='legalNameSection_firstName']`
  - `[data-automation-id='email']`
  - `[data-automation-id='phone-number']`
  - `[data-automation-id='file-upload-input-ref']` (resume upload)
  - `[data-automation-id='bottom-navigation-next-button']` (submit/next)

### Greenhouse (boards.greenhouse.io)
- Standard HTML forms
- Uses `input[name='first_name']`, `input[name='last_name']`, etc.

### Lever (jobs.lever.co)
- Clean form structure
- Standard input types

### Generic Form Filling Strategy
1. Try direct selectors first
2. Try label-based filling (`label[for='id']`)
3. Try placeholder matching
4. Try name/id attribute matching

## Common Issues & Solutions

### Issue: "Unexpected token '<', is not valid JSON"
**Cause:** Server returning HTML error page instead of JSON
**Solution:**
1. Ensure all `/api/` routes have try-except returning JSON
2. Use global error handlers in app.py
3. Check frontend sends proper JSON body

### Issue: Browser opens new windows/login pages
**Cause:** Code clicking navigation buttons (Apply, Create Account)
**Solution:** Remove all navigation button clicking from form fillers. Only fill fields on current page.

### Issue: Browser closes immediately
**Cause:** `automation.stop()` called in finally block
**Solution:** Browser now stays open when `headless=false`:
- In visible mode: browser stays open for user review
- In headless mode: browser closes automatically

### Issue: Form fields not being filled
**Cause:** Selectors not matching
**Solution:** Use multiple selector strategies, check browser console for actual field attributes

### Issue: Workday requires sign in
AutoApply does not log in for users. The run stops with `signin_required`; the user signs in
and completes the application manually.

### Issue: Submit button not found
**Cause:** Platform-specific submit button patterns
**Solution:** Use comprehensive selector list:
```python
submit_selectors = [
    "[data-automation-id='bottom-navigation-next-button']",
    "[data-automation-id='submitButton']",
    "button[type='submit']:has-text('Submit')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "input[type='submit']",
]
```

## Testing Checklist

### Before Making Changes
1. Note what's currently working
2. Make minimal, focused changes
3. Test each change independently

### After Making Changes
1. Check Python syntax: `python3 -m py_compile <file>.py`
2. Restart Flask: `python3 app.py`
3. Test workflow end-to-end:
   - Submit a job URL
   - Generate resume/cover letter
   - Click "Apply" button
   - Verify form fills (not navigates away)
   - Check for JSON errors in browser console

### Manual Testing Flow
1. Go to http://localhost:5001
2. Enter job URL (e.g., Workday, Greenhouse, Lever URL)
3. Click "Create CV/CL"
4. Wait for documents to generate
5. Click "Apply" button
6. Watch browser automation
7. Check progress indicators

## Configuration

### Settings (data/settings.json)
```json
{
  "anthropic_key": "sk-ant-...",
  "smtp_host": "smtp.gmail.com",
  "smtp_port": "587",
  "smtp_user": "email@gmail.com",
  "smtp_password": "app-password"
}
```

### Personal Info (data/personal_info.json)
Contains user profile for form auto-fill:
- first_name, last_name, email, phone
- address, city, state, zip_code
- linkedin, github, website
- work_authorization, requires_sponsorship
- And more...

## Code Style

### Python
- Use async/await for browser automation
- Use try-except with specific error handling
- Return dictionaries with 'success' and 'error' keys

### JavaScript
- Use async/await for API calls
- Always handle errors gracefully
- Update UI to show progress/status

## Key Files to Modify

| Task | File(s) |
|------|---------|
| Form filling logic | src/browser_automation.py |
| API endpoints | app.py |
| Frontend UI | templates/*.html |
| Styling | static/css/style.css |
| Data models | src/models.py |
| LLM form analysis | src/form_agent.py |

## Remember

1. **KISS** - Keep It Simple, Stupid. Don't over-engineer.
2. **Don't break what works** - Test before and after changes.
3. **JSON everywhere** - API must always return JSON.
4. **No navigation** - Form fillers should not click navigation buttons.
5. **Log errors** - Use print() for debugging, traceback for exceptions.

## MCP Server Configuration (Playwright)

**Status:** Configured and ready to use after Claude Code restart.

### Setup Files
- `.mcp.json` - MCP server config in project root
- `~/.claude/settings.json` - Auto-approval enabled

### Available MCP Tools (after restart)
When Playwright MCP is loaded, these tools become available:
- `browser_navigate` - Navigate to URLs
- `browser_click` - Click elements on page
- `browser_type` - Type text into fields
- `browser_snapshot` - Get accessibility tree of page
- `browser_take_screenshot` - Capture page screenshot

### Using MCP for Job Applications
Instead of complex Playwright selectors, use MCP tools to:
1. Navigate to job URL
2. Click "Apply" button
3. Fill form fields naturally
4. Handle multi-step forms
5. Submit application

## Alternative Browser Automation

### Browser-Use Library (src/browser_use_agent.py)
LLM-powered browser automation as fallback:
```python
from src.browser_use_agent import BrowserUseFormFiller

agent = BrowserUseFormFiller(anthropic_key="your-key")
result = await agent.apply_to_job(job_url, resume_data)
```

Install: `pip install browser-use langchain-anthropic`

## Current Status (Feb 2026)

### What's Working
- CV/CL generation from job URLs
- Form field detection and filling
- Resume upload

### Known Issues
- Workday multi-step navigation sometimes fails (validation errors block progress)
- Some dropdowns need manual selection

### Recent Fixes
2. Added validation error detection before navigation
3. Multiple click strategies for navigation buttons
4. Browser-Use integration as alternative approach
5. Playwright MCP configured for direct browser control

### Pending Tasks
- Test auto-apply flow with MCP tools
- Improve dropdown handling
- Add more ATS platform support
