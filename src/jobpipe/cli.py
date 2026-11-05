"""Command-line interface: jobpipe apply | score | list | status | export."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from zipfile import BadZipFile

import typer
from docx.opc.exceptions import PackageNotFoundError

from jobpipe.llm import provider_is_implicit
from jobpipe.pipeline import PipelineError, run_application
from jobpipe.scorer import score_documents
from jobpipe.tracker import STATUSES, Tracker, TrackerError

DEFAULT_DB = Path("data/jobpipe.db")
DEFAULT_PROFILE = Path("profile/profile.yaml")
DEFAULT_OUT = Path("output")

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


def _warn_if_implicit_provider() -> None:
    if provider_is_implicit():
        typer.secho("No LLM provider configured: using dry-run (deterministic, fact-only "
                    "text). See README, 'LLM providers'.", fg=typer.colors.YELLOW, err=True)


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[Path, typer.Option("--db", envvar="JOBPIPE_DB",
                                     help="SQLite tracker database.")] = DEFAULT_DB,
) -> None:
    ctx.obj = {"db": db}


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
    profile: Annotated[Path, typer.Option("--profile", help="Profile YAML.")] = DEFAULT_PROFILE,
    out: Annotated[Path, typer.Option("--out", help="Output folder.")] = DEFAULT_OUT,
    template: Annotated[str, typer.Option("--template", help="Template name or YAML path.")
                        ] = "classic",
) -> None:
    """Generate the tailored resume, cover letter and ATS report, and log the application."""
    try:
        result = run_application(
            job_text=job.read_text(encoding="utf-8"), company=company, role=role, url=url,
            profile_path=profile, out_dir=out, db_path=_db(ctx), template=template)
    except PipelineError as exc:
        _fail(str(exc))
    _warn_if_implicit_provider()
    paths = result.paths
    if result.blocked:
        _fail(f"Generated documents break a hard rule; see {paths['report']}. "
              "Nothing was logged to the tracker.", code=1)

    report = result.report
    typer.echo(f"Resume:       {paths['resume']}")
    typer.echo(f"Cover letter: {paths['cover']}")
    typer.echo(f"ATS report:   {paths['report']}")
    typer.echo(f"ATS score:    {report.overall}/100 ({report.grade})")
    typer.echo(f"Tracked as application #{result.app_id} (status: {result.status})")
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


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", help="Interface to bind. Keep 127.0.0.1 "
                                      "unless you know you want it on your network.")
                    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")] = 8000,
    profile: Annotated[Path, typer.Option("--profile", help="Profile YAML.")] = DEFAULT_PROFILE,
    out: Annotated[Path, typer.Option("--out", help="Output folder.")] = DEFAULT_OUT,
) -> None:
    """Start the web interface (needs the 'web' extra: pip install -e '.[web]')."""
    try:
        import uvicorn

        from jobpipe.web import create_app
    except ImportError:
        _fail("The web interface needs extra packages. Install them with: "
              "pip install -e '.[web]'")
    _warn_if_implicit_provider()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback:
        typer.secho(f"Warning: binding to {host} makes your profile, documents and tracker "
                    "readable by anyone who can reach this machine.", fg=typer.colors.YELLOW,
                    err=True)
    web_app = create_app(profile_path=profile, out_dir=out, db_path=_db(ctx),
                         allowed_hosts=None if loopback else ["*"])
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    typer.echo(f"jobpipe web interface: http://{shown}:{port}  (Ctrl+C to stop)")
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    app()
