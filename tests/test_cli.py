import csv

import pytest
from docx import Document
from typer.testing import CliRunner

from jobpipe.cli import app
from jobpipe.tracker import COLUMNS

from .conftest import EXAMPLE_PROFILE, SAMPLE_POSTING, TEMPLATE_PROFILE

runner = CliRunner()


def _apply(tmp_path, company="Acme", profile=EXAMPLE_PROFILE, *extra):
    return runner.invoke(app, [
        "--db", str(tmp_path / "jobs.db"), "apply", "--job", str(SAMPLE_POSTING),
        "--company", company, "--profile", str(profile), "--out", str(tmp_path / "output"),
        *extra,
    ])


def test_end_to_end_demo(tmp_path):
    result = _apply(tmp_path)
    assert result.exit_code == 0, result.output
    out = tmp_path / "output"
    assert sorted(p.name for p in out.iterdir()) == [
        "Acme_ats_report.md", "Taylor_Sample_CoverLetter_Acme.docx",
        "Taylor_Sample_Resume_Acme.docx"]
    for name in ("Taylor_Sample_Resume_Acme.docx", "Taylor_Sample_CoverLetter_Acme.docx"):
        assert len(Document(str(out / name)).tables) == 0
    report = (out / "Acme_ats_report.md").read_text(encoding="utf-8")
    for needle in ("# ATS report: Acme – Customer Success Manager", "## Red flags",
                   "## Claims check", "Provider: dry-run",
                   "Final documents: every claim is supported by profile.yaml."):
        assert needle in report
    assert "Tracked as application #1" in result.output

    listed = runner.invoke(app, ["--db", str(tmp_path / "jobs.db"), "list"])
    assert "Acme" in listed.output and "prepared" in listed.output


def test_real_template_profile_produces_named_files_and_gap_report(tmp_path):
    result = _apply(tmp_path, "Acme", TEMPLATE_PROFILE)
    assert result.exit_code == 0, result.output
    out = tmp_path / "output"
    assert (out / "Ranjeet_Skanda_Resume_Acme.docx").is_file()
    assert (out / "Ranjeet_Skanda_CoverLetter_Acme.docx").is_file()
    report = (out / "Acme_ats_report.md").read_text(encoding="utf-8")
    assert "## Profile gaps" in report and "contact.email" in report


def test_company_name_is_sanitised(tmp_path):
    result = _apply(tmp_path, "Acme Payments s.r.o./CZ")
    assert result.exit_code == 0, result.output
    names = sorted(p.name for p in (tmp_path / "output").iterdir())
    assert names == ["Acme_Payments_s_r_o_CZ_ats_report.md",
                     "Taylor_Sample_CoverLetter_Acme_Payments_s_r_o_CZ.docx",
                     "Taylor_Sample_Resume_Acme_Payments_s_r_o_CZ.docx"]


def test_status_and_export(tmp_path):
    assert _apply(tmp_path).exit_code == 0
    db = ["--db", str(tmp_path / "jobs.db")]
    result = runner.invoke(app, [*db, "status", "1", "interview"])
    assert result.exit_code == 0 and "interview" in result.output
    bad = runner.invoke(app, [*db, "status", "1", "ghosted"])
    assert bad.exit_code == 2
    missing = runner.invoke(app, [*db, "status", "42", "offer"])
    assert missing.exit_code == 2

    out = tmp_path / "apps.csv"
    result = runner.invoke(app, [*db, "export", "--csv", "--out", str(out)])
    assert result.exit_code == 0, result.output
    with out.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert tuple(rows[0]) == COLUMNS
    assert rows[0]["status"] == "interview"
    assert rows[0]["url"] == "https://careers.acme.example/jobs/csm-prague"
    assert runner.invoke(app, [*db, "export"]).exit_code == 2


def test_score_command(tmp_path):
    assert _apply(tmp_path).exit_code == 0
    out = tmp_path / "output"
    result = runner.invoke(app, [
        "score", "--resume", str(out / "Taylor_Sample_Resume_Acme.docx"),
        "--cover", str(out / "Taylor_Sample_CoverLetter_Acme.docx"),
        "--job", str(SAMPLE_POSTING)])
    assert result.exit_code == 0, result.output
    assert "**Overall:" in result.output and "| Keyword match |" in result.output

    report = tmp_path / "r.md"
    result = runner.invoke(app, ["score", "--resume", str(out / "Taylor_Sample_Resume_Acme.docx"),
                                 "--out", str(report)])
    assert result.exit_code == 0 and report.is_file()
    assert "Keyword match" not in report.read_text().split("## Red flags")[0]


@pytest.mark.parametrize("args, message", [
    (["--profile", "missing.yaml"], "Profile not found"),
    (["--template", "fancy"], "Template 'fancy' not found"),
])
def test_apply_errors_are_clear(tmp_path, args, message):
    result = _apply(tmp_path, "Acme", EXAMPLE_PROFILE, *args)
    assert result.exit_code == 2
    assert message in result.output


def test_misconfigured_provider_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBPIPE_PROVIDER", "anthropic")
    result = _apply(tmp_path)
    assert result.exit_code == 2 and "ANTHROPIC_API_KEY" in result.output


def test_score_rejects_non_docx(tmp_path):
    fake = tmp_path / "Taylor_Sample_Resume_Acme.docx"
    fake.write_text("not a docx")
    result = runner.invoke(app, ["score", "--resume", str(fake)])
    assert result.exit_code == 2 and "Could not read" in result.output


def test_list_when_empty(tmp_path):
    result = runner.invoke(app, ["--db", str(tmp_path / "jobs.db"), "list"])
    assert "No applications tracked yet." in result.output


def test_rejected_model_draft_is_reported(tmp_path, monkeypatch, profile):
    import json

    from jobpipe import cli
    from jobpipe.llm import DryRunClient

    bad_letter = ("Dear Hiring Team at Acme,\n\nI am writing to apply for the Customer Success "
                  "Manager role.\n\nKind regards,\nTaylor Sample")
    scripted = DryRunClient(responses=[
        json.dumps(profile.experience[0].real_bullets),
        json.dumps(profile.experience[1].real_bullets),
        profile.summary,
        bad_letter,
    ])
    monkeypatch.setattr(cli, "get_client", lambda: scripted)
    result = _apply(tmp_path)
    assert result.exit_code == 0, result.output
    report = (tmp_path / "output" / "Acme_ats_report.md").read_text(encoding="utf-8")
    assert "Draft 1 rejected: banned_opening" in report
    letter = Document(str(tmp_path / "output" / "Taylor_Sample_CoverLetter_Acme.docx"))
    assert "I am writing to apply" not in "\n".join(p.text for p in letter.paragraphs)


def test_documents_breaking_a_hard_rule_are_not_tracked(tmp_path, monkeypatch):
    from jobpipe import cli
    from jobpipe.tailor import CoverLetter

    bad = ("Dear Hiring Team at Acme,\n\nI am writing to apply for this role. I hold a Czech "
           "work permit.\n\nKind regards,\nTaylor Sample")
    monkeypatch.setattr(cli, "write_cover_letter",
                        lambda *a, **k: CoverLetter(text=bad, attempts=1, used_fallback=False))
    result = _apply(tmp_path)
    assert result.exit_code == 1
    assert "break a hard rule" in result.output
    report = (tmp_path / "output" / "Acme_ats_report.md").read_text(encoding="utf-8")
    assert "banned_opening" in report and "work_auth_phrasing" in report
    listed = runner.invoke(app, ["--db", str(tmp_path / "jobs.db"), "list"])
    assert "No applications tracked yet." in listed.output
