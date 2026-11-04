"""Command-line interface: jobpipe apply | score | list | status | export."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from zipfile import BadZipFile

import typer
from docx.opc.exceptions import PackageNotFoundError

from jobpipe.keywords import parse_posting
from jobpipe.llm import LLMError, get_client, provider_is_implicit
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
from jobpipe.tracker import STATUSES, Tracker, TrackerError
from jobpipe.validators import letter_claims_check, output_filenames

DEFAULT_DB = Path("data/jobpipe.db")

app = typer.Typer(
    help="Turn a job posting into a tailored resume, cover letter and ATS audit, and track "
         "your applications.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _fail(message: str, code: int = 2) -> None:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _db(ctx: typer.Context) -> Path:
    return ctx.obj["db"]


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path, typer.Option("--db", envvar="JOBPIPE_DB",
                                     help="SQLite tracker database.")] = DEFAULT_DB,
) -> None:
    ctx.obj = {"db": db}


def _generation_sections(client_name: str, tailored: TailoredResume, letter: CoverLetter,
                         claims_remaining: list, placeholders: list[str]
                         ) -> list[tuple[str, str]]:
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


@app.command()
def apply(
    ctx: typer.Context,
    job: Annotated[Path, typer.Option("--job", exists=True, dir_okay=False, readable=True,
                                      help="Job posting as a plain-text file.")],
    company: Annotated[str, typer.Option("--company", help="Company name.")],
    role: Annotated[str | None, typer.Option(
        "--role", help="Role title (default: detected from the posting).")] = None,
    url: Annotated[str | None, typer.Option(
        "--url", help="Posting URL (default: first URL in the posting).")] = None,
    profile: Annotated[Path, typer.Option("--profile", help="Profile YAML.")] = Path(
        "profile/profile.yaml"),
    out: Annotated[Path, typer.Option("--out", help="Output folder.")] = Path("output"),
    template: Annotated[str, typer.Option("--template", help="Template name or YAML path.")
                        ] = "classic",
) -> None:
    """Generate the tailored resume, cover letter and ATS report, and log the application."""
    try:
        prof = load_profile(profile)
        tpl = load_template(template)
        client = get_client()
        paths = output_paths(out, prof.preferred_name, company)
    except (ProfileError, TemplateError, LLMError, ValueError) as exc:
        _fail(str(exc))
    if provider_is_implicit():
        typer.secho("No LLM provider configured: using dry-run (deterministic, fact-only "
                    "text). See README, 'LLM providers'.", fg=typer.colors.YELLOW, err=True)

    job_text = job.read_text(encoding="utf-8")
    posting = parse_posting(job_text, title=role, company=company)
    role_name = role or posting.title
    if not role_name:
        _fail("Could not detect the role title from the posting; pass --role.")

    tailored = tailor_resume(prof, posting, client, company=company, role=role_name)
    letter = write_cover_letter(tailored, posting, client, company=company, role=role_name)
    render_resume(tailored, paths["resume"], tpl)
    render_cover_letter(letter.text, prof, company, paths["cover"], tpl)

    report: ScoreReport = score_documents(
        paths["resume"], paths["cover"], job_text, company=company,
        expected_names=output_filenames(prof.preferred_name, company))
    remaining = letter_claims_check(letter.text, prof.fact_texts(), allow=[company, role_name],
                                    known_names=job_text)
    report.extra_sections += _generation_sections(client.name, tailored, letter, remaining,
                                                  prof.placeholders())
    paths["report"].write_text(report.to_markdown(f"ATS report: {company} – {role_name}"),
                               encoding="utf-8")

    if report.hard_violations or remaining:
        _fail(f"Generated documents break a hard rule; see {paths['report']}. "
              "Nothing was logged to the tracker.", code=1)

    notes = f"provider={client.name}" + ("; fallback cover letter" if letter.used_fallback
                                         else "")
    app_id = Tracker(_db(ctx)).add(
        company=company, role=role_name, url=url or posting.url,
        resume_path=str(paths["resume"]), cover_path=str(paths["cover"]),
        ats_score=report.overall, notes=notes)

    typer.echo(f"Resume:       {paths['resume']}")
    typer.echo(f"Cover letter: {paths['cover']}")
    typer.echo(f"ATS report:   {paths['report']}")
    typer.echo(f"ATS score:    {report.overall}/100 ({report.grade})")
    typer.echo(f"Tracked as application #{app_id} (status: prepared)")
    for flag in report.red_flags:
        typer.secho(f"  ! {flag}", fg=typer.colors.YELLOW)


@app.command()
def score(
    resume: Annotated[Path, typer.Option("--resume", exists=True, dir_okay=False,
                                         help="Resume .docx")],
    cover: Annotated[Path | None, typer.Option("--cover", exists=True, dir_okay=False,
                                               help="Cover letter .docx")] = None,
    job: Annotated[Path | None, typer.Option("--job", exists=True, dir_okay=False,
                                             help="Job posting .txt (enables keywords)")
                   ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write the markdown report here "
                                             "instead of printing it.")] = None,
) -> None:
    """Score a resume (and optional cover letter) for ATS readiness."""
    job_text = job.read_text(encoding="utf-8") if job else None
    try:
        report = score_documents(resume, cover, job_text)
    except (PackageNotFoundError, BadZipFile, KeyError, ValueError) as exc:
        _fail(f"Could not read the document as .docx: {exc}")
    markdown = report.to_markdown(f"ATS report: {resume.name}")
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        typer.echo(f"{out}: {report.overall}/100 ({report.grade})")
    else:
        typer.echo(markdown)


@app.command("list")
def list_applications(ctx: typer.Context) -> None:
    """List tracked applications."""
    rows = Tracker(_db(ctx)).all()
    if not rows:
        typer.echo("No applications tracked yet.")
        return
    cols = [("id", 4), ("date", 10), ("company", 20), ("role", 28), ("status", 10),
            ("ats_score", 5)]
    typer.echo("  ".join(name.upper().ljust(width) for name, width in cols))
    for row in rows:
        cells = []
        for name, width in cols:
            value = "" if row[name] is None else str(row[name])
            cells.append((value[: width - 1] + "…" if len(value) > width else value).ljust(width))
        typer.echo("  ".join(cells).rstrip())


@app.command()
def status(
    ctx: typer.Context,
    app_id: Annotated[int, typer.Argument(help="Application id (see 'jobpipe list').")],
    new_status: Annotated[str, typer.Argument(help="One of: " + ", ".join(STATUSES))],
) -> None:
    """Update an application's status, e.g. 'jobpipe status 3 interview'."""
    try:
        Tracker(_db(ctx)).set_status(app_id, new_status)
    except TrackerError as exc:
        _fail(str(exc))
    typer.echo(f"Application #{app_id}: status set to {new_status.strip().lower()}")


@app.command()
def export(
    ctx: typer.Context,
    as_csv: Annotated[bool, typer.Option("--csv", help="Export as CSV.")] = False,
    out: Annotated[Path, typer.Option("--out", help="Destination file.")] = Path(
        "output/applications.csv"),
) -> None:
    """Export the tracker (CSV)."""
    if not as_csv:
        _fail("Choose an export format: --csv")
    count = Tracker(_db(ctx)).export_csv(out)
    typer.echo(f"Exported {count} application(s) to {out}")


if __name__ == "__main__":  # pragma: no cover
    app()
