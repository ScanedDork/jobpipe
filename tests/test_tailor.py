import json

import pytest

from jobpipe.llm import DryRunClient, LLMError, LLMRequest
from jobpipe.tailor import (
    build_fallback_letter,
    letter_body_words,
    supported_keywords,
    tailor_resume,
    write_cover_letter,
)
from jobpipe.validators import (
    WORK_AUTH_STATEMENT,
    check_cover_letter_opening,
    check_work_authorisation,
    letter_claims_check,
)

ROLE = "Customer Success Manager"


def _tailor(profile, job, llm=None):
    return tailor_resume(profile, job, llm or DryRunClient(), company="Acme", role=ROLE)


class _Failing:
    name = "broken"

    def complete(self, request: LLMRequest) -> str:
        raise LLMError("boom")


def _good_letter(profile):
    body = (
        "Acme's push into the DACH mid-market is the kind of growth I enjoy supporting. "
        "At Northwind Traders s.r.o., I grew net revenue retention from 97% to 112% in two "
        "years by running quarterly business reviews and structured upselling. "
    ) * 7
    return (f"Dear Hiring Team at Acme,\n\n{body}\n\n{WORK_AUTH_STATEMENT}.\n\n"
            "Kind regards,\nTaylor Sample")


# --- resume -------------------------------------------------------------------------------

def test_dry_run_tailoring_uses_profile_wording(profile, job):
    t = _tailor(profile, job)
    original = {b for exp in profile.experience for b in exp.real_bullets}
    assert {b for role in t.roles for b in role.bullets} == original
    assert t.summary == profile.summary
    assert t.rejected == []


def test_skills_are_reordered_but_never_added(profile, job):
    t = _tailor(profile, job)
    assert sorted(t.skills) == sorted(profile.skills)
    assert t.skills[0] in {"Account Management", "Customer Success"}  # required keywords
    assert t.skills.index("Salesforce") < t.skills.index("Jira")  # weight 2.0 vs 1.0


def test_supported_keywords_come_from_profile_only(profile, job):
    supported = {k.canonical for k in supported_keywords(job, profile)}
    assert "Salesforce" in supported
    assert "Gainsight" not in supported  # in the posting, not in the profile
    assert "mid-level" not in supported  # seniority is never a claim


def test_rephrasing_with_invented_facts_is_reverted(profile, job):
    originals = profile.experience[0].real_bullets
    rephrased = list(originals)
    rephrased[0] = "Managed 80 enterprise accounts in Gainsight worth €2.1M for Siemens."
    rephrased[1] = originals[1].replace("Grew", "Increased")  # faithful
    llm = DryRunClient(responses=[json.dumps(rephrased)])
    t = _tailor(profile, job, llm)
    bullets = t.roles[0].bullets
    assert originals[0] in bullets and rephrased[0] not in bullets
    assert rephrased[1] in bullets
    assert {"unsupported_number", "unsupported_term", "unsupported_name"} <= {
        i.code for i in t.rejected}


def test_rephrasing_with_visa_wording_is_reverted(profile, job):
    originals = profile.experience[0].real_bullets
    rephrased = [originals[0] + " Hold a work permit.", *originals[1:]]
    t = _tailor(profile, job, DryRunClient(responses=[json.dumps(rephrased)]))
    assert t.roles[0].bullets.count(originals[0]) == 1
    assert "work_auth_phrasing" in {i.code for i in t.rejected}


@pytest.mark.parametrize("bad", ["not json at all", json.dumps(["only one bullet"])])
def test_unusable_bullet_responses_keep_profile_wording(profile, job, bad):
    t = _tailor(profile, job, DryRunClient(responses=[bad]))
    assert set(t.roles[0].bullets) == set(profile.experience[0].real_bullets)
    assert any("unusable" in n for n in t.notes)


def test_invented_summary_is_reverted(profile, job):
    llm = DryRunClient(responses=["[]", "[]", "Account manager with 15 years in Gainsight."])
    t = _tailor(profile, job, llm)
    assert t.summary == profile.summary


def test_provider_errors_fall_back_to_facts(profile, job):
    t = _tailor(profile, job, _Failing())
    assert t.summary == profile.summary
    assert any("provider error" in n for n in t.notes)


def test_template_profile_omits_placeholders(template_profile, job):
    t = _tailor(template_profile, job)
    assert "TODO" not in t.all_text()
    assert any("placeholder" in n for n in t.notes)
    assert t.summary.startswith("Founder, DORK Company (2021 – 2025)")


# --- cover letter: rule 1 retry and fallback ----------------------------------------------

def test_banned_opening_triggers_retry_then_accepts(profile, job):
    t = _tailor(profile, job)
    good = _good_letter(profile)
    bad = good.replace("Acme's push into", "I am writing to apply for the role. Acme's push into")
    llm = DryRunClient(responses=[bad, good])
    letter = write_cover_letter(t, job, llm, company="Acme", role=ROLE)
    assert letter.text == good
    assert letter.attempts == 2 and not letter.used_fallback
    assert letter.rejected[0][1][0].code == "banned_opening"
    assert "PREVIOUS DRAFT WAS REJECTED" in llm.calls[-1].user


def test_persistent_rule_breaking_falls_back(profile, job):
    t = _tailor(profile, job)
    bad = "Dear Hiring Team,\n\nI am writing to express my interest in this role.\n\nBest,\nT"
    llm = DryRunClient(responses=[bad, bad, bad])
    letter = write_cover_letter(t, job, llm, company="Acme", role=ROLE)
    assert letter.used_fallback and letter.attempts == 3
    assert len(letter.rejected) == 3
    assert check_cover_letter_opening(letter.text) == []


def test_non_canonical_work_authorisation_is_rejected(profile, job):
    t = _tailor(profile, job)
    bad = _good_letter(profile).replace(WORK_AUTH_STATEMENT,
                                        "I hold a Czech work permit and moved here with my wife")
    letter = write_cover_letter(t, job, DryRunClient(responses=[bad] * 3), company="Acme",
                                role=ROLE)
    assert letter.used_fallback
    assert {"work_auth_phrasing", "relocation_reason"} <= {
        i.code for _, issues in letter.rejected for i in issues}
    assert check_work_authorisation(letter.text) == []


def test_invented_claims_in_letter_are_rejected(profile, job):
    t = _tailor(profile, job)
    bad = _good_letter(profile).replace("I grew", "I used Gainsight to grow")
    letter = write_cover_letter(t, job, DryRunClient(responses=[bad] * 3), company="Acme",
                                role=ROLE)
    assert letter.used_fallback
    assert "unsupported_term" in {i.code for _, issues in letter.rejected for i in issues}


def test_provider_error_uses_fallback_letter(profile, job):
    t = _tailor(profile, job)
    letter = write_cover_letter(t, job, _Failing(), company="Acme", role=ROLE)
    assert letter.used_fallback
    assert any("provider error" in n for n in letter.notes)


# --- the deterministic fallback letter ---------------------------------------------------

@pytest.mark.parametrize("company", ["Acme", "Contoso", "Fabrikam Ltd"])
def test_fallback_letter_obeys_every_rule(profile, job, company):
    t = _tailor(profile, job)
    text = build_fallback_letter(t, company=company, role=ROLE, job_text=job.text)
    assert check_cover_letter_opening(text) == []
    assert check_work_authorisation(text) == []
    assert letter_claims_check(text, profile.fact_texts(), allow=[company, ROLE],
                               known_names=job.text) == []
    assert WORK_AUTH_STATEMENT in text
    assert company in text


def test_dry_run_letter_for_example_profile_is_250_to_350_words(profile, job):
    letter = write_cover_letter(_tailor(profile, job), job, DryRunClient(), company="Acme",
                                role=ROLE)
    assert 250 <= letter_body_words(letter.text) <= 350
    assert letter.warnings == []
    assert letter.text.startswith("Dear Hiring Team at Acme,")
    assert letter.text.rstrip().endswith("Kind regards,\nTaylor Sample")


def test_sparse_profile_letter_is_short_but_valid(template_profile, job):
    letter = write_cover_letter(_tailor(template_profile, job), job, DryRunClient(),
                                company="Acme", role=ROLE)
    assert "length" in {w.code for w in letter.warnings}
    assert check_cover_letter_opening(letter.text) == []
    assert "TODO" not in letter.text


def test_fallback_letter_uses_two_to_three_evidence_points(profile, job):
    letter = build_fallback_letter(_tailor(profile, job), company="Acme", role=ROLE,
                                   job_text=job.text)
    clauses = [b[0].lower() + b[1:].rstrip(".") for exp in profile.experience
               for b in exp.real_bullets]
    assert 2 <= sum(clause in letter for clause in clauses) <= 3


def test_hook_reuses_the_posting_in_second_person(profile, job):
    letter = build_fallback_letter(_tailor(profile, job), company="Acme", role=ROLE,
                                   job_text=job.text)
    assert "You are growing your Prague hub and need a Customer Success Manager" in letter


def test_need_sentence_skips_benefits():
    from jobpipe.tailor import _need_sentence

    posting = "We offer a great salary and growing benefits for everyone here.\n"
    assert _need_sentence(posting, ROLE) == ""
    posting += "We're hiring a Customer Success Manager to look after our clients in Brno."
    assert _need_sentence(posting, ROLE) == (
        "You're hiring a Customer Success Manager to look after your clients in Brno")
