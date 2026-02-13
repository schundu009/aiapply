#!/usr/bin/env python3
"""AutoApply CLI."""
import asyncio, hashlib, json, secrets, string
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.live import Live
from .config import settings
from .job_fetcher import JobFetcher
from .models import Application, ApplicationStatus, BaseResume
from .notion_client import parse_resume_from_text
from .pdf_generator import PDFGenerator
from .resume_generator import ResumeGenerator
from .browser_automation import BrowserAutomation

app = typer.Typer(name="autoapply", help="AI-powered job application automation")
console = Console()

def gen_id(url: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(url.encode()).hexdigest()[:8]}"

async def process_job(url: str, base: BaseResume, out_dir: Path) -> Application:
    fetcher = JobFetcher()
    generator = ResumeGenerator()
    pdf_gen = PDFGenerator()
    app = Application(id=gen_id(url), job_url=url)
    
    try:
        console.print(f"[green]Fetching {url}...[/green]")
        job = await fetcher.fetch_job(url)
        app.job_description = job
        app.status = ApplicationStatus.JD_FETCHED
        console.print(f"  [green]{job.company_name} - {job.job_title}[/green]")
        
        safe = lambda s: "".join(c if c.isalnum() else "_" for c in s)
        app_dir = out_dir / f"{safe(job.company_name)}_{safe(job.job_title)}_{app.id}"
        app_dir.mkdir(parents=True, exist_ok=True)
        app.output_dir = app_dir
        
        console.print("[green]Generating resume...[/green]")
        resume = await generator.generate_tailored_resume(job, base)
        app.tailored_resume = resume
        app.status = ApplicationStatus.RESUME_GENERATED
        console.print(f"  [green]Match: {resume.match_score}%[/green]")
        
        console.print("[green]Generating cover letter...[/green]")
        cover = await generator.generate_cover_letter(job, base, resume)
        app.cover_letter = cover
        
        console.print("[green]Creating PDFs...[/green]")
        pdf_gen.generate_resume_pdf(resume, base, app_dir / "resume.pdf")
        pdf_gen.generate_cover_letter_pdf(cover, base, app_dir / "cover_letter.pdf")
        (app_dir / "resume.txt").write_text(resume.full_text)
        (app_dir / "cover_letter.txt").write_text(cover.content)
        app.status = ApplicationStatus.PDF_GENERATED
        console.print(f"  [green]Saved to {app_dir}[/green]")
        
    except Exception as e:
        app.status = ApplicationStatus.FAILED
        app.error_message = str(e)
        console.print(f"[bold white]Error: {e}[/bold white]")
    finally:
        await fetcher.close()
    return app

@app.command()
def apply(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Single job URL"),
    jobs: Optional[Path] = typer.Option(None, "--jobs", "-j", help="File with URLs"),
    resume: Optional[Path] = typer.Option(None, "--resume", "-r", help="Resume file"),
    output: Path = typer.Option(Path("output"), "--output", "-o", help="Output dir"),
):
    """Apply to jobs with AI-tailored resumes."""
    urls = [url] if url else []
    if jobs and jobs.exists():
        urls = [l.strip() for l in jobs.read_text().splitlines() if l.strip() and not l.startswith("#")]
    if not urls:
        console.print("[bold white]Provide --url or --jobs[/bold white]")
        raise typer.Exit(1)
    
    resume_path = resume or settings.base_resume_file or Path("resume.txt")
    if not resume_path.exists():
        console.print(f"[bold white]Resume not found: {resume_path}[/bold white]")
        raise typer.Exit(1)
    
    base = parse_resume_from_text(resume_path.read_text())
    console.print(f"[green]Loaded: {base.full_name}[/green]")
    output.mkdir(parents=True, exist_ok=True)
    
    results = []
    for u in urls:
        results.append(asyncio.run(process_job(u, base, output)))
    
    table = Table(title="Results", title_style="bold green", header_style="green", border_style="green")
    table.add_column("Company", style="white"); table.add_column("Title", style="white"); table.add_column("Status", style="green"); table.add_column("Score", style="green")
    for r in results:
        co = r.job_description.company_name if r.job_description else "?"
        ti = r.job_description.job_title if r.job_description else "?"
        sc = f"{r.tailored_resume.match_score}%" if r.tailored_resume else "-"
        table.add_row(co, ti, r.status.value, sc)
    console.print(table)

@app.command()
def init():
    """Create config files."""
    Path(".env").write_text("ANTHROPIC_API_KEY=your_key_here\n")
    Path("jobs.txt").write_text("# Add job URLs here\n")
    console.print("[green]Created .env and jobs.txt[/green]")


def load_personal_info() -> dict:
    """Load personal info from JSON file."""
    path = Path("personal_info.json")
    if path.exists():
        try:
            return json.loads(path.read_text())
        except:
            pass
    return {}


def save_personal_info(info: dict):
    """Save personal info to JSON file."""
    Path("personal_info.json").write_text(json.dumps(info, indent=2))


@app.command("create-accounts")
def create_accounts(
    platforms: Optional[str] = typer.Option(None, "--platforms", "-p", help="Comma-separated platforms (greenhouse,lever,workday,etc)"),
    email: Optional[str] = typer.Option(None, "--email", "-e", help="Override email"),
    password: Optional[str] = typer.Option(None, "--password", help="Override default password"),
):
    """Create accounts on job boards automatically."""
    console.print(Panel("[bold green]Job Board Account Creation[/bold green]", border_style="green"))

    # Load personal info
    personal_info = load_personal_info()

    # Override with CLI options
    if email:
        personal_info['email'] = email
    if password:
        personal_info['default_job_password'] = password

    # Validate required fields
    if not personal_info.get('email'):
        console.print("[bold red]Error:[/bold red] Email is required. Use --email or set in personal_info.json")
        raise typer.Exit(1)

    if not personal_info.get('first_name') or not personal_info.get('last_name'):
        console.print("[bold red]Error:[/bold red] First and last name required in personal_info.json")
        raise typer.Exit(1)

    # Generate password if not set
    if not personal_info.get('default_job_password'):
        chars = string.ascii_letters + string.digits + "!@#$"
        personal_info['default_job_password'] = ''.join(secrets.choice(chars) for _ in range(16))
        console.print(f"[yellow]Generated password:[/yellow] {personal_info['default_job_password']}")

    # Parse platforms
    platform_list = None
    if platforms:
        platform_list = [p.strip().lower() for p in platforms.split(",")]
    else:
        platform_list = ['greenhouse', 'lever', 'ashby', 'workday', 'smartrecruiters', 'jobvite', 'icims', 'taleo', 'bamboohr']

    console.print(f"\n[green]Email:[/green] {personal_info.get('email')}")
    console.print(f"[green]Name:[/green] {personal_info.get('first_name')} {personal_info.get('last_name')}")
    console.print(f"[green]Platforms:[/green] {', '.join(platform_list)}\n")

    # Create log callback for real-time output
    def log_callback(message: str):
        if message.startswith("  "):
            console.print(f"    [dim]{message.strip()}[/dim]")
        elif message.startswith("["):
            console.print(f"[green]{message}[/green]")
        else:
            console.print(f"[white]{message}[/white]")

    # Run account creation
    console.print("[bold green]Starting account creation...[/bold green]\n")

    try:
        async def run_creation():
            automation = BrowserAutomation(headless=True, personal_info=personal_info)
            try:
                result = await automation.create_job_board_accounts(platform_list, log_callback=log_callback)
                return result
            finally:
                try:
                    await automation.stop()
                except Exception:
                    pass  # Ignore cleanup errors

        response = asyncio.run(run_creation())

        results = response.get('results', response)

        # Update credentials
        for platform, result in results.items():
            if isinstance(result, dict) and result.get('success') and result.get('email'):
                personal_info[f'{platform}_email'] = result['email']
                if result.get('password'):
                    personal_info[f'{platform}_password'] = result['password']

        save_personal_info(personal_info)

        # Summary table
        console.print("\n")
        table = Table(title="Account Creation Summary", title_style="bold green", border_style="green")
        table.add_column("Platform", style="white")
        table.add_column("Status", style="green")
        table.add_column("Message", style="dim")

        for platform, result in results.items():
            status = "[green]✓[/green]" if result.get('success') else "[red]✗[/red]"
            msg = result.get('message', result.get('error', ''))[:40]
            table.add_row(platform.upper(), status, msg)

        console.print(table)
        console.print("\n[green]Credentials saved to personal_info.json[/green]")

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {str(e)}")
        raise typer.Exit(1)


@app.command("list-accounts")
def list_accounts():
    """List stored job board credentials."""
    personal_info = load_personal_info()

    platforms = ['google', 'linkedin', 'workday', 'greenhouse', 'lever', 'ashby', 'icims', 'taleo', 'smartrecruiters', 'jobvite', 'bamboohr']

    table = Table(title="Stored Job Board Credentials", title_style="bold green", border_style="green")
    table.add_column("Platform", style="white")
    table.add_column("Email", style="green")
    table.add_column("Password", style="dim")

    for platform in platforms:
        email = personal_info.get(f'{platform}_email', '')
        password = personal_info.get(f'{platform}_password', '')
        pwd_display = "●●●●●●●●" if password else "[dim]-[/dim]"
        email_display = email if email else "[dim]-[/dim]"
        table.add_row(platform.upper(), email_display, pwd_display)

    console.print(table)

    if personal_info.get('default_job_password'):
        console.print(f"\n[yellow]Default Password:[/yellow] {personal_info.get('default_job_password')}")


if __name__ == "__main__":
    app()