import pytest

from jobpipe.validators import (
    WORK_AUTH_STATEMENT,
    check_cover_letter_opening,
    check_output_filename,
    check_work_authorisation,
    claims_check,
    letter_claims_check,
    output_filenames,
    sanitize_filename_part,
    validate_cover_letter,
)

# --- Hard rule 1: banned cover letter openings --------------------------------------------

BANNED = [
    "I am writing to apply for the Customer Success Manager role at Acme.",
    "I'm writing to apply for the position of Account Manager.",
    "I’m writing to express my interest in the role.",
    "I am writing to express my interest in the Product Manager vacancy.",
    "I am writing in response to your advertisement.",
    "I write to apply for the role of Account Manager.",
    "I would like to apply for the Account Manager position.",
    "I'd like to express my interest in joining Acme.",
    "I wish to apply for the post of Sales Operations Analyst.",
    "I am applying for the Customer Success Manager role.",
    "I am excited to apply for the Customer Success Manager role.",
    "I am very interested in the Account Manager position at Acme.",
    "Please accept this letter as my application for the role.",
    "Please find enclosed my CV for the Account Manager role.",
    "With great interest I read your job posting.",
    "This letter is to express my interest in the role.",
    "In response to your job posting for an Account Manager, I enclose my CV.",
]


@pytest.mark.parametrize("opening", BANNED)
def test_banned_openings_are_rejected(opening):
    letter = f"Dear Hiring Team at Acme,\n\n{opening} More text follows.\n\nKind regards,\nTaylor"
    issues = check_cover_letter_opening(letter)
    assert [i.code for i in issues] == ["banned_opening"]


def test_banned_opening_on_salutation_line_is_rejected():
    assert check_cover_letter_opening("Dear team, I am writing to apply for the role.")


@pytest.mark.parametrize("opening", [
    "Acme's push into the DACH mid-market is exactly the kind of growth I have supported.",
    "Your Customer Success Manager opening stands out because it centres on renewals.",
    "When I took over a portfolio of 45 accounts, net revenue retention was 97%.",
])
def test_specific_hooks_are_allowed(opening):
    letter = f"Dear Hiring Team at Acme,\n\n{opening}\n\nKind regards,\nTaylor"
    assert check_cover_letter_opening(letter) == []


def test_banned_phrase_later_in_letter_is_not_an_opening():
    letter = "Dear team,\n\nAcme's growth caught my eye. I am writing to share three results."
    assert check_cover_letter_opening(letter) == []


# --- Hard rule 2: work authorisation ------------------------------------------------------

def test_exact_work_authorisation_statement_passes():
    text = f"Based in Prague. {WORK_AUTH_STATEMENT}."
    assert check_work_authorisation(text) == []


@pytest.mark.parametrize("text", [
    "EU work authorised, residency in process - no sponsorship required",  # hyphen not em dash
    "EU work authorized, residency in process — no sponsorship required",  # US spelling
    "I do not require visa sponsorship.",
    "I hold a valid work permit for the Czech Republic.",
    "Eligible to work in the EU.",
    "My Czech residence permit is being processed.",
    "EU Blue Card holder.",
    "EU citizen.",
    "No sponsorship needed.",
])
def test_other_work_authorisation_phrasing_is_flagged(text):
    codes = {i.code for i in check_work_authorisation(text)}
    assert "work_auth_phrasing" in codes


@pytest.mark.parametrize("text", [
    "I relocated to Prague in 2024.",
    "I moved to Prague to join my wife.",
    "I am moving to Czechia with my partner.",
    "I left India for family reasons.",
    "Open to relocation.",
])
def test_relocation_reasons_and_relationships_are_flagged(text):
    codes = {i.code for i in check_work_authorisation(text)}
    assert "relocation_reason" in codes


def test_statement_plus_extra_explanation_is_flagged():
    text = f"{WORK_AUTH_STATEMENT}, as my spouse is an EU citizen."
    codes = {i.code for i in check_work_authorisation(text)}
    assert codes == {"work_auth_phrasing", "relocation_reason"}


@pytest.mark.parametrize("text", [
    "Secured an executive sponsor at each of 12 enterprise accounts.",
    "Managed event sponsorship deals with channel partners.",
    "Built partnerships with logistics providers across Europe.",
    "Based in Prague, Czech Republic.",
])
def test_business_language_is_not_flagged(text):
    assert check_work_authorisation(text) == []


# --- Hard rule 3: file names --------------------------------------------------------------

def test_output_filenames_exact_pattern():
    assert output_filenames("Ranjeet Skanda", "Acme") == {
        "resume": "Ranjeet_Skanda_Resume_Acme.docx",
        "cover": "Ranjeet_Skanda_CoverLetter_Acme.docx",
        "report": "Acme_ats_report.md",
    }


@pytest.mark.parametrize("raw, safe", [
    ("Acme", "Acme"),
    ("Acme Corp, Inc.", "Acme_Corp_Inc"),
    ("Škoda Auto a.s.", "Skoda_Auto_a_s"),
    ("AT&T", "AT_and_T"),
    ("../../etc/passwd", "etc_passwd"),
    ("Acme/Payments:EU*?", "Acme_Payments_EU"),
    ("CON", "CON_"),
    ("  Müller   GmbH  ", "Muller_GmbH"),
])
def test_company_names_are_sanitised(raw, safe):
    assert sanitize_filename_part(raw) == safe


def test_sanitise_rejects_empty_and_truncates_long_names():
    with pytest.raises(ValueError):
        sanitize_filename_part("!!!")
    assert len(sanitize_filename_part("A" * 200)) == 60


@pytest.mark.parametrize("name, kind, ok", [
    ("Ranjeet_Skanda_Resume_Acme.docx", "resume", True),
    ("Ranjeet_Skanda_CoverLetter_Acme_Corp.docx", "cover", True),
    ("Acme_ats_report.md", "report", True),
    ("resume final (2).docx", "resume", False),
    ("Ranjeet Skanda Resume Acme.docx", "resume", False),
    ("Ranjeet_Skanda_Cover_Letter_Acme.docx", "cover", False),
    ("Ranjeet_Skanda_Resume_Acme.pdf", "resume", False),
])
def test_filename_pattern_check(name, kind, ok):
    assert (check_output_filename(name, kind) == []) is ok


def test_filename_check_against_expected_name():
    assert check_output_filename("out/Ranjeet_Skanda_Resume_Acme.docx", "resume",
                                 expected="Ranjeet_Skanda_Resume_Acme.docx") == []
    assert check_output_filename("out/Ranjeet_Skanda_Resume_Beta.docx", "resume",
                                 expected="Ranjeet_Skanda_Resume_Acme.docx")


# --- Hard rule 4: claims check -------------------------------------------------------------

SOURCE = ["Managed a portfolio of 45 mid-market accounts in the DACH region worth €2.1M in "
          "annual recurring revenue.", "Account Manager", "Northwind Traders s.r.o."]


def test_faithful_rephrasing_passes():
    text = "Owned 45 mid-market DACH accounts worth €2.1M in annual recurring revenue."
    assert claims_check(text, SOURCE, check_novelty=True) == []


@pytest.mark.parametrize("text, code", [
    ("Managed 60 mid-market accounts in the DACH region worth €2.1M.", "unsupported_number"),
    ("Managed 45 DACH accounts worth €2.1M, doubling revenue.", "unsupported_number"),
    ("Managed 45 DACH accounts worth €2.1M in Gainsight.", "unsupported_term"),
    ("Managed 45 DACH accounts worth €2.1M for Siemens.", "unsupported_name"),
    ("Managed 45 DACH accounts worth €2.1M, launching a referral programme with influencers "
     "and podcasts.", "unsupported_content"),
])
def test_unsupported_claims_are_flagged(text, code):
    assert code in {i.code for i in claims_check(text, SOURCE, check_novelty=True)}


def test_letter_claims_only_checks_first_person_sentences(profile):
    facts = profile.fact_texts()
    letter = ("Dear Hiring Team at Acme,\n\nAcme tracks churn risk in Gainsight across 300 "
              "accounts. I grew net revenue retention from 97% to 112%.\n\nKind regards,\nTaylor")
    assert letter_claims_check(letter, facts, allow=["Acme"]) == []
    bad = letter.replace("I grew", "I used Gainsight and grew")
    assert {i.code for i in letter_claims_check(bad, facts, allow=["Acme"])} == {
        "unsupported_term"}


# --- Cover letter validation bundle --------------------------------------------------------

def test_validate_cover_letter_reports_length_and_hook_as_warnings():
    letter = "Dear Hiring Team,\n\nShort letter about renewals.\n\nKind regards,\nTaylor"
    issues = {i.code: i.severity for i in validate_cover_letter(letter, company="Acme")}
    assert issues == {"length": "warning", "no_company_hook": "warning"}


def test_validate_cover_letter_flags_placeholders():
    letter = "Dear Hiring Team,\n\nI admire [Company] and its TODO mission.\n\nKind regards,\nX"
    assert "placeholder" in {i.code for i in validate_cover_letter(letter)}
