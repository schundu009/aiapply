# AIApply (AutoApply)

Automated job application tool with AI-powered resume tailoring using Claude API.
Repository: `schundu009/aiapply` · Production: https://aiapply.up.railway.app (Cariara admins only)

## Access and safety model

- **Admin-only.** Every page, API route, SSE stream, static file and download requires a
  Cariara access token (from `https://cariara-backend.up.railway.app/auth/login`) sent as
  `Authorization: Bearer <token>` or via the `/login` page cookie. The token is verified with
  `JWT_SECRET_KEY` / `JWT_ALGORITHM` (same values as `cariara-backend`) and the user must have
  the `admin`, `administrator`, `manager` or `developer` role (re-checked against the backend's
  `/auth/me`, cached 60s). Only `/healthz` is public. Without `JWT_SECRET_KEY` the app returns 503.
- **No credentials.** The app never stores job-site/Google/LinkedIn/ATS passwords, never creates
  accounts and never signs in for you. If a site shows a sign-in wall, the run stops with
  "sign in manually".
- **No evasion.** Standard Playwright Chromium; no automation-flag hiding, user-agent or
  location spoofing, or artificial delays. CAPTCHAs are detected and left for you to complete.
- **No invented answers.** Work authorization, sponsorship, relocation, consents, EEO and
  "how did you hear" answers come only from your saved profile; blank stays blank. The LLM only
  drafts free-text answers and only sees non-sensitive profile fields plus your resume.
- **No auto-submit by default.** Runs stop at review; `auto_submit` must be explicitly `true`.

Run tests with `pip install -r requirements.txt -r requirements-dev.txt && pytest`.

## Features

- **Multi-ATS Support**: Greenhouse, Lever, Workday, Ashby, and generic job boards
- **AI-Powered Resume Tailoring**: Uses Claude to generate ATS-optimized resumes
- **Cover Letter Generation**: Tailored cover letters for each application
- **PDF Generation**: Professional PDF output for resumes and cover letters
- **Browser Automation**: Auto-fill application forms (Playwright-based)
- **Notion Integration**: Store your master resume in Notion
- **Batch Processing**: Process multiple job URLs from a file

## Installation

### Prerequisites

- Python 3.10+
- Anthropic API key

### Setup

```bash
# Clone/navigate to the project
cd autoapply

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -e .

# Install Playwright browsers (for form automation)
playwright install chromium

# Initialize configuration
autoapply init
```

### Configuration

Edit `.env` file:

```bash
# Required
ANTHROPIC_API_KEY=your_api_key_here

# Optional: Notion integration
NOTION_API_KEY=your_notion_api_key
NOTION_RESUME_DATABASE_ID=your_database_id
```

## Usage

### Quick Start - Single Job

```bash
# Apply to a single job URL
autoapply single "https://job-boards.greenhouse.io/company/jobs/12345" \
  --resume resume.txt

# Just analyze a job (no application)
autoapply analyze "https://job-boards.greenhouse.io/company/jobs/12345"
```

### Batch Processing

1. Add job URLs to `jobs.txt` (one per line):

```txt
https://job-boards.greenhouse.io/company1/jobs/12345
https://jobs.lever.co/company2/abc-123
https://company.wd5.myworkdayjobs.com/careers/job/Title_ID
```

2. Run batch application:

```bash
autoapply apply --jobs jobs.txt --resume resume.txt
```

### With Browser Automation

```bash
# Open browser to fill forms (manual submit)
autoapply apply --jobs jobs.txt --resume resume.txt --browser

# With auto-submit attempt (use with caution)
autoapply apply --jobs jobs.txt --resume resume.txt --browser --auto-submit
```

### Using Notion for Resume Storage

```bash
autoapply apply --jobs jobs.txt --notion
```

## Command Reference

### `autoapply init`

Initialize configuration files:
- Creates `.env` template
- Creates `jobs.txt` example
- Creates `templates/` directory

### `autoapply apply`

Main application command.

| Option | Description |
|--------|-------------|
| `--jobs, -j` | File containing job URLs |
| `--url, -u` | Single job URL |
| `--resume, -r` | Path to base resume file |
| `--output, -o` | Output directory (default: `output/`) |
| `--notion` | Load resume from Notion |
| `--browser, -b` | Enable browser automation |
| `--auto-submit` | Attempt auto-submission |
| `--headless` | Run browser in headless mode |
| `--concurrent, -c` | Number of concurrent jobs |

### `autoapply single <url>`

Quick apply to a single job.

### `autoapply analyze <url>`

Analyze a job posting without applying:
- Extracts requirements
- Prioritizes must-have vs nice-to-have
- Lists technologies and keywords

## Output Structure

Each application generates:

```
output/
└── CompanyName_JobTitle_20240101_abc123/
    ├── job_description.json    # Parsed JD
    ├── resume.pdf              # Tailored resume
    ├── resume.txt              # Plain text version
    ├── cover_letter.pdf        # Cover letter
    ├── cover_letter.txt        # Plain text version
    └── application.json        # Application record
```

## Resume Format

Your base resume should be a plain text file with clear sections:

```txt
JOHN DOE
San Francisco, CA | john@email.com | (555) 123-4567 | linkedin.com/in/johndoe

PROFESSIONAL SUMMARY
Senior Software Engineer with 10+ years...

TECHNICAL SKILLS
Languages: Python, Go, JavaScript...
Cloud: AWS, GCP, Azure...

PROFESSIONAL EXPERIENCE

Senior Software Engineer
Company Name | January 2020 - Present
• Led development of microservices architecture...
• Reduced deployment time by 50%...

EDUCATION
Bachelor of Science in Computer Science - University Name

CERTIFICATIONS
AWS Solutions Architect | Kubernetes Administrator (CKA)
```

## Notion Integration

To use Notion for resume storage:

1. Create a Notion integration at https://www.notion.so/my-integrations
2. Create a database with these properties:
   - `Name` (Title)
   - `Email` (Email)
   - `Phone` (Phone)
   - `Location` (Text)
   - `LinkedIn` (URL)
   - `Summary` (Text)
   - `Skills` (Multi-select or Text)
   - `Certifications` (Multi-select or Text)
3. Share the database with your integration
4. Add credentials to `.env`

## Supported ATS Platforms

| Platform | Job Fetch | Form Fill | Auto-Submit |
|----------|-----------|-----------|-------------|
| Greenhouse | ✅ | ✅ | ⚠️ |
| Lever | ✅ | ✅ | ⚠️ |
| Ashby | ✅ | ✅ | ⚠️ |
| Workday | ✅ | ⚠️ (login required) | ❌ |
| SmartRecruiters | ✅ | ⚠️ | ⚠️ |
| Generic | ✅ | ⚠️ | ⚠️ |

**Legend**: ✅ Supported | ⚠️ Limited/Manual intervention may be needed | ❌ Not supported

## Limitations

- **CAPTCHAs**: When detected, the tool pauses for manual solving
- **Login Required**: Some ATS platforms require authentication
- **Anti-Bot Measures**: Rate limiting and detection may block automation
- **Form Variations**: Non-standard forms may require manual completion

## Best Practices

1. **Review Before Submit**: Always use `--browser` without `--auto-submit` first
2. **Quality over Quantity**: Target relevant jobs rather than mass applying
3. **Customize Base Resume**: Keep your base resume comprehensive
4. **Monitor Output**: Check generated resumes for accuracy
5. **Respect Rate Limits**: Don't overwhelm job boards

## Troubleshooting

### "Anthropic API key not found"
```bash
export ANTHROPIC_API_KEY=your_key_here
# Or add to .env file
```

### "Playwright browsers not installed"
```bash
playwright install chromium
```

### "WeasyPrint installation issues"
WeasyPrint requires system dependencies:

**macOS**:
```bash
brew install pango
```

**Ubuntu/Debian**:
```bash
apt-get install libpango-1.0-0 libpangocairo-1.0-0
```

### Form not filling correctly
Use `--browser` without `--auto-submit` and complete manually. Report the ATS platform for future support.

## License

MIT License
