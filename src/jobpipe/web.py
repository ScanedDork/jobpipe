"""Local web interface: a JSON API over the pipeline, plus a static single-page frontend.

Start it with ``jobpipe serve``. It binds to 127.0.0.1 by default and only answers requests
addressed to a loopback host name, so other sites cannot reach it through DNS rebinding.
"""

from __future__ import annotations

import csv
import io
import re
import tempfile
from pathlib import Path
from typing import Annotated
from zipfile import BadZipFile

from docx.opc.exceptions import PackageNotFoundError
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from jobpipe import __version__
from jobpipe.llm import LLMError, get_client, provider_is_implicit
from jobpipe.pipeline import PipelineError, run_application
from jobpipe.profile import ProfileError, is_placeholder, load_profile
from jobpipe.render import available_templates
from jobpipe.scorer import score_documents
from jobpipe.tracker import COLUMNS, STATUSES, Tracker, TrackerError

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\.(?:docx|md)$")
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]", "testserver"]
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_POSTING_CHARS = 50_000
MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
}


class ApplyRequest(BaseModel):
    posting: str = Field(min_length=1, max_length=MAX_POSTING_CHARS)
    company: str = Field(min_length=1, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, max_length=2000)
    template: str = Field(default="classic", max_length=100)


class StatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=40)


def _profile_summary(profile_path: Path) -> dict:
    info: dict = {"path": str(profile_path), "ok": False, "error": None}
    try:
        p = load_profile(profile_path)
    except ProfileError as exc:
        info["error"] = str(exc)
        return info

    def real(value: str) -> str:
        return "" if is_placeholder(value) else value

    info.update(
        ok=True,
        name=p.preferred_name,
        full_name=p.full_name,
        headline=real(p.headline),
        location=real(p.location),
        contact=[x for x in (p.email, p.phone, p.linkedin, p.website) if not is_placeholder(x)],
        work_authorisation=p.work_authorisation,
        summary=real(p.summary),
        roles=[{"title": e.title, "company": e.company, "dates": e.dates,
                "bullets": len(e.real_bullets)} for e in p.experience],
        education=[f"{e.degree}, {e.institution}" for e in p.education],
        certifications=[c.name for c in p.certifications if not is_placeholder(c.name)],
        certification_hours=real(p.certification_hours),
        skills=[s for s in p.skills if not is_placeholder(s)],
        languages=[x for x in p.languages if not is_placeholder(x)],
        target_roles=p.target_roles,
        placeholders=p.placeholders(),
    )
    return info


def create_app(*, profile_path: str | Path, out_dir: str | Path, db_path: str | Path,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    profile_path, out_dir, db_path = Path(profile_path), Path(out_dir), Path(db_path)
    app = FastAPI(title="jobpipe", version=__version__, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def file_info(path: str | Path | None) -> dict | None:
        if not path:
            return None
        path = Path(path)
        try:
            inside = path.resolve().parent == out_dir.resolve()
        except OSError:
            return None
        if inside and SAFE_FILE_RE.match(path.name) and path.is_file():
            return {"name": path.name, "url": f"/api/files/{path.name}"}
        return None

    def report_for(resume_path: str) -> dict | None:
        m = re.search(r"_Resume_([A-Za-z0-9_-]+)\.docx$", resume_path or "")
        return file_info(out_dir / f"{m.group(1)}_ats_report.md") if m else None

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/status")
    def status() -> dict:
        provider, provider_error = None, None
        try:
            provider = get_client().name
        except LLMError as exc:
            provider_error = str(exc)
        return {
            "version": __version__,
            "provider": provider,
            "provider_implicit": provider_is_implicit(),
            "provider_error": provider_error,
            "profile": _profile_summary(profile_path),
            "templates": available_templates(),
            "statuses": list(STATUSES),
            "out_dir": str(out_dir),
        }

    @app.post("/api/apply")
    def apply(req: ApplyRequest) -> dict:
        try:
            result = run_application(
                job_text=req.posting, company=req.company, role=req.role, url=req.url,
                profile_path=profile_path, out_dir=out_dir, db_path=db_path,
                template=req.template)
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "app_id": result.app_id,
            "blocked": result.blocked,
            "company": result.company,
            "role": result.role,
            "url": result.url,
            "provider": result.provider,
            "status": result.status if result.app_id else None,
            "files": {kind: file_info(path) for kind, path in result.paths.items()},
            "report": result.report.to_dict(),
            "cover_letter": result.letter.text,
        }

    def save_upload(upload: UploadFile, folder: Path) -> Path:
        name = Path(upload.filename or "").name
        if not name.lower().endswith(".docx"):
            raise HTTPException(status_code=400, detail=f"'{name or 'file'}' is not a .docx file")
        data = upload.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"'{name}' is larger than 5 MB")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        target.write_bytes(data)
        return target

    @app.post("/api/score")
    def score(resume: Annotated[UploadFile, File()],
              cover: Annotated[UploadFile | None, File()] = None,
              posting: Annotated[str, Form(max_length=MAX_POSTING_CHARS)] = "") -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            resume_path = save_upload(resume, Path(tmp) / "resume")
            cover_path = (save_upload(cover, Path(tmp) / "cover")
                          if cover is not None and cover.filename else None)
            try:
                report = score_documents(resume_path, cover_path, posting.strip() or None)
            except (PackageNotFoundError, BadZipFile, KeyError, ValueError) as exc:
                raise HTTPException(status_code=400,
                                    detail=f"Could not read the document as .docx: {exc}"
                                    ) from exc
            return {"report": report.to_dict(),
                    "markdown": report.to_markdown(f"ATS report: {resume_path.name}")}

    @app.get("/api/applications")
    def applications() -> list[dict]:
        rows = Tracker(db_path).all()
        for row in rows:
            row["files"] = {"resume": file_info(row["resume_path"]),
                            "cover": file_info(row["cover_path"]),
                            "report": report_for(row["resume_path"])}
        return rows

    @app.patch("/api/applications/{app_id}")
    def set_status(app_id: int, req: StatusRequest) -> dict:
        tracker = Tracker(db_path)
        try:
            tracker.get(app_id)
        except TrackerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            tracker.set_status(app_id, req.status)
        except TrackerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return tracker.get(app_id)

    @app.get("/api/applications.csv")
    def export_csv() -> Response:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(Tracker(db_path).all())
        return Response(buffer.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="applications.csv"'})

    @app.get("/api/files/{name}")
    def download(name: str) -> FileResponse:
        path = out_dir / name
        if not SAFE_FILE_RE.match(name) or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix], filename=name)

    return app
