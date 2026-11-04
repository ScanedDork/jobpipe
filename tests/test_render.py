from datetime import date

import pytest
from docx import Document
from docx.shared import Cm

from jobpipe.llm import DryRunClient
from jobpipe.render import (
    TemplateError,
    available_templates,
    load_template,
    output_paths,
    render_cover_letter,
    render_resume,
)
from jobpipe.tailor import tailor_resume, write_cover_letter
from jobpipe.validators import STANDARD_FONTS, WORK_AUTH_STATEMENT, check_cover_letter_opening

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ROLE = "Customer Success Manager"


def _render(profile, job, out_dir, template="classic"):
    llm = DryRunClient()
    tailored = tailor_resume(profile, job, llm, company="Acme", role=ROLE)
    letter = write_cover_letter(tailored, job, llm, company="Acme", role=ROLE)
    paths = output_paths(out_dir, profile.preferred_name, "Acme")
    tpl = load_template(template)
    render_resume(tailored, paths["resume"], tpl)
    render_cover_letter(letter.text, profile, "Acme", paths["cover"], tpl,
                        today=date(2026, 11, 4))
    return paths


def _headings(doc):
    return [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]


def _assert_ats_safe(doc):
    body = doc.element.body
    assert len(doc.tables) == 0
    assert not list(body.iter(f"{W}tbl"))
    assert not list(body.iter(f"{W}txbxContent"))
    assert not list(body.iter(f"{W}drawing")) and not list(body.iter(f"{W}pict"))
    assert all(int(c.get(f"{W}num", "1")) == 1 for c in body.iter(f"{W}cols"))
    for section in doc.sections:
        assert section.header.is_linked_to_previous and section.footer.is_linked_to_previous
        assert round(section.page_width.cm, 1) == 21.0
        assert round(section.page_height.cm, 1) == 29.7
    fonts = {r.font.name for p in doc.paragraphs for r in p.runs if r.font.name}
    fonts |= {doc.styles[s].font.name for s in ("Normal", "Heading 1", "List Bullet")}
    assert {f.lower() for f in fonts} <= STANDARD_FONTS


def test_resume_file_name_headings_and_layout(profile, job, tmp_path):
    paths = _render(profile, job, tmp_path)
    assert paths["resume"].name == "Taylor_Sample_Resume_Acme.docx"
    assert paths["resume"].parent == tmp_path and paths["resume"].is_file()
    doc = Document(str(paths["resume"]))
    assert _headings(doc) == ["Professional Summary", "Skills", "Professional Experience",
                              "Education", "Certifications", "Languages"]
    _assert_ats_safe(doc)
    texts = [p.text for p in doc.paragraphs]
    assert texts[0] == "Taylor Sample"
    assert any("taylor.sample@example.com" in t for t in texts[:4])
    assert WORK_AUTH_STATEMENT in texts
    bullets = [p.text for p in doc.paragraphs if p.style.name == "List Bullet"]
    for exp in profile.experience:
        assert set(exp.real_bullets) <= set(bullets)
    assert doc.core_properties.author == "Taylor Sample"


def test_cover_letter_file_name_and_content(profile, job, tmp_path):
    paths = _render(profile, job, tmp_path)
    assert paths["cover"].name == "Taylor_Sample_CoverLetter_Acme.docx"
    doc = Document(str(paths["cover"]))
    _assert_ats_safe(doc)
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert "4 November 2026" in texts
    salutation = texts.index("Dear Hiring Team at Acme,")
    assert check_cover_letter_opening("\n\n".join(texts[salutation:])) == []
    assert texts[-2:] == ["Kind regards,", "Taylor Sample"]


def test_compact_template_changes_order_and_headings(profile, job, tmp_path):
    paths = _render(profile, job, tmp_path, template="compact")
    doc = Document(str(paths["resume"]))
    assert _headings(doc) == ["Profile", "Work Experience", "Key Skills", "Education",
                              "Certifications", "Languages"]
    assert round(doc.sections[0].left_margin.cm, 2) == round(Cm(1.5).cm, 2)
    assert doc.styles["Normal"].font.name == "Arial"


def test_template_profile_renders_without_placeholders(template_profile, job, tmp_path):
    paths = _render(template_profile, job, tmp_path)
    assert paths["resume"].name == "Ranjeet_Skanda_Resume_Acme.docx"
    assert paths["cover"].name == "Ranjeet_Skanda_CoverLetter_Acme.docx"
    for kind in ("resume", "cover"):
        doc = Document(str(paths[kind]))
        assert "TODO" not in "\n".join(p.text for p in doc.paragraphs)
        _assert_ats_safe(doc)
    headings = _headings(Document(str(paths["resume"])))
    assert "Skills" not in headings  # all skills are still TODO, so the section is omitted
    assert "Professional Experience" in headings


def test_unsafe_company_names_stay_inside_output_dir(tmp_path):
    paths = output_paths(tmp_path, "Ranjeet Skanda", "../../Evil/Corp")
    assert paths["resume"].name == "Ranjeet_Skanda_Resume_Evil_Corp.docx"
    assert all(p.parent == tmp_path for p in paths.values())


def test_bundled_templates_are_valid():
    assert {"classic", "compact"} <= set(available_templates())
    for name in available_templates():
        load_template(name)


GOOD = """
name: custom
font: Calibri
body_size: 11
name_size: 16
heading_size: 12
margins_cm: 2
space_after_pt: 2
headings: {summary: Summary, skills: Skills, experience: Experience, education: Education}
section_order: [summary, skills, experience, education]
"""


def test_custom_template_from_path(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(GOOD)
    assert load_template(path).section_order == ("summary", "skills", "experience", "education")


@pytest.mark.parametrize("change, message", [
    (("font: Calibri", "font: Comic Sans MS"), "not a standard"),
    (("experience: Experience", "experience: My Journey"), "not a standard ATS heading"),
    (("[summary, skills, experience, education]", "[summary, skills, experience]"), "include"),
    (("margins_cm: 2", "margins_cm: 0.5"), "between"),
    (("name: custom", "name: custom\ncolumns: 2"), "unknown keys"),
])
def test_invalid_templates_are_rejected(tmp_path, change, message):
    path = tmp_path / "bad.yaml"
    path.write_text(GOOD.replace(*change))
    with pytest.raises(TemplateError, match=message):
        load_template(path)


def test_missing_template_lists_available():
    with pytest.raises(TemplateError, match="classic"):
        load_template("does-not-exist")


def test_long_profiles_are_trimmed_to_two_pages(job, tmp_path):
    import yaml

    from jobpipe.profile import parse_profile
    from jobpipe.scorer import estimate_pages

    from .conftest import EXAMPLE_PROFILE

    data = yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    data["experience"] = [
        {"title": "Account Manager", "company": f"Example Company {r}", "start": "2010",
         "end": "2020", "bullets": [
             f"Delivered programme {r}{b} for logistics clients across Central Europe, "
             "coordinating ten stakeholders and reporting weekly progress to the regional "
             "leadership team." for b in range(6)]}
        for r in range(6)]
    tailored = tailor_resume(parse_profile(data), job, DryRunClient(), company="Acme",
                             role=ROLE)
    path = render_resume(tailored, tmp_path / "Taylor_Sample_Resume_Acme.docx",
                         load_template("classic"))
    doc = Document(str(path))
    assert estimate_pages((p.text, p.style.name) for p in doc.paragraphs) <= 2.0
    assert len(tailored.roles[0].bullets) == 6  # the most recent role is trimmed last
    assert all(len(role.bullets) >= 2 for role in tailored.roles)
    assert any(note.startswith("Left out") for note in tailored.notes)
