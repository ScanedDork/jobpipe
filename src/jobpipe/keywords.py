"""Keyword extraction from job postings and keyword matching against documents.

Vocabulary is curated for the target roles (business development, account management,
customer success, sales operations, product management) and the European market. Each
canonical term has a category and a list of common synonyms. Matching distinguishes an
exact phrase match (the posting's own wording) from a synonym match, because many ATS
parsers only credit the exact phrase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

# canonical term -> (category, synonyms)
TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    # --- tools -----------------------------------------------------------------------
    "Salesforce": ("tool", ("SFDC", "Salesforce CRM", "Sales Cloud")),
    "HubSpot": ("tool", ("HubSpot CRM",)),
    "Pipedrive": ("tool", ()),
    "Microsoft Dynamics 365": ("tool", ("Dynamics 365", "MS Dynamics", "Microsoft Dynamics")),
    "Zoho CRM": ("tool", ()),
    "Gainsight": ("tool", ()),
    "ChurnZero": ("tool", ()),
    "Totango": ("tool", ()),
    "Zendesk": ("tool", ()),
    "Intercom": ("tool", ()),
    "Freshdesk": ("tool", ()),
    "Jira": ("tool", ("Atlassian Jira",)),
    "Confluence": ("tool", ()),
    "Trello": ("tool", ()),
    "Asana": ("tool", ()),
    "Monday.com": ("tool", ()),
    "Notion": ("tool", ()),
    "Miro": ("tool", ()),
    "Figma": ("tool", ()),
    "Excel": ("tool", ("Microsoft Excel", "MS Excel")),
    "Google Sheets": ("tool", ()),
    "PowerPoint": ("tool", ("MS PowerPoint", "Microsoft PowerPoint")),
    "Power BI": ("tool", ("PowerBI",)),
    "Tableau": ("tool", ()),
    "Looker": ("tool", ()),
    "SQL": ("tool", ()),
    "Python": ("tool", ()),
    "Google Analytics": ("tool", ("GA4",)),
    "Mixpanel": ("tool", ()),
    "Amplitude": ("tool", ()),
    "LinkedIn Sales Navigator": ("tool", ("Sales Navigator",)),
    "Salesloft": ("tool", ()),
    "Outreach": ("tool", ("Outreach.io",)),
    "Gong": ("tool", ()),
    "DocuSign": ("tool", ()),
    "SAP": ("tool", ()),
    "Slack": ("tool", ()),
    # --- hard skills ------------------------------------------------------------------
    "account management": ("skill", ("client management", "account manager")),
    "key account management": ("skill", ("KAM", "strategic account management")),
    "business development": ("skill", ("biz dev", "BizDev")),
    "customer success": ("skill", ("client success",)),
    "sales operations": ("skill", ("sales ops", "SalesOps")),
    "revenue operations": ("skill", ("RevOps", "rev ops")),
    "product management": ("skill", ("product manager",)),
    "product roadmap": ("skill", ("roadmap", "roadmapping")),
    "product discovery": ("skill", ()),
    "product strategy": ("skill", ()),
    "backlog management": ("skill", ("product backlog", "backlog")),
    "user stories": ("skill", ()),
    "requirements gathering": ("skill", ("requirements analysis",)),
    "user research": ("skill", ("customer research", "customer interviews")),
    "go-to-market": ("skill", ("GTM", "go to market strategy")),
    "lead generation": ("skill", ("lead gen", "demand generation")),
    "prospecting": ("skill", ("outbound prospecting",)),
    "cold outreach": ("skill", ("cold calling", "cold emailing")),
    "pipeline management": ("skill", ("sales pipeline", "pipeline")),
    "sales forecasting": ("skill", ("forecasting", "forecast accuracy")),
    "CRM": ("skill", ("customer relationship management",)),
    "B2B": ("skill", ("business-to-business",)),
    "SaaS": ("skill", ("software as a service",)),
    "B2B sales": ("skill", ("enterprise sales",)),
    "solution selling": ("skill", ("consultative selling", "value selling")),
    "negotiation": ("skill", ("negotiating", "negotiate", "negotiated")),
    "contract negotiation": ("skill", ("contract negotiations",)),
    "stakeholder management": ("skill", ("stakeholder engagement", "stakeholder relations")),
    "customer onboarding": ("skill", ("onboarding", "client onboarding")),
    "churn reduction": ("skill", ("churn", "reducing churn", "churn prevention")),
    "customer retention": ("skill", ("retention", "client retention")),
    "net revenue retention": ("skill", ("NRR", "net dollar retention", "NDR")),
    "renewals": ("skill", ("renewal", "contract renewals")),
    "upselling": ("skill", ("upsell", "up-selling", "upsold", "expansion revenue")),
    "cross-selling": ("skill", ("cross-sell", "cross selling")),
    "account planning": ("skill", ("account plans", "account plan")),
    "quarterly business reviews": ("skill", ("QBR", "QBRs", "business reviews")),
    "NPS": ("skill", ("net promoter score",)),
    "customer health scores": ("skill", ("health scores", "health score", "health scoring")),
    "customer experience": ("skill", ("CX",)),
    "customer journey": ("skill", ("customer lifecycle",)),
    "KPI": ("skill", ("KPIs", "key performance indicators")),
    "OKR": ("skill", ("OKRs", "objectives and key results")),
    "dashboards": ("skill", ("dashboard", "reporting dashboards")),
    "data analysis": ("skill", ("data analytics", "analytics")),
    "market research": ("skill", ("market analysis",)),
    "competitive analysis": ("skill", ("competitor analysis",)),
    "A/B testing": ("skill", ("split testing", "experimentation")),
    "Agile": ("skill", ("agile methodology", "agile methodologies")),
    "Scrum": ("skill", ()),
    "Kanban": ("skill", ()),
    "project management": ("skill", ()),
    "process improvement": ("skill", ("process optimisation", "process optimization")),
    "partnerships": ("skill", ("partner management", "channel partnerships", "alliances")),
    "sales enablement": ("skill", ()),
    "pricing": ("skill", ("pricing strategy",)),
    "P&L": ("skill", ("profit and loss",)),
    "budgeting": ("skill", ("budget management",)),
    "RFP": ("skill", ("RFPs", "tenders", "request for proposal")),
    "GDPR": ("skill", ("data protection",)),
    "European business law": ("skill", ("EU business law",)),
    "fintech": ("skill", ("financial technology",)),
    "payments": ("skill", ("payment processing",)),
    "AI": ("skill", ("artificial intelligence",)),
    "machine learning": ("skill", ("ML",)),
    # --- job titles -------------------------------------------------------------------
    "Account Manager": ("title", ()),
    "Key Account Manager": ("title", ()),
    "Customer Success Manager": ("title", ("CSM",)),
    "Business Development Manager": ("title", ("BDM",)),
    "Business Development Representative": ("title", ("BDR",)),
    "Sales Development Representative": ("title", ("SDR",)),
    "Account Executive": ("title", ()),
    "Sales Operations Manager": ("title", ()),
    "Sales Operations Analyst": ("title", ()),
    "Revenue Operations Manager": ("title", ()),
    "Product Manager": ("title", ()),
    "Product Owner": ("title", ()),
    "Partnership Manager": ("title", ("Partnerships Manager",)),
    "Founder": ("title", ("Co-Founder", "Cofounder")),
    # --- seniority --------------------------------------------------------------------
    "junior": ("seniority", ("entry-level", "graduate")),
    "mid-level": ("seniority", ("mid level", "intermediate")),
    "senior": ("seniority", ()),
    "principal": ("seniority", ()),
    "head of": ("seniority", ()),
    "director": ("seniority", ()),
    # --- languages --------------------------------------------------------------------
    "English": ("language", ()),
    "Czech": ("language", ()),
    "German": ("language", ()),
    "French": ("language", ()),
    "Spanish": ("language", ()),
    "Italian": ("language", ()),
    "Polish": ("language", ()),
    "Slovak": ("language", ()),
    "Hungarian": ("language", ()),
    "Dutch": ("language", ()),
    "Portuguese": ("language", ()),
    "Russian": ("language", ()),
    "Ukrainian": ("language", ()),
    "Hindi": ("language", ()),
    "Arabic": ("language", ()),
    "Mandarin": ("language", ()),
}

# Forms that are ordinary English words in lower case ("excel at", "notion of", "slack time").
CASE_SENSITIVE = {
    "Excel", "Notion", "Outreach", "Gong", "Slack", "Looker", "Intercom", "Asana", "Miro",
    "AI", "ML", "KAM", "CSM", "SAP", "GTM", "NPS", "CX", "NRR", "NDR", "BDM", "BDR", "SDR",
    "Amplitude", "Python", "Confluence", "Tableau",
}

# Acronyms that are not skills, used to filter "other" keywords found in postings.
ACRONYM_STOPLIST = {
    "EU", "US", "USA", "UK", "CEO", "CTO", "CFO", "COO", "CMO", "HR", "CV", "ID", "IT", "FAQ",
    "PTO", "ASAP", "AND", "OR", "THE", "WE", "YOU", "OUR", "CZ", "DE", "EN", "GMBH", "LTD",
    "INC", "LLC", "SRO", "AS", "AG", "SE", "NA", "TBD", "ETC", "EUR", "CZK", "USD", "OK",
    "A", "I", "B", "C", "PS", "EMEA", "CEE", "APAC", "LATAM", "HQ", "VP",
    "A1", "A2", "B1", "B2", "C1", "C2",  # CEFR language levels
}

ACTION_VERBS: frozenset[str] = frozenset(
    """
    accelerate accelerated achieve achieved acquire acquired advise advised analyse
    analysed analyze analyzed architect architected automate automated build built close
    closed coach coached consolidate consolidated convert converted coordinate
    coordinated create created cut decrease decreased define defined deliver delivered
    design designed develop developed direct directed double doubled drive drove earn
    earned establish established exceed exceeded expand expanded found founded generate
    generated grow grew guide guided identify identified implement implemented improve
    improved increase increased initiate initiated introduce introduced launch launched
    lead led manage managed mentor mentored negotiate negotiated onboard onboarded open
    opened optimise optimised optimize optimized organise organised organize organized
    oversee oversaw own owned partner partnered pioneer pioneered plan planned present
    presented prioritise prioritised prioritize prioritized produce produced prospect
    prospected run ran rebuild rebuilt recruit recruited redesign redesigned reduce
    reduced renew renewed resolve resolved restructure restructured retain retained
    revamp revamped save saved scale scaled secure secured sell sold simplify simplified
    spearhead spearheaded standardise standardised standardize standardized streamline
    streamlined strengthen strengthened supervise supervised train trained transform
    transformed triple tripled upsell upsold win won write wrote complete completed
    """.split()
)

WEAK_PHRASES = (
    "responsible for", "duties included", "duties include", "worked on", "helped with",
    "helped to", "assisted with", "assisted in", "involved in", "tasked with", "in charge of",
    "participated in", "various", "etc.",
)

NUMBER_WORDS = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
    "twenty": "20", "thirty": "30", "fifty": "50", "hundred": "100", "thousand": "1000",
    "dozen": "12", "dozens": "12", "hundreds": "100", "thousands": "1000", "million": "1000000",
    "double": "2", "doubled": "2", "doubling": "2", "triple": "3", "tripled": "3",
    "tripling": "3", "twice": "2", "halved": "2", "halving": "2",
}

SECTION_WEIGHTS = {"required": 2.0, "general": 1.0, "preferred": 0.5}
_REQUIRED_HEAD = re.compile(
    r"require|qualification|must|what you (?:bring|need|have|offer)|who you are|you have|"
    r"your profile|we expect|skills|experience|about you|you bring|you will need",
    re.I,
)
_PREFERRED_HEAD = re.compile(
    r"nice[- ]to[- ]have|bonus|preferred|a plus|advantage|desirable|ideally|good to have", re.I
)
_YEARS_RE = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?years?\b", re.I)
_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class Keyword:
    canonical: str  # canonical vocabulary term (or the acronym itself for "other")
    phrase: str  # the wording used in the posting (the "exact phrase")
    category: str  # tool | skill | title | seniority | language | other
    weight: float  # 2.0 required, 1.0 general, 0.5 nice-to-have
    count: int


@dataclass(frozen=True)
class KeywordMatch:
    keyword: Keyword
    status: str  # exact | synonym | missing
    found: str = ""  # the text that matched in the document


@dataclass
class JobPosting:
    text: str
    title: str
    url: str
    keywords: list[Keyword]
    years_required: int | None = None


def forms(canonical: str) -> tuple[str, ...]:
    """All surface forms for a canonical term (canonical first)."""
    entry = TERMS.get(canonical)
    return (canonical, *entry[1]) if entry else (canonical,)


@lru_cache(maxsize=2048)
def phrase_pattern(form: str) -> re.Pattern[str]:
    """Regex for a phrase: whole-word, flexible spaces/hyphens, optional plural."""
    parts = [p for p in re.split(r"[\s\-]+", form.strip()) if p]
    last = parts[-1]
    if len(last) > 3 and last.endswith("s") and not last.endswith("ss"):
        parts[-1] = last[:-1]  # "renewals" also matches "renewal"
    body = r"[\s\-]+".join(re.escape(p) for p in parts)
    flags = 0 if form in CASE_SENSITIVE else re.IGNORECASE
    return re.compile(rf"(?<![\w]){body}(?:s|es)?(?![\w])", flags)


def find_phrase(form: str, text: str) -> str:
    m = phrase_pattern(form).search(text)
    return m.group(0) if m else ""


def find_terms(text: str, categories: tuple[str, ...] = ("tool", "skill", "title", "language")
               ) -> dict[str, str]:
    """Canonical vocabulary terms present in ``text`` (exact or synonym) -> matched text."""
    found: dict[str, str] = {}
    for canonical, (category, _) in TERMS.items():
        if category not in categories:
            continue
        for form in forms(canonical):
            hit = find_phrase(form, text)
            if hit:
                found[canonical] = hit
                break
    return found


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split a posting into (section_kind, line) pairs. Heading lines get kind 'heading'."""
    kind = "general"
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bare = line.lstrip("-•*·– ").rstrip()
        words = bare.split()
        is_heading = (
            line[:1] not in "-•*·–"
            and 0 < len(words) <= 6
            and (bare.endswith(":") or bare.isupper()
                 or _PREFERRED_HEAD.search(bare) is not None
                 or _REQUIRED_HEAD.search(bare) is not None
                 or re.match(r"^(what|who|about|your|our|the role|responsibilities|benefits|"
                             r"we offer|why)", bare, re.I) is not None)
            and not bare.endswith(".")
        )
        if is_heading:
            if _PREFERRED_HEAD.search(bare):
                kind = "preferred"
            elif _REQUIRED_HEAD.search(bare):
                kind = "required"
            else:
                kind = "general"
            out.append(("heading", bare.rstrip(":")))
        else:
            out.append((kind, line))
    return out


def guess_title(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(?:job\s+title|position|role|title)\s*:\s*(.+)$", line, re.I)
        if m:
            line = m.group(1)
        line = re.split(r"\s+[—–|-]\s+|\s+at\s+|,", line)[0]
        line = re.sub(r"\s*\([^)]*\)", "", line).strip()
        return line[:80]
    return ""


def extract_keywords(text: str, *, title: str = "", exclude: tuple[str, ...] = ()
                     ) -> list[Keyword]:
    """Extract weighted keywords (hard skills, tools, titles, seniority, languages)."""
    sections = _split_sections(text)
    excluded = {e.lower() for e in exclude}
    stats: dict[str, dict] = {}

    def record(canonical: str, phrase: str, category: str, weight: float) -> None:
        s = stats.setdefault(canonical, {"phrase": phrase, "category": category,
                                         "weight": 0.0, "count": 0})
        s["count"] += 1
        s["weight"] = max(s["weight"], weight)

    first_line = next((line for _, line in sections), "")
    # Longest forms first: a span claimed by "net revenue retention" is not also counted as
    # "retention", and "Customer Success Manager" is not also counted as "customer success".
    all_forms = sorted(((form, canonical, category) for canonical, (category, _) in TERMS.items()
                        for form in forms(canonical)), key=lambda f: -len(f[0]))
    for kind, line in sections:
        if kind == "heading":
            continue
        weight = SECTION_WEIGHTS[kind]
        if line == first_line:
            weight = max(weight, 2.0)
        scan = _URL_RE.sub(" ", line)
        claimed: list[tuple[int, int]] = []
        for form, canonical, category in all_forms:
            for m in phrase_pattern(form).finditer(scan):
                if any(m.start() < end and start < m.end() for start, end in claimed):
                    continue
                claimed.append(m.span())
                record(canonical, m.group(0), category, weight)
        for m in re.finditer(r"(?<![\w&.])([A-Z][A-Z0-9&]{1,5})(?![\w&])", scan):
            acronym = m.group(1)
            if (acronym in ACRONYM_STOPLIST or acronym.lower() in excluded
                    or any(m.start() < end and start < m.end() for start, end in claimed)):
                continue
            record(acronym, acronym, "other", weight * 0.5)

    for s in stats.values():  # terms in the job title are always must-haves
        if title and find_phrase(s["phrase"], title):
            s["weight"] = max(s["weight"], 2.0)
    keywords = [
        Keyword(canonical=c, phrase=s["phrase"], category=s["category"],
                weight=s["weight"], count=s["count"])
        for c, s in stats.items() if c.lower() not in excluded
    ]
    keywords.sort(key=lambda k: (-k.weight, -k.count, k.canonical.lower()))
    return keywords


def parse_posting(text: str, *, title: str | None = None, company: str = "") -> JobPosting:
    resolved_title = title or guess_title(text)
    exclude = tuple(w for w in re.split(r"\s+", company) if w) if company else ()
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(text)]
    url = _URL_RE.search(text)
    return JobPosting(
        text=text,
        title=resolved_title,
        url=url.group(0).rstrip(".,)") if url else "",
        keywords=extract_keywords(text, title=resolved_title, exclude=exclude),
        years_required=max(years) if years else None,
    )


def match_keywords(keywords: list[Keyword], text: str) -> list[KeywordMatch]:
    """Match each keyword against ``text``: exact posting phrase first, then synonyms."""
    matches = []
    for kw in keywords:
        hit = find_phrase(kw.phrase, text)
        if hit:
            matches.append(KeywordMatch(kw, "exact", hit))
            continue
        synonym_hit = ""
        for form in forms(kw.canonical):
            if form.lower() == kw.phrase.lower():
                continue
            synonym_hit = find_phrase(form, text)
            if synonym_hit:
                break
        matches.append(KeywordMatch(kw, "synonym" if synonym_hit else "missing", synonym_hit))
    return matches


def keyword_score(matches: list[KeywordMatch]) -> float:
    """Weighted coverage 0-100. Synonym matches earn 75% because many ATS miss them.
    Seniority terms are reported separately and do not count towards the score."""
    matches = [m for m in matches if m.keyword.category != "seniority"]  # reported, not scored
    total = sum(m.keyword.weight for m in matches)
    if not total:
        return 0.0
    credit = {"exact": 1.0, "synonym": 0.75, "missing": 0.0}
    got = sum(m.keyword.weight * credit[m.status] for m in matches)
    return round(100 * got / total, 1)


def relevance(text: str, keywords: list[Keyword]) -> float:
    """How strongly a piece of text relates to the posting (sum of matched keyword weights)."""
    score = 0.0
    for m in match_keywords(keywords, text):
        if m.status != "missing":
            score += m.keyword.weight
    return score


def display_term(kw: Keyword) -> str:
    """Human-friendly form of a keyword for use inside a sentence."""
    if kw.category in ("tool", "language", "other", "title"):
        return kw.phrase
    if any(ch.isupper() for ch in kw.canonical):
        return kw.canonical  # acronyms and proper nouns: SaaS, KPI, GDPR
    return kw.canonical.lower()
