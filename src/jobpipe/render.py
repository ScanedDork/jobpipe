"""Render tailored documents as ATS-friendly .docx files.

Layout rules (validated in tests by re-opening the files): single column, A4, a standard
font, real Word headings with standard names, real Word bullets, contact details in the
body (not in a header), and no tables, text boxes, images or icons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from jobpipe import resource_dir
from jobpipe.profile import Profile, is_placeholder
from jobpipe.scorer import estimate_pages
from jobpipe.tailor import TailoredResume
from jobpipe.validators import STANDARD_FONTS, STANDARD_HEADINGS, letter_parts, output_filenames

SECTION_KEYS = ("summary", "skills", "experience", "education", "certifications", "languages")
REQUIRED_SECTIONS = ("experience", "education", "skills")


class TemplateError(ValueError):
    """A template is missing or breaks an ATS layout rule."""


@dataclass(frozen=True)
class Template:
    name: str
    font: str
    body_size: float
    name_size: float
    heading_size: float
    margins_cm: float
    space_after_pt: float
    headings: dict[str, str]
    section_order: tuple[str, ...]
    description: str = ""


_TEMPLATE_KEYS = {"name", "description", "font", "body_size", "name_size", "heading_size",
                  "margins_cm", "space_after_pt", "headings", "section_order"}
_RANGES = {"body_size": (9.5, 12), "name_size": (12, 20), "heading_size": (10, 14),
           "margins_cm": (1.27, 2.54), "space_after_pt": (0, 8)}


def available_templates() -> list[str]:
    return sorted(p.stem for p in resource_dir("templates").glob("*.yaml"))


def load_template(name_or_path: str | Path = "classic") -> Template:
    path = Path(name_or_path)
    if not path.is_file():
        path = resource_dir("templates") / f"{name_or_path}.yaml"
    if not path.is_file():
        raise TemplateError(f"Template '{name_or_path}' not found. Available: "
                            + ", ".join(available_templates()))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TemplateError(f"{path} must be a YAML mapping")
    unknown = set(data) - _TEMPLATE_KEYS
    if unknown:
        raise TemplateError(f"{path.name}: unknown keys {', '.join(sorted(unknown))}")

    font = str(data.get("font", ""))
    if font.lower() not in STANDARD_FONTS:
        raise TemplateError(f"{path.name}: font '{font}' is not a standard ATS-safe font "
                            f"({', '.join(sorted(STANDARD_FONTS))})")
    numbers = {}
    for key, (low, high) in _RANGES.items():
        try:
            value = float(data[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise TemplateError(f"{path.name}: '{key}' must be a number") from exc
        if not low <= value <= high:
            raise TemplateError(f"{path.name}: '{key}' must be between {low} and {high}")
        numbers[key] = value

    headings = data.get("headings") or {}
    order = tuple(data.get("section_order") or ())
    for key in order:
        if key not in SECTION_KEYS:
            raise TemplateError(f"{path.name}: unknown section '{key}'")
    if len(set(order)) != len(order) or not set(REQUIRED_SECTIONS) <= set(order):
        raise TemplateError(f"{path.name}: section_order must list each section once and "
                            f"include {', '.join(REQUIRED_SECTIONS)}")
    for key in order:
        heading = str(headings.get(key, ""))
        if heading.lower() not in STANDARD_HEADINGS[key]:
            allowed = ", ".join(sorted(STANDARD_HEADINGS[key]))
            raise TemplateError(f"{path.name}: heading '{heading}' for '{key}' is not a "
                                f"standard ATS heading ({allowed})")
    return Template(name=str(data.get("name", path.stem)), font=font,
                    headings={k: str(headings[k]) for k in order}, section_order=order,
                    description=str(data.get("description", "")), **numbers)


def output_paths(out_dir: str | Path, preferred_name: str, company: str) -> dict[str, Path]:
    out = Path(out_dir)
    return {kind: out / name for kind, name in output_filenames(preferred_name, company).items()}


# --- document plumbing --------------------------------------------------------------------

def _set_font(style, font: str, size: float | None = None, bold: bool | None = None) -> None:
    style.font.name = font
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        rfonts.attrib.pop(qn(attr), None)  # theme fonts would override the named font
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(attr), font)
    if size is not None:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    style.font.italic = False
    style.font.color.rgb = RGBColor(0, 0, 0)


def _new_document(t: Template) -> DocxDocument:
    doc = Document()
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21.0), Cm(29.7)  # A4
        for side in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
            setattr(section, side, Cm(t.margins_cm))
    normal = doc.styles["Normal"]
    _set_font(normal, t.font, t.body_size)
    normal.paragraph_format.space_after = Pt(t.space_after_pt)
    normal.paragraph_format.line_spacing = 1.0
    heading = doc.styles["Heading 1"]
    _set_font(heading, t.font, t.heading_size, bold=True)
    heading.paragraph_format.space_before = Pt(10)
    heading.paragraph_format.space_after = Pt(3)
    heading.paragraph_format.keep_with_next = True
    _set_font(doc.styles["List Bullet"], t.font, t.body_size)
    return doc


def _paragraph(doc: DocxDocument, *runs: tuple[str, bool], size: float | None = None,
               style: str | None = None):
    p = doc.add_paragraph(style=style)
    for text, bold in runs:
        if text:
            run = p.add_run(text)
            run.bold = bold or None
            if size:
                run.font.size = Pt(size)
    return p


def _header_block(doc: DocxDocument, profile: Profile, t: Template, *, with_auth: bool) -> None:
    _paragraph(doc, (profile.preferred_name, True), size=t.name_size)
    if not is_placeholder(profile.headline):
        _paragraph(doc, (profile.headline, False))
    contact = profile.contact_items()
    if contact:
        _paragraph(doc, (" | ".join(contact), False))
    if with_auth and profile.work_authorisation:
        _paragraph(doc, (profile.work_authorisation, False))


def _set_properties(doc: DocxDocument, profile: Profile, title: str) -> None:
    props = doc.core_properties
    props.author = profile.preferred_name
    props.last_modified_by = profile.preferred_name
    props.title = title
    props.subject = ""
    props.keywords = ""
    props.comments = ""


# --- resume -------------------------------------------------------------------------------

def _join_parts(*parts: str) -> str:
    return " | ".join(p for p in parts if p and not is_placeholder(p))


MAX_PAGES = 2.0


def _trim_one_bullet(tailored: TailoredResume, max_pages: float) -> bool:
    """Drop the least relevant bullet of the oldest role that can spare one (older roles
    keep at least 2 bullets, the most recent keeps at least 3). Bullets are already sorted
    by relevance, so the last one goes first."""
    for index in range(len(tailored.roles) - 1, -1, -1):
        role = tailored.roles[index]
        keep = 3 if index == 0 else 2
        if len(role.bullets) > keep:
            dropped = role.bullets.pop()
            exp = role.experience
            tailored.notes.append(f"Left out \"{dropped}\" ({exp.title} at {exp.company}) to "
                                  f"keep the resume within {max_pages:g} pages.")
            return True
    return False


def render_resume(tailored: TailoredResume, path: str | Path, template: Template, *,
                  max_pages: float = MAX_PAGES) -> Path:
    """Render the resume, trimming the least relevant bullets of older roles while the
    estimated length exceeds ``max_pages``."""
    path = Path(path)
    while True:
        doc = _build_resume(tailored, template)
        pages = estimate_pages((p.text, p.style.name) for p in doc.paragraphs)
        if pages <= max_pages or not _trim_one_bullet(tailored, max_pages):
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _build_resume(tailored: TailoredResume, template: Template) -> DocxDocument:
    profile = tailored.profile
    doc = _new_document(template)
    _header_block(doc, profile, template, with_auth=True)

    def heading(key: str) -> None:
        doc.add_paragraph(template.headings[key], style="Heading 1")

    for key in template.section_order:
        if key == "summary" and tailored.summary:
            heading(key)
            _paragraph(doc, (tailored.summary, False))
        elif key == "skills" and tailored.skills:
            heading(key)
            _paragraph(doc, (", ".join(tailored.skills), False))
        elif key == "experience" and tailored.roles:
            heading(key)
            for role in tailored.roles:
                exp = role.experience
                details = _join_parts(exp.location, exp.dates)
                _paragraph(doc, (f"{exp.title}, {exp.company}", True),
                           (f" | {details}" if details else "", False))
                if not is_placeholder(exp.description):
                    _paragraph(doc, (exp.description, False))
                for bullet in role.bullets:
                    _paragraph(doc, (bullet, False), style="List Bullet")
        elif key == "education" and profile.education:
            heading(key)
            for edu in profile.education:
                details = _join_parts(edu.location, edu.year)
                _paragraph(doc, (edu.degree, True), (f", {edu.institution}", False),
                           (f" | {details}" if details else "", False))
        elif key == "certifications" and tailored.certifications:
            heading(key)
            if not is_placeholder(profile.certification_hours):
                _paragraph(doc, (f"{profile.certification_hours} hours of professional "
                                 "certifications, including:", False))
            for cert in tailored.certifications:
                extra = " – ".join(x for x in (cert.issuer, cert.year) if not is_placeholder(x))
                _paragraph(doc, (cert.name + (f" – {extra}" if extra else ""), False),
                           style="List Bullet")
        elif key == "languages":
            languages = [x for x in profile.languages if not is_placeholder(x)]
            if languages:
                heading(key)
                _paragraph(doc, (", ".join(languages), False))

    _set_properties(doc, profile, f"{profile.preferred_name} – Resume")
    return doc


# --- cover letter -------------------------------------------------------------------------

def render_cover_letter(text: str, profile: Profile, company: str, path: str | Path,
                        template: Template, *, today: date | None = None) -> Path:
    path = Path(path)
    today = today or date.today()
    doc = _new_document(template)
    _header_block(doc, profile, template, with_auth=False)
    spaced = Pt(10)
    _paragraph(doc, (f"{today.day} {today:%B %Y}", False)).paragraph_format.space_before = spaced
    _paragraph(doc, (company, False)).paragraph_format.space_after = spaced

    salutation, paragraphs, signoff = letter_parts(text)
    if salutation:
        _paragraph(doc, (salutation, False)).paragraph_format.space_after = Pt(8)
    for para in paragraphs:
        _paragraph(doc, (para, False)).paragraph_format.space_after = Pt(8)
    for line in signoff.splitlines():
        _paragraph(doc, (line, False))

    _set_properties(doc, profile, f"{profile.preferred_name} – Cover letter – {company}")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path
