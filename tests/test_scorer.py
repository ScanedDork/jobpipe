from datetime import date

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from jobpipe.llm import DryRunClient
from jobpipe.render import load_template, output_paths, render_cover_letter, render_resume
from jobpipe.scorer import grade, letter_text, read_docx, score_documents
from jobpipe.tailor import tailor_resume, write_cover_letter
from jobpipe.validators import WORK_AUTH_STATEMENT, output_filenames

ROLE = "Customer Success Manager"


@pytest.fixture
def good_docs(profile, job, tmp_path):
    llm = DryRunClient()
    tailored = tailor_resume(profile, job, llm, company="Acme", role=ROLE)
    letter = write_cover_letter(tailored, job, llm, company="Acme", role=ROLE)
    paths = output_paths(tmp_path, profile.preferred_name, "Acme")
    tpl = load_template("classic")
    render_resume(tailored, paths["resume"], tpl)
    render_cover_letter(letter.text, profile, "Acme", paths["cover"], tpl,
                        today=date(2026, 11, 4))
    return paths


def _letter_docx(path, opening):
    doc = Document()
    for line in ["Taylor Sample", "taylor@example.com | +420 600 000 000", "4 November 2026",
                 "Acme", "Dear Hiring Team at Acme,", opening,
                 "At Northwind I grew net revenue retention from 97% to 112%.",
                 "I would welcome a conversation.", "Kind regards,", "Taylor Sample"]:
        doc.add_paragraph(line)
    doc.save(str(path))
    return path


def _bad_resume(path):
    doc = Document()
    section = doc.sections[0]
    cols = OxmlElement("w:cols")
    cols.set(qn("w:num"), "2")
    section._sectPr.append(cols)
    section.header.paragraphs[0].text = "taylor@example.com"
    doc.add_paragraph("Taylor Sample")
    doc.add_paragraph("My Journey", style="Heading 1")
    doc.add_paragraph("Responsible for clients", style="List Bullet")
    doc.add_paragraph("Helped with sales and various tasks", style="List Bullet")
    doc.add_paragraph("TODO: add a bullet", style="List Bullet")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Skills"
    doc.save(str(path))
    return path


def test_grades():
    assert [grade(s) for s in (95, 85, 75, 65, 59)] == ["A", "B", "C", "D", "F"]


def test_good_documents_score_well(good_docs, posting_text):
    report = score_documents(good_docs["resume"], good_docs["cover"], posting_text,
                             company="Acme",
                             expected_names=output_filenames("Taylor Sample", "Acme"))
    cats = {c.key: c.score for c in report.categories}
    assert set(cats) == {"keywords", "structure", "bullets", "length", "rules", "cover"}
    assert cats["structure"] == 100 and cats["rules"] == 100 and cats["bullets"] == 100
    assert report.hard_violations == 0
    assert 75 <= report.overall < 95  # strict: missing keywords keep it below an A
    assert "Gainsight" in {m.keyword.phrase for m in report.keyword_matches
                           if m.status == "missing"}


def test_keyword_category_only_with_job(good_docs):
    report = score_documents(good_docs["resume"])
    assert report.category("keywords") is None
    assert report.category("cover") is None


def test_bad_resume_red_flags_and_line_fixes(tmp_path):
    path = _bad_resume(tmp_path / "Taylor_Sample_Resume_Acme.docx")
    report = score_documents(path)
    flags = "\n".join(report.red_flags)
    for needle in ("table", "2-column", "Professional Experience", "Skills", "Education",
                   "placeholder"):
        assert needle in flags
    assert report.category("structure").score <= 20
    assert report.category("bullets").score <= 20
    fixes = {f.excerpt: f for f in report.findings}
    assert "Weak phrase 'responsible for'" in fixes["Responsible for clients"].problem
    assert "My Journey" in fixes
    assert any("header/footer" in n for n in report.category("structure").notes)


def test_banned_opening_in_cover_is_a_hard_violation(good_docs, tmp_path):
    cover = _letter_docx(tmp_path / "Taylor_Sample_CoverLetter_Acme.docx",
                         "I am writing to apply for the Customer Success Manager role.")
    report = score_documents(good_docs["resume"], cover)
    assert report.hard_violations == 1
    assert report.overall <= 59
    assert any("banned_opening" in f for f in report.red_flags)


def test_letter_header_block_is_not_the_opening(tmp_path):
    cover = _letter_docx(tmp_path / "x.docx", "Acme's growth in the DACH region stood out.")
    text = letter_text(read_docx(cover))
    assert text.startswith("Dear Hiring Team at Acme,")


@pytest.mark.parametrize("phrase", [
    "EU work authorised, residency in process - no sponsorship required",
    "I hold a Czech work permit.",
    "I moved to Prague with my partner.",
])
def test_work_authorisation_violations_in_cover(good_docs, tmp_path, phrase):
    cover = _letter_docx(tmp_path / "Taylor_Sample_CoverLetter_Acme.docx",
                         f"Acme's growth stood out. {phrase}")
    report = score_documents(good_docs["resume"], cover)
    assert report.hard_violations >= 1 and report.overall <= 59


def test_exact_work_authorisation_is_fine(good_docs, tmp_path):
    cover = _letter_docx(tmp_path / "Taylor_Sample_CoverLetter_Acme.docx",
                         f"Acme's growth stood out. {WORK_AUTH_STATEMENT}.")
    assert score_documents(good_docs["resume"], cover).hard_violations == 0


def test_wrong_file_names_are_hard_violations(good_docs, tmp_path):
    renamed = tmp_path / "resume final.docx"
    renamed.write_bytes(good_docs["resume"].read_bytes())
    report = score_documents(renamed)
    assert report.hard_violations == 1 and report.overall <= 59
    report = score_documents(good_docs["resume"],
                             expected_names={"resume": "Taylor_Sample_Resume_Beta.docx"})
    assert report.hard_violations == 1


def test_markdown_report_sections(good_docs, posting_text):
    report = score_documents(good_docs["resume"], good_docs["cover"], posting_text)
    report.extra_sections.append(("Claims check", "All clear."))
    md = report.to_markdown("ATS report: Acme")
    for heading in ("# ATS report: Acme", "**Overall:", "| Category | Score | Grade |",
                    "## Red flags", "## Line-level fixes", "## Keyword match",
                    "| Keyword | Type | Weight | Resume | Cover letter |", "## Category notes",
                    "## Claims check"):
        assert heading in md


def test_single_evidence_point_in_letter_is_flagged(good_docs, tmp_path):
    cover = _letter_docx(tmp_path / "Taylor_Sample_CoverLetter_Acme.docx",
                         "Acme's growth in the DACH region stood out.")
    report = score_documents(good_docs["resume"], cover)
    assert any("only one quantified evidence point" in n
               for n in report.category("cover").notes)
