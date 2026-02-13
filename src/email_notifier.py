"""Email notification system for job application receipts."""

import smtplib
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from .config import settings
from .models import Application


class EmailNotifier:
    """Sends email notifications for job applications."""

    def __init__(self):
        self.enabled = settings.email_notifications_enabled
        self.smtp_host = settings.smtp_host
        self.smtp_port = settings.smtp_port
        self.smtp_user = settings.smtp_user
        self.smtp_password = settings.smtp_password
        self.from_email = settings.smtp_from_email or settings.smtp_user

    def is_configured(self) -> bool:
        """Check if email is properly configured."""
        return bool(
            self.enabled
            and self.smtp_host and self.smtp_host.strip()
            and self.smtp_user and self.smtp_user.strip()
            and self.smtp_password
        )

    def send_application_receipt(
        self,
        application: Application,
        personal_info: dict,
        resume_pdf_path: Optional[Path] = None,
        cover_letter_pdf_path: Optional[Path] = None,
    ) -> tuple[bool, str]:
        """
        Send an email receipt for a submitted job application.

        Args:
            application: The Application record
            personal_info: User's personal info dict (must contain 'email')
            resume_pdf_path: Path to the generated resume PDF
            cover_letter_pdf_path: Path to the generated cover letter PDF

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not self.is_configured():
            return False, "Email notifications not configured. Set SMTP settings in .env"

        to_email = personal_info.get('email')
        if not to_email:
            return False, "No recipient email address in personal_info"

        jd = application.job_description
        viab = application.job_viability

        company = jd.company_name if jd else "Unknown Company"
        job_title = jd.job_title if jd else "Unknown Position"

        # Build email
        msg = MIMEMultipart()
        msg['From'] = self.from_email
        msg['To'] = to_email
        msg['Subject'] = f"Application Submitted: {company} - {job_title}"

        # Build email body
        body_parts = [
            f"Application Submitted Successfully",
            f"{'=' * 40}",
            f"",
            f"Company: {company}",
            f"Position: {job_title}",
        ]

        if jd:
            if jd.location:
                body_parts.append(f"Location: {jd.location}")
            if jd.salary_range:
                body_parts.append(f"Salary: {jd.salary_range}")
            if jd.remote_type:
                body_parts.append(f"Work Type: {jd.remote_type}")

        body_parts.append(f"Job URL: {application.job_url}")
        body_parts.append(f"")

        if application.applied_at:
            body_parts.append(f"Applied: {application.applied_at.strftime('%Y-%m-%d %H:%M')}")
        else:
            body_parts.append(f"Submitted: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        body_parts.append(f"Status: {application.status.value}")
        body_parts.append(f"")

        # Add viability notes
        if viab:
            body_parts.append(f"Viability Assessment")
            body_parts.append(f"{'-' * 20}")
            body_parts.append(f"Recommendation: {viab.overall_recommendation}")
            body_parts.append(f"Match Score: {viab.match_score:.0f}%")
            if viab.viability_notes:
                body_parts.append(f"")
                body_parts.append("Notes:")
                for note in viab.viability_notes:
                    body_parts.append(f"  - {note}")

        body_parts.append(f"")
        body_parts.append(f"{'=' * 40}")
        body_parts.append(f"")
        body_parts.append("Your tailored resume and cover letter are attached to this email.")
        body_parts.append(f"")
        body_parts.append("Good luck with your application!")
        body_parts.append(f"")
        body_parts.append("- AutoApply")

        body = "\n".join(body_parts)
        msg.attach(MIMEText(body, 'plain'))

        # Attach resume PDF
        if resume_pdf_path and resume_pdf_path.exists():
            with open(resume_pdf_path, 'rb') as f:
                attach = MIMEApplication(f.read(), _subtype='pdf')
                attach.add_header(
                    'Content-Disposition',
                    'attachment',
                    filename=resume_pdf_path.name
                )
                msg.attach(attach)

        # Attach cover letter PDF
        if cover_letter_pdf_path and cover_letter_pdf_path.exists():
            with open(cover_letter_pdf_path, 'rb') as f:
                attach = MIMEApplication(f.read(), _subtype='pdf')
                attach.add_header(
                    'Content-Disposition',
                    'attachment',
                    filename=cover_letter_pdf_path.name
                )
                msg.attach(attach)

        # Send email
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            return True, f"Email sent to {to_email}"

        except smtplib.SMTPAuthenticationError:
            return False, "SMTP authentication failed. Check username and password."
        except smtplib.SMTPConnectError:
            return False, f"Could not connect to SMTP server {self.smtp_host}:{self.smtp_port}"
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {str(e)}"
        except Exception as e:
            return False, f"Failed to send email: {str(e)}"


# Singleton instance
email_notifier = EmailNotifier()
