"""The apply pipeline, shared by the CLI and the web interface.

profile + posting -> tailored resume and cover letter -> .docx files -> ATS report -> tracker
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jobpipe.keywords import parse_posting
from jobpipe.llm import LLMClient, LLMError, get_client
from jobpipe.profile import ProfileError, load_profile
from jobpipe.render import (
    TemplateError,
    load_template,
    output_paths,
    render_cover_letter,
    render_resume,
)
from jobpipe.scorer import ScoreReport, score_documents
from jobpipe.tailor import CoverLetter, TailoredResume, tailor_resume, write_cover_letter
from jobpipe.tracker import DEFAULT_STATUS, Tracker
from jobpipe.validators import Issue, letter_claims_check, output_filenames


class PipelineError(ValueError):
    """The inputs cannot produce documents (bad profile, template, provider or posting)."""


@dataclass
class ApplicationResult:
    company: str
    role: str
    url: str
    paths: dict[str, Path]
    report: ScoreReport
    provider: str
    tailored: TailoredResume
    letter: CoverLetter
    remaining_claims: list[Issue] = field(default_factory=list)
    app_id: int | None = None  # None when the documents broke a hard rule (not tracked)
    status: str = DEFAULT_STATUS

    @property
    def blocked(self) -> bool:
        return bool(self.report.hard_violations or self.remaining_claims)


def generation_sections(client_name: str, tailored: TailoredResume, letter: CoverLetter,
                        claims_remaining: list[Issue], placeholders: list[str]
                        ) -> list[tuple[str, str]]:
    """Extra report sections describing how the documents were produced."""
    notes = [f"- Provider: {client_name}"
             + (" (deterministic, fact-only text; no LLM was called)"
                if client_name == "dry-run" else "")]
    notes.append(f"- Cover letter: {letter.attempts} attempt(s); "
                 f"fact-only fallback used: {'yes' if letter.used_fallback else 'no'}")
    for attempt, issues in letter.rejected:
        reasons = "; ".join(f"{i.code}: {i.message}" for i in issues)
        notes.append(f"- Draft {attempt} rejected: {reasons}")
    notes += [f"- {w.message}" for w in letter.warnings]
    notes += [f"- {n}" for n in tailored.notes + letter.notes]

    claims = []
    if tailored.rejected:
        claims.append(f"- {len(tailored.rejected)} issue(s) found in model rephrasings; the "
                      "affected text was reverted to your profile wording:")
        claims += [f"  - {i.code}: {i.message} (\"{i.excerpt}\")" for i in tailored.rejected]
    else:
        claims.append("- No model rephrasing was rejected.")
    if claims_remaining:
        claims.append("- Unsupported claims remain in the final documents:")
        claims += [f"  - {i.code}: {i.message} (\"{i.excerpt}\")" for i in claims_remaining]
    else:
        claims.append("- Final documents: every claim is supported by profile.yaml.")

    sections = [("Generation notes", "\n".join(notes)), ("Claims check", "\n".join(claims))]
    if placeholders:
        sections.append(("Profile gaps", "These profile fields still hold TODO placeholders "
                         "and were left out. Fill them with real facts only:\n"
                         + "\n".join(f"- {p}" for p in placeholders)))
    return sections


def run_application(*, job_text: str, company: str, profile_path: str | Path,
                    out_dir: str | Path, db_path: str | Path, role: str | None = None,
                    url: str | None = None, template: str = "classic",
                    client: LLMClient | None = None) -> ApplicationResult:
    """Generate, score and (unless a hard rule is broken) track one application."""
    company = company.strip()
    if not company:
        raise PipelineError("Company name is required.")
    if not job_text.strip():
        raise PipelineError("The job posting is empty.")
    try:
        profile = load_profile(profile_path)
        tpl = load_template(template)
        client = client or get_client()
        paths = output_paths(out_dir, profile.preferred_name, company)
    except (ProfileError, TemplateError, LLMError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc

    role = (role or "").strip() or None
    posting = parse_posting(job_text, title=role, company=company)
    role_name = role or posting.title
    if not role_name:
        raise PipelineError("Could not detect the role title from the posting; give the role.")

    tailored = tailor_resume(profile, posting, client, company=company, role=role_name)
    letter = write_cover_letter(tailored, posting, client, company=company, role=role_name)
    render_resume(tailored, paths["resume"], tpl)
    render_cover_letter(letter.text, profile, company, paths["cover"], tpl)

    report = score_documents(
        paths["resume"], paths["cover"], job_text, company=company,
        expected_names=output_filenames(profile.preferred_name, company))
    remaining = letter_claims_check(letter.text, profile.fact_texts(),
                                    allow=[company, role_name], known_names=job_text)
    report.extra_sections += generation_sections(client.name, tailored, letter, remaining,
                                                 profile.placeholders())
    paths["report"].write_text(report.to_markdown(f"ATS report: {company} – {role_name}"),
                               encoding="utf-8")

    result = ApplicationResult(
        company=company, role=role_name, url=(url or "").strip() or posting.url, paths=paths,
        report=report, provider=client.name, tailored=tailored, letter=letter,
        remaining_claims=remaining)
    if not result.blocked:
        notes = f"provider={client.name}" + ("; fallback cover letter"
                                             if letter.used_fallback else "")
        result.app_id = Tracker(db_path).add(
            company=company, role=role_name, url=result.url,
            resume_path=str(paths["resume"]), cover_path=str(paths["cover"]),
            ats_score=report.overall, notes=notes)
    return result
