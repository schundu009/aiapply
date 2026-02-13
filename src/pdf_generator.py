from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle,
)
from .models import BaseResume, CoverLetter, TailoredResume

# Brand colors - white and green theme
_DARK = HexColor("#1a5d1a")      # Dark green for headings
_ACCENT = HexColor("#2e8b2e")    # Medium green for accents
_RULE = HexColor("#90ee90")      # Light green for rules
_LIGHT_BG = HexColor("#ffffff")  # White background


def _build_resume_styles():
    """Build professional resume paragraph styles."""
    base = getSampleStyleSheet()
    styles = {}

    styles["Name"] = ParagraphStyle(
        "Name",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        textColor=_DARK,
        alignment=TA_CENTER,
        spaceAfter=2,
    )
    styles["Contact"] = ParagraphStyle(
        "Contact",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=HexColor("#3d8b3d"),  # Green
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    styles["TargetTitle"] = ParagraphStyle(
        "TargetTitle",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=13,
        textColor=_ACCENT,
        alignment=TA_CENTER,
        spaceAfter=10,
    )
    styles["SectionHeading"] = ParagraphStyle(
        "SectionHeading",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=_DARK,
        spaceBefore=10,
        spaceAfter=3,
        textTransform="uppercase",
    )
    styles["Body"] = ParagraphStyle(
        "Body",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=HexColor("#1a5d1a"),  # Dark green
        spaceAfter=3,
    )
    styles["JobTitle"] = ParagraphStyle(
        "JobTitle",
        parent=base["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=HexColor("#1a5d1a"),  # Dark green
        spaceBefore=6,
        spaceAfter=1,
    )
    styles["CompanyDate"] = ParagraphStyle(
        "CompanyDate",
        parent=base["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        textColor=HexColor("#2e8b2e"),  # Medium green
        spaceAfter=3,
    )
    styles["Bullet"] = ParagraphStyle(
        "Bullet",
        parent=base["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=HexColor("#1a5d1a"),  # Dark green
        leftIndent=14,
        firstLineIndent=-10,
        spaceAfter=2,
    )
    return styles


def _section_rule():
    """A thin horizontal rule used as a section divider."""
    return HRFlowable(
        width="100%",
        thickness=0.5,
        color=_RULE,
        spaceBefore=2,
        spaceAfter=4,
    )


def _thick_rule():
    """A thicker accent rule for the header area."""
    return HRFlowable(
        width="100%",
        thickness=1.5,
        color=_ACCENT,
        spaceBefore=2,
        spaceAfter=6,
    )


def _parse_experience_block(text, styles):
    """Parse experience text into styled flowables, detecting job titles and bullets."""
    elements = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Detect bullet points
        if line.startswith(("•", "-", "&#8226;", "*")):
            clean = line.lstrip("•-*&#8226; ").strip()
            elements.append(Paragraph(f"&#8226;  {clean}", styles["Bullet"]))
        # Detect company/date lines (contain "|" with dates)
        elif "|" in line and any(
            kw in line for kw in ["20", "19", "Present", "Current"]
        ):
            elements.append(Paragraph(line, styles["CompanyDate"]))
        # Lines that look like job titles (short, no bullet, before a company line)
        elif (
            len(line) < 80
            and not line.startswith(("•", "-"))
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
        ):
            elements.append(Paragraph(line, styles["JobTitle"]))
        elif len(line) < 80 and not line.startswith(("•", "-")):
            # Could be a standalone title or subheading
            elements.append(Paragraph(f"<b>{line}</b>", styles["Body"]))
        else:
            elements.append(Paragraph(line, styles["Body"]))
        i += 1
    return elements


class PDFGenerator:
    def generate_resume_pdf(
        self, resume: TailoredResume, base: BaseResume, path: Path
    ) -> Path:
        doc = SimpleDocTemplate(
            str(path),
            pagesize=letter,
            leftMargin=0.6 * inch,
            rightMargin=0.6 * inch,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
        )
        styles = _build_resume_styles()
        story = []

        # ── Header ──
        story.append(Paragraph(base.full_name.upper(), styles["Name"]))

        contact_parts = []
        if base.location:
            contact_parts.append(base.location)
        if base.email:
            contact_parts.append(base.email)
        if base.phone:
            contact_parts.append(base.phone)
        if base.linkedin:
            contact_parts.append(base.linkedin)
        story.append(Paragraph(" &nbsp;|&nbsp; ".join(contact_parts), styles["Contact"]))

        story.append(
            Paragraph(resume.job_title.upper(), styles["TargetTitle"])
        )
        story.append(_thick_rule())

        # ── Professional Summary ──
        story.append(Paragraph("PROFESSIONAL SUMMARY", styles["SectionHeading"]))
        story.append(_section_rule())
        story.append(Paragraph(resume.summary, styles["Body"]))
        story.append(Spacer(1, 4))

        # ── Core Skills ──
        story.append(Paragraph("TECHNICAL SKILLS", styles["SectionHeading"]))
        story.append(_section_rule())
        for line in resume.skills_section.split("\n"):
            line = line.strip()
            if not line:
                continue
            # If line has "Category: items" format, bold the category
            if ":" in line:
                cat, items = line.split(":", 1)
                story.append(
                    Paragraph(f"<b>{cat.strip()}:</b> {items.strip()}", styles["Body"])
                )
            else:
                story.append(Paragraph(line, styles["Body"]))
        story.append(Spacer(1, 4))

        # ── Professional Experience ──
        story.append(
            Paragraph("PROFESSIONAL EXPERIENCE", styles["SectionHeading"])
        )
        story.append(_section_rule())
        story.extend(_parse_experience_block(resume.experience_section, styles))
        story.append(Spacer(1, 4))

        # ── Key Achievements ──
        story.append(Paragraph("KEY ACHIEVEMENTS", styles["SectionHeading"]))
        story.append(_section_rule())
        for line in resume.achievements_section.split("\n"):
            line = line.strip()
            if not line:
                continue
            clean = line.lstrip("•-*&#8226; ").strip()
            if clean:
                story.append(Paragraph(f"&#8226;  {clean}", styles["Bullet"]))
        story.append(Spacer(1, 4))

        # ── Education & Certifications ──
        story.append(
            Paragraph("EDUCATION & CERTIFICATIONS", styles["SectionHeading"])
        )
        story.append(_section_rule())
        for line in resume.education_section.split("\n"):
            if line.strip():
                story.append(Paragraph(line.strip(), styles["Body"]))

        doc.build(story)
        return path

    def generate_cover_letter_pdf(
        self, cl: CoverLetter, base: BaseResume, path: Path
    ) -> Path:
        doc = SimpleDocTemplate(
            str(path),
            pagesize=letter,
            leftMargin=inch,
            rightMargin=inch,
            topMargin=0.8 * inch,
            bottomMargin=0.8 * inch,
        )
        base_styles = getSampleStyleSheet()

        header_style = ParagraphStyle(
            "CLHeader",
            parent=base_styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=18,
            textColor=_DARK,
            spaceAfter=2,
        )
        contact_style = ParagraphStyle(
            "CLContact",
            parent=base_styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=HexColor("#3d8b3d"),  # Green
            spaceAfter=2,
        )
        body_style = ParagraphStyle(
            "CLBody",
            parent=base_styles["Normal"],
            fontName="Helvetica",
            fontSize=10.5,
            leading=15,
            textColor=HexColor("#1a5d1a"),  # Dark green
            spaceAfter=10,
        )

        story = []
        story.append(Paragraph(base.full_name, header_style))
        story.append(Paragraph(base.location, contact_style))
        story.append(
            Paragraph(f"{base.email} | {base.phone}", contact_style)
        )
        story.append(_thick_rule())
        story.append(Spacer(1, 12))

        for para in cl.content.split("\n\n"):
            if para.strip():
                story.append(Paragraph(para.strip(), body_style))

        doc.build(story)
        return path


def save_text(resume: TailoredResume, base: BaseResume, path: Path) -> Path:
    path.write_text(f"{base.full_name}\n{base.email}\n\n{resume.full_text}")
    return path
