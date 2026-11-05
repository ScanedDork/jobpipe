import re

import pytest
import yaml

from jobpipe.profile import ProfileError, is_placeholder, load_profile, parse_profile
from jobpipe.validators import WORK_AUTH_STATEMENT

from .conftest import EXAMPLE_PROFILE, TEMPLATE_PROFILE


def _example_data():
    return yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8"))


def test_example_profile_loads(profile):
    assert profile.preferred_name == "Taylor Sample"
    assert profile.work_authorisation == WORK_AUTH_STATEMENT
    assert profile.placeholders() == []
    assert len(profile.experience) == 2
    assert profile.experience[0].dates == "2021 – Present"


def test_template_contains_only_the_given_facts(template_profile):
    p = template_profile
    assert p.full_name == "Ramar Ranjeet Skanda"
    assert p.preferred_name == "Ranjeet Skanda"
    assert p.location == "Prague, Czech Republic"
    assert p.target_roles == ["Business Development", "Account Management", "Customer Success",
                              "Sales Operations", "Product Management"]
    assert p.experience[0].company == "DORK Company"
    assert (p.experience[0].start, p.experience[0].end) == ("2021", "2025")
    assert p.education[0].degree == "M.Tech in Biomedical Engineering"
    assert p.education[0].institution == "MNNIT Allahabad"
    assert p.certification_hours == "600+"
    assert [c.name for c in p.certifications] == [
        "Microsoft AI Product Management", "Salesforce Sales Operations",
        "European Business Law", "Agile/Jira", "IIM Bangalore programs"]
    # Contact details, skills, languages and achievement bullets are placeholders only.
    assert all(is_placeholder(x) for x in (p.email, p.phone, p.linkedin, p.headline, p.summary))
    assert p.skills and all(is_placeholder(s) for s in p.skills)
    assert p.experience[0].real_bullets == [
        "Founded and ran the agency, serving B2B clients in India, the US and the Middle East."]


def test_template_invents_no_numbers(template_profile):
    numbers = set()
    for fact in template_profile.fact_texts():
        numbers |= set(re.findall(r"(?<![A-Za-z])\d+(?![A-Za-z])", fact))  # not B2B
    assert numbers <= {"2021", "2025", "600"}


def test_rejects_paraphrased_work_authorisation():
    data = _example_data()
    data["work_authorisation"] = (
        "EU work authorized, residency in process - no sponsorship required")
    with pytest.raises(ProfileError, match="work_authorisation must be exactly"):
        parse_profile(data)


def test_empty_work_authorisation_is_allowed():
    data = _example_data()
    data["work_authorisation"] = ""
    assert parse_profile(data).work_authorisation == ""


@pytest.mark.parametrize("bullet", [
    "Relocated to Prague to join my wife.",
    "Hold a valid Czech work permit.",
    "Moved to Prague for family reasons.",
])
def test_rejects_relocation_or_visa_wording_anywhere(bullet):
    data = _example_data()
    data["experience"][0]["bullets"].append(bullet)
    with pytest.raises(ProfileError, match="work-authorisation rule"):
        parse_profile(data)


def test_rejects_unknown_keys_and_missing_name():
    data = _example_data()
    data["hobbies"] = ["chess"]
    with pytest.raises(ProfileError, match="Unknown profile keys"):
        parse_profile(data)
    data = _example_data()
    data["name"] = {"full": "TODO", "preferred": "TODO"}
    with pytest.raises(ProfileError, match="name.preferred"):
        parse_profile(data)


def test_missing_file_has_helpful_message(tmp_path):
    with pytest.raises(ProfileError, match="profile.template.yaml"):
        load_profile(tmp_path / "nope.yaml")


def test_target_roles_are_not_facts(template_profile):
    facts = "\n".join(template_profile.fact_texts())
    assert "Sales Operations" in facts  # from the certification name
    assert "Business Development" not in facts
    assert TEMPLATE_PROFILE.exists()
