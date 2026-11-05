from jobpipe.keywords import (
    extract_keywords,
    find_terms,
    guess_title,
    keyword_score,
    match_keywords,
    parse_posting,
)


def _by_canonical(keywords):
    return {k.canonical: k for k in keywords}


def test_parse_posting_basics(job):
    assert job.title == "Customer Success Manager"
    assert job.url == "https://careers.acme.example/jobs/csm-prague"
    assert job.years_required == 3


def test_extracts_tools_skills_titles_seniority_and_languages(job):
    kws = _by_canonical(job.keywords)
    for term in ("Salesforce", "HubSpot", "Gainsight", "Jira", "Excel"):
        assert kws[term].category == "tool"
    for term in ("churn reduction", "net revenue retention", "stakeholder management",
                 "quarterly business reviews", "cross-selling", "SaaS", "B2B"):
        assert kws[term].category == "skill"
    assert kws["Customer Success Manager"].category == "title"
    assert kws["mid-level"].category == "seniority"
    assert kws["German"].category == "language"
    assert kws["DACH"].category == "other"


def test_section_weights(job):
    kws = _by_canonical(job.keywords)
    assert kws["negotiation"].weight == 2.0  # "What you bring" = required
    assert kws["Jira"].weight == 1.0  # responsibilities = general
    assert kws["fintech"].weight == 0.5  # "Nice to have"
    assert kws["Customer Success Manager"].weight == 2.0  # job title


def test_longer_terms_claim_their_span():
    kws = _by_canonical(extract_keywords("- Grow net revenue retention"))
    assert "net revenue retention" in kws
    assert "customer retention" not in kws


def test_counts_each_mention_once():
    kws = _by_canonical(extract_keywords("- Run QBRs and quarterly business reviews"))
    assert kws["quarterly business reviews"].count == 2


def test_exact_vs_synonym_vs_missing():
    kws = extract_keywords("Requirements:\n- customer success experience\n- Salesforce\n- KPIs")
    matches = {m.keyword.canonical: m.status for m in match_keywords(
        kws, "Led client success for 40 accounts in Salesforce.")}
    assert matches == {"customer success": "synonym", "Salesforce": "exact", "KPI": "missing"}


def test_singular_and_plural_both_exact():
    kws = extract_keywords("- Track KPI dashboards")
    statuses = {m.keyword.canonical: m.status for m in match_keywords(kws, "Built a dashboard")}
    assert statuses["dashboards"] == "exact"


def test_case_sensitive_tools_avoid_false_positives():
    assert "Excel" not in find_terms("I excel at negotiation")
    assert "Excel" in find_terms("Advanced Excel modelling")
    assert "Notion" not in find_terms("the notion of ownership")


def test_keyword_score_weights_and_ignores_seniority():
    kws = extract_keywords("Senior Account Manager\nRequirements:\n- Salesforce\n- HubSpot")
    assert keyword_score(match_keywords(kws, "Account Manager using Salesforce and HubSpot")) == 100
    assert keyword_score(match_keywords(kws, "Salesforce")) < 50


def test_guess_title_variants():
    assert guess_title("Job title: Product Owner\nmore") == "Product Owner"
    assert guess_title("\nKey Account Manager – Contoso, Berlin") == "Key Account Manager"


def test_company_name_is_not_a_keyword():
    job = parse_posting("Account Manager at ACME\nRequirements:\n- ACME products", company="ACME")
    assert "ACME" not in {k.canonical for k in job.keywords}
