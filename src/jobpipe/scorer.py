"""ATS scorer: a strict, explainable 0-100 score with letter grades per category.

Categories: keyword match (only with a job posting), structure/parseability, bullet quality,
length, rule violations, plus cover letter content when a letter is supplied. A hard-rule
violation caps the overall score at 59 (F) however good the rest is.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document

from jobpipe.keywords import (
    ACTION_VERBS,
    NUMBER_WORDS,
    WEAK_PHRASES,
    KeywordMatch,
    keyword_score,
    match_keywords,
    parse_posting,
)
from jobpipe.validators import (
    PLACEHOLDER_RE,
    SALUTATION_RE,
    STANDARD_FONTS,
    STANDARD_HEADINGS,
    WORK_AUTH_STATEMENT,
    check_cover_letter_opening,
    check_output_filename,
    check_work_authorisation,
    letter_parts,
    word_count,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
V_NS = "urn:schemas-microsoft-com:vml"
BULLET_GLYPHS = "•·-–—*▪●◦‣"
FANCY_RE = re.compile(r"[←-⇿☀-➿⬀-⯿\U0001F300-\U0001FAFF✓✔★☆➢]")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
CLICHES = ("to whom it may concern", "dear sir or madam", "team player", "hard-working",
           "hardworking", "go-getter", "think outside the box", "perfect fit", "passionate about",
           "detail-oriented", "results-driven", "self-starter", "dynamic individual")
# Keywords dominate because ATS ranking is driven by them. Rules weigh little because a
# hard-rule violation already caps the overall score at 59.
WEIGHTS_WITH_JOB = {"keywords": 40, "structure": 20, "bullets": 20, "length": 10, "rules": 10}
WEIGHTS_NO_JOB = {"structure": 35, "bullets": 35, "length": 15, "rules": 15}
COVER_WEIGHT = 10
LABELS = {"keywords": "Keyword match", "structure": "Structure & parseability",
          "bullets": "Bullet quality", "length": "Length", "rules": "Rule violations",
          "cover": "Cover letter content"}


def grade(score: float) -> str:
    for threshold, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if score >= threshold:
            return letter
    return "F"


@dataclass
class Para:
    line: int  # 1-based position among non-empty paragraphs
    text: str
    style: str
    bold: bool
    is_list: bool


@dataclass
class DocInfo:
    path: Path
    paragraphs: list[Para]
    tables: int
    textboxes: int
    drawings: int
    columns: int
    header_footer_text: list[str]
    fonts: set[str]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.paragraphs)


@dataclass
class Finding:
    where: str
    excerpt: str
    problem: str
    fix: str


@dataclass
class Category:
    key: str
    score: int
    notes: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade(self.score)


@dataclass
class ScoreReport:
    overall: int
    categories: list[Category]
    red_flags: list[str]
    findings: list[Finding]
    keyword_matches: list[KeywordMatch] = field(default_factory=list)
    cover_keyword_matches: list[KeywordMatch] = field(default_factory=list)
    hard_violations: int = 0
    seniority: list[str] = field(default_factory=list)
    extra_sections: list[tuple[str, str]] = field(default_factory=list)

    @property
    def grade(self) -> str:
        return grade(self.overall)

    def category(self, key: str) -> Category | None:
        return next((c for c in self.categories if c.key == key), None)

    def to_markdown(self, title: str = "ATS report") -> str:
        return _to_markdown(self, title)


# --- reading .docx ------------------------------------------------------------------------

def _style_chain_fonts(style) -> set[str]:
    fonts = set()
    while style is not None:
        if style.font is not None and style.font.name:
            fonts.add(style.font.name)
        style = style.base_style
    return fonts


def read_docx(path: str | Path) -> DocInfo:
    path = Path(path)
    doc = Document(str(path))
    body = doc.element.body
    paragraphs: list[Para] = []
    fonts: set[str] = set()
    for p in doc.paragraphs:
        fonts |= _style_chain_fonts(p.style)
        fonts |= {r.font.name for r in p.runs if r.font.name}
        text = p.text.strip()
        if not text:
            continue
        style = p.style.name if p.style is not None else ""
        style_bold = bool(p.style is not None and p.style.font.bold)
        runs = [r for r in p.runs if r.text.strip()]
        bold = bool(runs) and all(r.bold if r.bold is not None else style_bold for r in runs)
        ppr = p._p.pPr
        numbered = ppr is not None and ppr.numPr is not None
        is_list = "list" in style.lower() or numbered or text[0] in BULLET_GLYPHS
        paragraphs.append(Para(len(paragraphs) + 1, text, style, bold, is_list))

    header_footer = []
    for section in doc.sections:
        for part in (section.header, section.footer, section.first_page_header,
                     section.first_page_footer, section.even_page_header,
                     section.even_page_footer):
            if not part.is_linked_to_previous:
                header_footer += [p.text.strip() for p in part.paragraphs if p.text.strip()]
    return DocInfo(
        path=path,
        paragraphs=paragraphs,
        tables=sum(1 for _ in body.iter(f"{{{W_NS}}}tbl")),
        textboxes=sum(1 for _ in body.iter(f"{{{W_NS}}}txbxContent"))
        + sum(1 for _ in body.iter(f"{{{V_NS}}}textbox")),
        drawings=sum(1 for _ in body.iter(f"{{{W_NS}}}drawing"))
        + sum(1 for _ in body.iter(f"{{{W_NS}}}pict")),
        columns=max((int(c.get(f"{{{W_NS}}}num", "1")) for c in body.iter(f"{{{W_NS}}}cols")),
                    default=1),
        header_footer_text=header_footer,
        fonts=fonts,
    )


def letter_text(info: DocInfo) -> str:
    """Letter text from salutation to signature, skipping the name/contact/date block."""
    paras = [p.text for p in info.paragraphs]
    start = next((i for i, t in enumerate(paras) if SALUTATION_RE.match(t)), None)
    if start is None:
        start = 0
        while start < len(paras) and (
            EMAIL_RE.search(paras[start]) or " | " in paras[start]
            or (len(paras[start].split()) <= 8
                and not paras[start].rstrip().endswith((".", "!", "?")))):
            start += 1
    return "\n\n".join(paras[start:])


def heading_key(text: str) -> str | None:
    norm = re.sub(r"[^a-z ]", "", text.lower().replace("&", "and")).strip()
    norm = re.sub(r"\s+", " ", norm)
    for key, names in STANDARD_HEADINGS.items():
        if norm in names:
            return key
    return None


def _is_heading(p: Para) -> bool:
    if p.line == 1:
        return False  # the name line
    if p.style.lower().startswith(("heading", "title")):
        return True
    words = p.text.rstrip(":").split()
    if not 1 <= len(words) <= 5 or p.is_list or p.text.rstrip().endswith((".", ",")):
        return False
    return heading_key(p.text) is not None or (p.text.isupper() and len(p.text) > 3)


def sections(info: DocInfo) -> tuple[list[tuple[Para, str | None]], dict[str, list[Para]]]:
    headings: list[tuple[Para, str | None]] = []
    content: dict[str, list[Para]] = {}
    current = "header"
    for p in info.paragraphs:
        if _is_heading(p):
            key = heading_key(p.text)
            headings.append((p, key))
            current = key or f"nonstandard:{p.text}"
            continue
        content.setdefault(current, []).append(p)
    return headings, content


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --- categories ---------------------------------------------------------------------------

def _structure(info: DocInfo, red: list[str], findings: list[Finding]) -> Category:
    score, notes = 100, []

    def hit(points: int, message: str, *, flag: bool = False) -> None:
        nonlocal score
        score -= points
        notes.append(f"-{points}: {message}")
        if flag:
            red.append(message)

    if info.tables:
        hit(30, f"Resume contains {info.tables} table(s); many ATS scramble or drop table "
                "content. Use plain paragraphs.", flag=True)
    if info.textboxes:
        hit(30, f"Resume contains {info.textboxes} text box(es); ATS usually skip them.",
            flag=True)
    if info.drawings:
        hit(20, f"Resume contains {info.drawings} image(s)/icon(s)/shape(s); they are "
                "invisible to ATS and can break parsing.", flag=True)
    if info.columns > 1:
        hit(30, f"Resume uses a {info.columns}-column layout; ATS read across columns and "
                "mix sections. Use a single column.", flag=True)
    if info.header_footer_text:
        hit(10, "Text in the page header/footer ("
            + _short("; ".join(info.header_footer_text), 60)
            + ") is skipped by some ATS. Move contact details into the body.")
    odd_fonts = sorted(f for f in info.fonts if f.lower() not in STANDARD_FONTS)
    if odd_fonts:
        hit(10, f"Non-standard font(s): {', '.join(odd_fonts)}. Use Calibri, Arial or similar.")

    headings, content = sections(info)
    found = {key for _, key in headings if key}
    for key in ("experience", "education", "skills"):
        if key not in found:
            label = {"experience": "Professional Experience", "education": "Education",
                     "skills": "Skills"}[key]
            hit(15, f"No '{label}' section heading found; ATS map content by standard "
                    "headings.", flag=True)
            findings.append(Finding("Resume", "(missing section)", f"No {label} section.",
                                    f"Add a '{label}' heading with real content from your "
                                    "profile."))
    if "summary" not in found:
        hit(5, "No summary/profile section; European recruiters expect 2-3 lines at the top.")
    for p, key in headings:
        if key is None:
            hit(5, f"Non-standard heading '{p.text}'.")
            findings.append(Finding(f"Resume line {p.line}", p.text, "Non-standard heading.",
                                    "Rename it to a standard heading such as 'Professional "
                                    "Experience', 'Skills' or 'Education'."))

    top = "\n".join(p.text for p in info.paragraphs[:6])
    if not EMAIL_RE.search(top):
        hit(10, "No email address in the top lines of the resume.", flag=True)
    if not PHONE_RE.search(top):
        hit(5, "No phone number in the top lines of the resume.")
    if "linkedin" not in top.lower():
        hit(3, "No LinkedIn URL; most EU recruiters check it.")
    fancy = [p for p in info.paragraphs if FANCY_RE.search(p.text)]
    if fancy:
        hit(5, "Decorative symbols/emoji found; ATS may garble them.")
        for p in fancy[:3]:
            findings.append(Finding(f"Resume line {p.line}", _short(p.text),
                                    "Decorative symbol or emoji.",
                                    "Replace with plain text or a standard bullet (•)."))
    return Category("structure", max(0, score), notes)


def _bullet_problems(text: str) -> tuple[int, list[str], list[str]]:
    words = text.lstrip(BULLET_GLYPHS + " ").split()
    first = words[0].lower().strip(",.;:") if words else ""
    lowered = text.lower()
    verb = first in ACTION_VERBS
    quantified = bool(re.search(r"\d", text)) or any(
        w in NUMBER_WORDS for w in re.findall(r"[a-z]+", lowered))
    weak = next((ph for ph in WEAK_PHRASES if ph in lowered), "")
    pronoun = re.search(r"\b(I|me|my)\b", text)
    problems, fixes = [], []
    if "TODO" in text:
        return 0, ["Placeholder text."], ["Replace it with a real achievement or delete it."]
    if not verb:
        problems.append(f"Starts with '{words[0] if words else ''}', not an action verb.")
        fixes.append("Lead with a strong verb (Managed, Grew, Negotiated, Launched).")
    if not quantified:
        problems.append("No number, so no measurable result.")
        fixes.append("Add a real metric from your records (accounts, revenue, %, time "
                     "saved) to profile.yaml; never estimate.")
    if weak:
        problems.append(f"Weak phrase '{weak}'.")
        fixes.append("Say what you achieved, not what you were responsible for.")
    if pronoun:
        problems.append("First-person pronoun in a bullet.")
        fixes.append("Drop 'I/my'; bullets are implied first person.")
    if len(words) < 8:
        problems.append(f"Only {len(words)} words: too thin to show scope and result.")
        fixes.append("Add scope (who/how many) and the result.")
    elif len(words) > 30:
        problems.append(f"{len(words)} words: too long to scan.")
        fixes.append("Cut to one line or two (under 30 words).")
    points = (40 if verb else 0) + (40 if quantified else 0)
    points += 20 if not (weak or pronoun or len(words) < 8 or len(words) > 30) else 0
    return points, problems, fixes


def _bullets(info: DocInfo, red: list[str], findings: list[Finding]) -> Category:
    _, content = sections(info)
    experience = content.get("experience")
    pool = experience if experience is not None else info.paragraphs
    bullets = [p for p in pool if p.is_list]
    if not bullets:
        red.append("No bullet points found under experience; ATS and recruiters scan bullets "
                   "for achievements.")
        return Category("bullets", 0, ["No experience bullets found."])
    total = quantified = verb_led = 0
    for p in bullets:
        points, problems, fixes = _bullet_problems(p.text)
        total += points
        real = "TODO" not in p.text
        quantified += real and not any(pr.startswith("No number") for pr in problems)
        verb_led += real and not any(pr.startswith("Starts with") for pr in problems)
        if problems:
            findings.append(Finding(f"Resume line {p.line}", _short(p.text), " ".join(problems),
                                    " ".join(fixes)))
    score = round(total / len(bullets))
    notes = [f"{len(bullets)} experience bullets: {verb_led} start with an action verb, "
             f"{quantified} contain a number."]
    if len(bullets) < 3:
        score = min(score, 50)
        red.append(f"Only {len(bullets)} experience bullet(s); aim for 3-6 per recent role.")
    if quantified / len(bullets) < 0.5:
        red.append(f"Only {quantified} of {len(bullets)} bullets are quantified; results "
                   "without numbers rank and read weaker.")
    return Category("bullets", score, notes)


def _estimate_pages(info: DocInfo) -> float:
    lines = 0.0
    for p in info.paragraphs:
        lines += max(1, math.ceil(len(p.text) / 95))
        if p.style.lower().startswith("heading"):
            lines += 1  # spacing around headings
    return round(lines / 46, 2)


def _length(info: DocInfo, cover_words: int | None, red: list[str],
            findings: list[Finding]) -> Category:
    words = word_count(info.text)
    pages = _estimate_pages(info)
    notes = [f"Resume: {words} words, about {pages:.1f} page(s) (estimate)."]
    if words < 250:
        resume_score = 40
        red.append(f"Resume has only {words} words; ATS have little to match and recruiters "
                   "see no evidence. Fill in the TODO items in profile.yaml.")
    elif pages > 2.05:
        resume_score = 40
        red.append(f"Resume runs to about {pages:.1f} pages; keep it to 1-2 pages.")
    elif words < 350:
        resume_score = 75
        notes.append("Thin: 350+ words usually needed to cover keywords and results.")
    else:
        resume_score = 100
    if cover_words is None:
        return Category("length", resume_score, notes)
    distance = max(0, 250 - cover_words, cover_words - 350)
    cover_score = max(0, 100 - distance)
    notes.append(f"Cover letter body: {cover_words} words (target 250-350).")
    if distance:
        findings.append(Finding("Cover letter", f"{cover_words} words",
                                "Body outside 250-350 words.",
                                "Trim to the 2-3 strongest evidence points." if cover_words > 350
                                else "Add a concrete evidence point from your profile."))
    return Category("length", round(0.7 * resume_score + 0.3 * cover_score), notes)


def _cover_content(text: str, company: str, red: list[str], findings: list[Finding]
                   ) -> Category:
    score, notes = 100, []
    _, paragraphs, _ = letter_parts(text)
    body = " ".join(paragraphs)
    if company and company.lower().replace("_", " ") not in body.lower():
        score -= 30
        red.append(f"Cover letter never names {company}: it reads as generic.")
        findings.append(Finding("Cover letter", "(whole letter)", "No company-specific hook.",
                                f"Open with something concrete about {company} and the role."))
    if not re.search(r"\d", body):
        score -= 15
        notes.append("-15: no quantified evidence.")
        findings.append(Finding("Cover letter", "(evidence)", "No numbers in the letter.",
                                "Include 2-3 quantified results from your profile."))
    if not 3 <= len(paragraphs) <= 5:
        score -= 10
        notes.append(f"-10: {len(paragraphs)} paragraph(s); 3-5 short paragraphs scan best.")
    for phrase in CLICHES:
        if phrase in body.lower() or (phrase.startswith(("to whom", "dear sir"))
                                       and phrase in text.lower()):
            score -= 10
            notes.append(f"-10: cliché '{phrase}'.")
            findings.append(Finding("Cover letter", phrase, "Cliché.",
                                    "Replace it with a specific fact from your profile."))
    return Category("cover", max(0, score), notes)


def _rules(resume_path: Path, resume: DocInfo, cover_path: Path | None, cover_text: str,
           expected: dict[str, str] | None, red: list[str], findings: list[Finding]
           ) -> tuple[Category, int]:
    hard = 0
    notes = []
    checks = [("Resume", resume.text, check_output_filename(
        resume_path, "resume", expected=(expected or {}).get("resume")))]
    if cover_path is not None:
        checks.append(("Cover letter", cover_text, check_output_filename(
            cover_path, "cover", expected=(expected or {}).get("cover"))))
    placeholder_hits = 0
    for label, text, filename_issues in checks:
        issues = list(filename_issues) + check_work_authorisation(text)
        if label == "Cover letter":
            issues += check_cover_letter_opening(text)
        for issue in issues:
            hard += 1
            red.append(f"{label}: HARD RULE ({issue.code}) {issue.message}")
            fix = {
                "banned_opening": "Open with a specific hook about the company and role.",
                "work_auth_phrasing": f"Replace with exactly: '{WORK_AUTH_STATEMENT}'.",
                "relocation_reason": "Delete it; never explain relocation or relationships.",
                "filename": "Rename the file to the required pattern.",
            }[issue.code]
            findings.append(Finding(label, _short(issue.excerpt), issue.message, fix))
        for m in PLACEHOLDER_RE.finditer(text):
            placeholder_hits += 1
            findings.append(Finding(label, m.group(0), "Unfilled placeholder.",
                                    "Replace with a real fact or remove it."))
    if placeholder_hits:
        red.append(f"{placeholder_hits} unfilled placeholder(s) (TODO, [Company]...) in the "
                   "documents.")
    score = max(0, 100 - 40 * hard - (25 if placeholder_hits else 0))
    notes.append(f"{hard} hard-rule violation(s); {placeholder_hits} placeholder(s).")
    return Category("rules", score, notes), hard


# --- public API ---------------------------------------------------------------------------

def score_documents(resume_path: str | Path, cover_path: str | Path | None = None,
                    job_text: str | None = None, *, company: str = "",
                    expected_names: dict[str, str] | None = None) -> ScoreReport:
    resume_path = Path(resume_path)
    resume = read_docx(resume_path)
    cover_text, cover_words = "", None
    if cover_path is not None:
        cover_path = Path(cover_path)
        cover_text = letter_text(read_docx(cover_path))
        _, paragraphs, _ = letter_parts(cover_text)
        cover_words = word_count(" ".join(paragraphs))
        if not company:
            m = re.search(r"_CoverLetter_(.+)\.docx$", cover_path.name)
            company = m.group(1).replace("_", " ") if m else ""

    red: list[str] = []
    findings: list[Finding] = []
    categories: list[Category] = []
    matches: list[KeywordMatch] = []
    cover_matches: list[KeywordMatch] = []
    seniority: list[str] = []

    if job_text:
        job = parse_posting(job_text, company=company)
        matches = match_keywords(job.keywords, resume.text)
        if cover_text:
            cover_matches = match_keywords(job.keywords, cover_text)
        kw_score = round(keyword_score(matches))
        categories.append(Category("keywords", kw_score, [
            f"{sum(m.status == 'exact' for m in matches)} exact, "
            f"{sum(m.status == 'synonym' for m in matches)} synonym-only, "
            f"{sum(m.status == 'missing' for m in matches)} missing of {len(matches)} "
            "posting keywords (weighted: must-have x2, nice-to-have x0.5)."]))
        missing_must = [m.keyword.phrase for m in matches if m.status == "missing"
                        and m.keyword.weight >= 2 and m.keyword.category != "seniority"]
        if missing_must:
            red.append("Missing must-have keywords: " + ", ".join(missing_must))
        seniority = [m.keyword.phrase for m in matches if m.keyword.category == "seniority"]
        if job.years_required:
            seniority.append(f"{job.years_required}+ years of experience")

    categories.append(_structure(resume, red, findings))
    categories.append(_bullets(resume, red, findings))
    categories.append(_length(resume, cover_words, red, findings))
    rules, hard = _rules(resume_path, resume, cover_path, cover_text, expected_names, red,
                         findings)
    categories.append(rules)
    weights = dict(WEIGHTS_WITH_JOB if job_text else WEIGHTS_NO_JOB)
    if cover_path is not None:
        categories.append(_cover_content(cover_text, company, red, findings))
        weights["cover"] = COVER_WEIGHT

    overall = round(sum(c.score * weights[c.key] for c in categories) / sum(weights.values()))
    if hard:
        overall = min(overall, 59)
    return ScoreReport(overall=overall, categories=categories, red_flags=red,
                       findings=findings, keyword_matches=matches,
                       cover_keyword_matches=cover_matches, hard_violations=hard,
                       seniority=seniority)


# --- markdown -----------------------------------------------------------------------------

def _keyword_section(report: ScoreReport) -> list[str]:
    if not report.keyword_matches:
        return []
    lines = ["## Keyword match", ""]
    exact = [m.keyword.phrase for m in report.keyword_matches if m.status == "exact"]
    synonym = [m for m in report.keyword_matches if m.status == "synonym"]
    missing = [m for m in report.keyword_matches if m.status == "missing"
               and m.keyword.category != "seniority"]
    lines.append(f"- **Exact phrase found:** {', '.join(exact) or 'none'}")
    if synonym:
        lines.append("- **Synonym only** (many ATS will not count these; use the posting's "
                     "exact phrase if it is true): "
                     + ", ".join(f"{m.keyword.phrase} (resume says '{m.found}')" for m in synonym))
    must = [m.keyword.phrase for m in missing if m.keyword.weight >= 2]
    other = [m.keyword.phrase for m in missing if m.keyword.weight < 2]
    lines.append(f"- **Missing must-haves:** {', '.join(must) or 'none'}")
    lines.append(f"- **Missing others:** {', '.join(other) or 'none'}")
    lines.append("- Add a missing keyword only if it is true: put the evidence in "
                 "profile.yaml first, then regenerate.")
    if report.seniority:
        lines.append(f"- **Seniority signals in the posting** (not scored): "
                     f"{', '.join(report.seniority)}")
    cover = {m.keyword.canonical: m.status for m in report.cover_keyword_matches}
    lines += ["", "| Keyword | Type | Weight | Resume | Cover letter |",
              "|---|---|---|---|---|"]
    for m in report.keyword_matches:
        kw = m.keyword
        lines.append(f"| {kw.phrase} | {kw.category} | {kw.weight:g} | {m.status} | "
                     f"{cover.get(kw.canonical, '-')} |")
    return lines + [""]


def _to_markdown(report: ScoreReport, title: str) -> str:
    lines = [f"# {title}", "",
             f"**Overall: {report.overall}/100 ({report.grade})**", "",
             "Scores are deliberately strict: B means safe to send, A is rare. "
             + ("A hard-rule violation caps the score at 59." if report.hard_violations else ""),
             "", "| Category | Score | Grade |", "|---|---|---|"]
    for c in report.categories:
        lines.append(f"| {LABELS[c.key]} | {c.score} | {c.grade} |")
    lines += ["", "## Red flags", ""]
    lines += [f"- {flag}" for flag in report.red_flags] or ["- None found."]
    lines += ["", "## Line-level fixes", ""]
    if report.findings:
        for i, f in enumerate(report.findings, 1):
            lines.append(f"{i}. **{f.where}**: \"{f.excerpt}\"  ")
            lines.append(f"   Problem: {f.problem}  ")
            lines.append(f"   Fix: {f.fix}")
    else:
        lines.append("- Nothing to fix at line level.")
    lines.append("")
    lines += _keyword_section(report)
    lines += ["## Category notes", ""]
    for c in report.categories:
        lines.append(f"**{LABELS[c.key]} ({c.score}, {c.grade})**")
        lines += [f"- {n}" for n in c.notes] or ["- No deductions."]
        lines.append("")
    for heading, body in report.extra_sections:
        lines += [f"## {heading}", "", body.rstrip(), ""]
    return "\n".join(lines).rstrip() + "\n"
