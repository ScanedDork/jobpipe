import csv
import io

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from docx import Document  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jobpipe import pipeline, web  # noqa: E402
from jobpipe.tailor import CoverLetter  # noqa: E402
from jobpipe.tracker import COLUMNS  # noqa: E402
from jobpipe.web import create_app  # noqa: E402

from .conftest import EXAMPLE_PROFILE  # noqa: E402

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def client(tmp_path):
    app = create_app(profile_path=EXAMPLE_PROFILE, out_dir=tmp_path / "output",
                     db_path=tmp_path / "jobs.db")
    return TestClient(app)


@pytest.fixture
def applied(client, posting_text):
    response = client.post("/api/apply", json={"posting": posting_text, "company": "Acme"})
    assert response.status_code == 200, response.text
    return response.json()


def test_frontend_is_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="apply-form"' in page.text and "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_status_reports_provider_profile_and_templates(client):
    data = client.get("/api/status").json()
    assert data["provider"] == "dry-run" and data["provider_error"] is None
    assert data["profile"]["ok"] and data["profile"]["name"] == "Taylor Sample"
    assert data["profile"]["placeholders"] == []
    assert {"classic", "compact"} <= set(data["templates"])
    assert data["statuses"][0] == "prepared"


def test_status_with_missing_profile(tmp_path):
    app = create_app(profile_path=tmp_path / "nope.yaml", out_dir=tmp_path, db_path=tmp_path / "db")
    profile = TestClient(app).get("/api/status").json()["profile"]
    assert profile["ok"] is False and "Profile not found" in profile["error"]


def test_apply_end_to_end(client, applied):
    assert applied["app_id"] == 1 and applied["blocked"] is False
    assert applied["role"] == "Customer Success Manager"
    assert applied["url"] == "https://careers.acme.example/jobs/csm-prague"
    assert {k: v["name"] for k, v in applied["files"].items()} == {
        "resume": "Taylor_Sample_Resume_Acme.docx",
        "cover": "Taylor_Sample_CoverLetter_Acme.docx",
        "report": "Acme_ats_report.md",
    }
    report = applied["report"]
    assert 0 <= report["overall"] <= 100 and report["grade"] in "ABCDF"
    assert {c["key"] for c in report["categories"]} >= {"keywords", "structure", "rules"}
    assert any(k["phrase"] == "Gainsight" and k["resume"] == "missing"
               for k in report["keywords"])
    assert [s["heading"] for s in report["sections"]][:2] == ["Generation notes",
                                                              "Claims check"]
    assert applied["cover_letter"].startswith("Dear Hiring Team at Acme,")

    resume = client.get(applied["files"]["resume"]["url"])
    assert resume.status_code == 200 and resume.headers["content-type"] == DOCX
    assert len(Document(io.BytesIO(resume.content)).tables) == 0
    report_md = client.get(applied["files"]["report"]["url"])
    assert report_md.text.startswith("# ATS report: Acme")


@pytest.mark.parametrize("payload, code, message", [
    ({"posting": "x"}, 422, "company"),
    ({"posting": "   ", "company": "Acme"}, 400, "empty"),
    ({"posting": "Account Manager\nRequirements:\n- CRM", "company": "!!!"}, 400,
     "usable characters"),
    ({"posting": "Account Manager", "company": "Acme", "template": "fancy"}, 400,
     "Template 'fancy' not found"),
])
def test_apply_validation(client, payload, code, message):
    response = client.post("/api/apply", json=payload)
    assert response.status_code == code
    assert message in response.text


def test_apply_blocked_by_hard_rule_is_not_tracked(client, posting_text, monkeypatch):
    bad = ("Dear Hiring Team at Acme,\n\nI am writing to apply for this role.\n\n"
           "Kind regards,\nTaylor Sample")
    monkeypatch.setattr(pipeline, "write_cover_letter",
                        lambda *a, **k: CoverLetter(text=bad, attempts=1, used_fallback=False))
    data = client.post("/api/apply", json={"posting": posting_text, "company": "Acme"}).json()
    assert data["blocked"] is True and data["app_id"] is None
    assert any("banned_opening" in flag for flag in data["report"]["red_flags"])
    assert client.get("/api/applications").json() == []


def test_tracker_list_status_and_csv(client, applied):
    rows = client.get("/api/applications").json()
    assert len(rows) == 1
    assert rows[0]["files"]["resume"]["url"] == "/api/files/Taylor_Sample_Resume_Acme.docx"
    assert rows[0]["files"]["report"]["name"] == "Acme_ats_report.md"

    updated = client.patch("/api/applications/1", json={"status": "interview"})
    assert updated.status_code == 200 and updated.json()["status"] == "interview"
    assert client.patch("/api/applications/1", json={"status": "ghosted"}).status_code == 400
    assert client.patch("/api/applications/99", json={"status": "offer"}).status_code == 404

    export = client.get("/api/applications.csv")
    assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
    reader = csv.DictReader(io.StringIO(export.text))
    assert tuple(reader.fieldnames) == COLUMNS
    assert next(reader)["status"] == "interview"


@pytest.mark.parametrize("name", ["..%2F..%2Fetc%2Fpasswd", "notes.txt", "missing.docx",
                                  ".hidden.md", "a%00.docx"])
def test_downloads_are_restricted_to_generated_files(client, applied, name):
    assert client.get(f"/api/files/{name}").status_code == 404


def test_score_upload(client, applied, posting_text):
    resume = client.get(applied["files"]["resume"]["url"]).content
    cover = client.get(applied["files"]["cover"]["url"]).content
    response = client.post(
        "/api/score",
        files={"resume": ("Taylor_Sample_Resume_Acme.docx", resume, DOCX),
               "cover": ("Taylor_Sample_CoverLetter_Acme.docx", cover, DOCX)},
        data={"posting": posting_text})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["report"]["keywords"] and data["report"]["hard_violations"] == 0
    assert "cover" in {c["key"] for c in data["report"]["categories"]}
    assert data["markdown"].startswith("# ATS report: Taylor_Sample_Resume_Acme.docx")


def test_score_upload_filename_rule_still_applies(client, applied):
    resume = client.get(applied["files"]["resume"]["url"]).content
    data = client.post("/api/score", files={"resume": ("my resume.docx", resume, DOCX)}).json()
    assert data["report"]["hard_violations"] == 1
    assert "keywords" not in {c["key"] for c in data["report"]["categories"]}


@pytest.mark.parametrize("filename, content, code", [
    ("resume.pdf", b"%PDF-1.4", 400),
    ("Taylor_Sample_Resume_Acme.docx", b"not a zip", 400),
])
def test_score_rejects_bad_uploads(client, filename, content, code):
    response = client.post("/api/score", files={"resume": (filename, content, DOCX)})
    assert response.status_code == code


def test_score_rejects_oversized_uploads(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD_BYTES", 10)
    response = client.post("/api/score",
                           files={"resume": ("Taylor_Sample_Resume_Acme.docx", b"x" * 50, DOCX)})
    assert response.status_code == 413


def test_foreign_host_names_are_rejected(tmp_path):
    app = create_app(profile_path=EXAMPLE_PROFILE, out_dir=tmp_path, db_path=tmp_path / "db")
    evil = TestClient(app, base_url="http://attacker.example")
    assert evil.get("/api/status").status_code == 400


def test_cross_site_form_posts_are_not_parsed_as_json(client, posting_text):
    import json

    response = client.post("/api/apply", content=json.dumps({"posting": posting_text,
                                                             "company": "Acme"}),
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 422
    assert client.get("/api/applications").json() == []
