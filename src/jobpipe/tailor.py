"""Tailoring: reorder, emphasise and rephrase profile facts for one job posting.

The LLM may only rephrase. Ordering is deterministic (keyword relevance), every generated
sentence goes through the claims check and the hard-rule validators, and anything that fails
is replaced by the profile's own wording or a deterministic fact-only fallback.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from jobpipe.keywords import (
    ACTION_VERBS,
    JobPosting,
    Keyword,
    display_term,
    match_keywords,
    relevance,
)
from jobpipe.llm import LLMClient, LLMError, LLMRequest, render_prompt
from jobpipe.profile import Certification, Experience, Profile, is_placeholder
from jobpipe.validators import (
    WORK_AUTH_STATEMENT,
    Issue,
    check_placeholders,
    check_work_authorisation,
    claims_check,
    letter_claims_check,
    letter_parts,
    validate_cover_letter,
    word_count,
)

MIN_LETTER_WORDS, MAX_LETTER_WORDS = 250, 350
MAX_EVIDENCE_POINTS = 3  # the brief: 2-3 evidence points per cover letter


@dataclass
class TailoredRole:
    experience: Experience
    bullets: list[str]


@dataclass
class TailoredResume:
    profile: Profile
    summary: str
    roles: list[TailoredRole]
    skills: list[str]
    certifications: list[Certification]
    keywords: list[Keyword]  # posting keywords the profile supports
    rejected: list[Issue] = field(default_factory=list)  # rephrasings reverted by checks
    notes: list[str] = field(default_factory=list)

    def all_text(self) -> str:
        parts = [self.summary, *self.skills]
        for role in self.roles:
            parts += role.experience.context + role.bullets
        return "\n".join(parts)


@dataclass
class CoverLetter:
    text: str
    attempts: int
    used_fallback: bool
    rejected: list[tuple[int, list[Issue]]] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --- helpers ------------------------------------------------------------------------------

def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _article(word: str) -> str:
    if not word:
        return "a"
    if word[0].lower() in "aeiou":
        return "an"
    if len(word) > 1 and word[0] in "FHLMNRSX" and (word[1].isupper() or word[1] == "."):
        return "an"  # an MSc, an M.Tech, an MBA
    return "a"


def _lower_first(text: str) -> str:
    if len(text) > 1 and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def _clean_llm_text(text: str) -> str:
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    text = text.replace("**", "").replace("__", "")
    return re.sub(r"^#+\s*", "", text, flags=re.M).strip()


def _parse_json_list(text: str) -> list[str] | None:
    text = _clean_llm_text(text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        return None
    return [" ".join(x.split()) for x in data]


def _ask(llm: LLMClient, request: LLMRequest, notes: list[str]) -> str:
    try:
        return llm.complete(request)
    except LLMError as exc:
        notes.append(f"{request.task}: provider error, used fact-only fallback ({exc})")
        return request.fallback


def supported_keywords(job: JobPosting, profile: Profile) -> list[Keyword]:
    """Posting keywords that the profile itself evidences (exact phrase or synonym)."""
    facts = "\n".join(profile.fact_texts())
    return [m.keyword for m in match_keywords(job.keywords, facts)
            if m.status != "missing" and m.keyword.category != "seniority"]


def _by_relevance(items: list, keywords: list[Keyword], key=str) -> list:
    scored = [(relevance(key(item), keywords), i, item) for i, item in enumerate(items)]
    return [item for _, _, item in sorted(scored, key=lambda t: (-t[0], t[1]))]


def _facts_block(profile: Profile) -> str:
    return "\n".join(f"- {fact}" for fact in profile.fact_texts())


def _keywords_line(keywords: list[Keyword]) -> str:
    return ", ".join(display_term(k) for k in keywords) or "(none)"


# --- resume -------------------------------------------------------------------------------

def _tailor_bullets(llm: LLMClient, exp: Experience, *, company: str, role: str,
                    keywords: list[Keyword], system: str, rejected: list[Issue],
                    notes: list[str]) -> list[str]:
    originals = exp.real_bullets
    if not originals:
        return []
    request = LLMRequest(
        task="tailor_bullets",
        system=system,
        user=render_prompt(
            "tailor_bullets", role=role, company=company, title=exp.title,
            employer=exp.company, dates=exp.dates or "dates not given",
            keywords=_keywords_line(keywords),
            bullets_json=json.dumps(originals, ensure_ascii=False, indent=2),
            count=str(len(originals)),
        ),
        fallback=json.dumps(originals, ensure_ascii=False),
    )
    proposed = _parse_json_list(_ask(llm, request, notes))
    if proposed is None or len(proposed) != len(originals):
        notes.append(f"{exp.title} at {exp.company}: model returned an unusable bullet list; "
                     "kept the profile wording")
        return originals
    result = []
    for original, new in zip(originals, proposed, strict=True):
        issues = claims_check(new, [original, *exp.context], check_novelty=True)
        issues += check_work_authorisation(new) + check_placeholders(new)
        if issues:
            rejected.extend(issues)
            result.append(original)
        else:
            result.append(new)
    return result


def fallback_summary(profile: Profile, keywords: list[Keyword] = ()) -> str:
    """Fact-only summary: the profile's own summary, or one assembled from its facts."""
    if not is_placeholder(profile.summary):
        return profile.summary
    parts = []
    if not is_placeholder(profile.headline):
        parts.append(profile.headline.rstrip(".") + ".")
    if profile.experience:
        exp = profile.experience[0]
        clause = f"{exp.title}, {exp.company}"
        if exp.dates:
            clause += f" ({exp.dates})"
        if not is_placeholder(exp.description):
            clause += f": {_lower_first(exp.description.rstrip('.'))}"
        parts.append(clause + ".")
    if profile.education:
        edu = profile.education[0]
        parts.append(f"{edu.degree}, {edu.institution}.")
    certs = [c.name for c in _by_relevance(profile.certifications, list(keywords),
                                           key=lambda c: c.name)][:3]
    if certs and not is_placeholder(profile.certification_hours):
        parts.append(f"{profile.certification_hours} hours of professional certifications, "
                     f"including {_join(certs)}.")
    elif certs:
        parts.append(f"Certifications include {_join(certs)}.")
    return " ".join(parts)


def _tailor_summary(llm: LLMClient, profile: Profile, *, company: str, role: str,
                    keywords: list[Keyword], system: str, rejected: list[Issue],
                    notes: list[str]) -> str:
    fallback = fallback_summary(profile, keywords)
    if not fallback:
        return ""
    request = LLMRequest(
        task="summary",
        system=system,
        user=render_prompt("summary", role=role, company=company,
                           facts=_facts_block(profile), keywords=_keywords_line(keywords),
                           current_summary=fallback),
        fallback=fallback,
    )
    text = " ".join(_clean_llm_text(_ask(llm, request, notes)).split())
    issues = claims_check(text, profile.fact_texts()) + check_work_authorisation(text)
    issues += check_placeholders(text)
    if word_count(text) > 80:
        issues.append(Issue("summary_length", "Summary longer than 80 words.", excerpt=text[:80]))
    if issues:
        rejected.extend(issues)
        return fallback
    return text


def tailor_resume(profile: Profile, job: JobPosting, llm: LLMClient, *, company: str,
                  role: str) -> TailoredResume:
    keywords = supported_keywords(job, profile)
    system = render_prompt("system")
    rejected: list[Issue] = []
    notes: list[str] = []

    roles = []
    for exp in profile.experience:  # keep reverse-chronological order of roles
        bullets = _tailor_bullets(llm, exp, company=company, role=role, keywords=keywords,
                                  system=system, rejected=rejected, notes=notes)
        roles.append(TailoredRole(exp, _by_relevance(bullets, keywords)))

    summary = _tailor_summary(llm, profile, company=company, role=role, keywords=keywords,
                              system=system, rejected=rejected, notes=notes)
    skills = _by_relevance([s for s in profile.skills if not is_placeholder(s)], keywords)
    certs = _by_relevance([c for c in profile.certifications if not is_placeholder(c.name)],
                          keywords, key=lambda c: c.name)
    placeholders = profile.placeholders()
    if placeholders:
        notes.append(f"{len(placeholders)} TODO placeholder(s) in the profile were left out: "
                     + ", ".join(placeholders))
    return TailoredResume(profile=profile, summary=summary, roles=roles, skills=skills,
                          certifications=certs, keywords=keywords, rejected=rejected,
                          notes=notes)


# --- cover letter -------------------------------------------------------------------------

def _first_person(bullet: str) -> str | None:
    words = bullet.split()
    if words and words[0].lower().strip(",") in ACTION_VERBS:
        return bullet[0].lower() + bullet[1:].rstrip(".")
    return None


def evidence_points(tailored: TailoredResume, limit: int = 4) -> list[tuple[Experience, str]]:
    """Strongest bullets for a letter: relevant to the posting, quantified, verb-led."""
    scored = []
    for r_index, role in enumerate(tailored.roles):
        for b_index, bullet in enumerate(role.bullets):
            score = relevance(bullet, tailored.keywords)
            score += 1.5 if re.search(r"\d", bullet) else 0
            score += 0.5 if _first_person(bullet) else 0
            scored.append((score, r_index, b_index, role.experience, bullet))
    top = sorted(scored, key=lambda t: (-t[0], t[1], t[2]))[:limit]
    return [(exp, bullet) for _, _, _, exp, bullet in sorted(top, key=lambda t: (t[1], t[2]))]


def _posting_sentences(posting: str) -> list[str]:
    """Plain prose sentences of a posting (bullet lists and headings excluded)."""
    paragraphs, current = [], []
    for line in [*posting.splitlines(), ""]:
        line = line.strip()
        if line and line[0] not in "-•*·–":
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    return [s for p in paragraphs for s in re.split(r"(?<=[.!?])\s+", p)]


_NEED_RE = re.compile(r"\b(?:need|needs|looking\s+for|hiring|seeking|growing|building|"
                      r"expanding|scaling)\b", re.I)
_PRONOUNS = {"we": "you", "our": "your", "us": "you", "ours": "yours", "we're": "you're",
             "we've": "you've", "we'll": "you'll"}


def _need_sentence(posting: str, role: str) -> str:
    """The posting's own 'we are hiring/growing...' sentence, turned to second person."""
    for sentence in _posting_sentences(posting):
        words = sentence.split()
        if (re.match(r"We(?:\s|'(?:re|ve|ll)\b)", sentence) and 8 <= len(words) <= 40
                and (role.lower() in sentence.lower() or _NEED_RE.search(sentence))
                and not re.search(r"\b(?:offer|apply|benefits?|salary)\b|http", sentence,
                                  re.I)):
            def swap(m: re.Match) -> str:
                word = m.group(0)
                new = _PRONOUNS[word.lower()]
                return new.capitalize() if word[0].isupper() else new
            return re.sub(r"\b(?:we're|we've|we'll|we|our|ours|us)\b", swap, sentence,
                          flags=re.I).rstrip(".!? ")
    return ""


def _company_sentence(posting: str, company: str) -> str:
    """First plain sentence of the posting that describes the company, for the hook."""
    if not company:
        return ""
    for sentence in _posting_sentences(posting):
        words = sentence.split()
        if (sentence.lower().startswith(company.lower()) and 8 <= len(words) <= 35
                and "http" not in sentence and not re.search(r"\bapply\b", sentence, re.I)):
            return sentence.rstrip(".!? ")
    return ""


def build_fallback_letter(tailored: TailoredResume, *, company: str, role: str,
                          job_text: str = "") -> str:
    """Deterministic, fact-only cover letter that always satisfies the hard rules."""
    profile = tailored.profile
    ranked = sorted(tailored.keywords, key=lambda k: {"skill": 0, "tool": 1}.get(k.category, 2))
    focus = [display_term(k) for k in ranked if k.category in ("skill", "tool")][:3]
    about = _company_sentence(job_text, company)
    need = _need_sentence(job_text, role)
    if about and need and focus:
        # Company facts stay in their own sentences, out of the first-person claim sentence.
        hook = (f"{about}. {need}. The role centres on {_join(focus)}, areas my background "
                "speaks to directly.")
    elif about and focus:
        hook = (f"{about}. Your {role} opening centres on {_join(focus)}, areas my "
                "background speaks to directly.")
    elif focus:
        hook = (f"Your {role} opening at {company} stands out to me because it centres on "
                f"{_join(focus)}, areas my background speaks to directly.")
    else:
        hook = (f"Your {role} opening at {company} stands out to me because it builds on the "
                "work I have done so far.")

    evidence = evidence_points(tailored, limit=MAX_EVIDENCE_POINTS)
    evidence_sentences: list[str] = []
    last_employer = None
    same_role = 0
    for exp, bullet in evidence:
        clause = _first_person(bullet)
        if clause is None:
            sentence = bullet.rstrip(".") + "."
        elif exp.company != last_employer:
            same_role = 0
            if last_employer is None:
                recent = profile.experience and exp is profile.experience[0]
                sentence = f"There, I {clause}." if recent else f"At {exp.company}, I {clause}."
            else:
                sentence = f"Before that, at {exp.company}, I {clause}."
        else:
            same_role += 1
            lead = ("I also", "In addition, I", "I also")[(same_role - 1) % 3]
            sentence = f"{lead} {clause}."
        last_employer = exp.company
        evidence_sentences.append(sentence)

    context = ""
    if profile.experience:
        exp = profile.experience[0]
        start = "" if is_placeholder(exp.start) else exp.start
        end = "" if is_placeholder(exp.end) else exp.end
        if start and end and end.lower() not in ("present", "current", "now", "today"):
            context = f"From {start} to {end}, I worked as {exp.title} at {exp.company}"
        elif start:
            context = f"Since {start}, I have worked as {exp.title} at {exp.company}"
        else:
            context = f"Most recently, I worked as {exp.title} at {exp.company}"
        if not is_placeholder(exp.description):
            desc = _lower_first(exp.description.rstrip("."))
            context += f", {_article(desc)} {desc}"
        context += "."

    credentials: dict[str, str] = {}
    if profile.education:
        edu = profile.education[0]
        credentials["education"] = (f"I hold {_article(edu.degree)} {edu.degree} from "
                                    f"{edu.institution}.")
    cert_names = [c.name for c in tailored.certifications][:3]
    if cert_names and not is_placeholder(profile.certification_hours):
        credentials["certs"] = (f"I have completed {profile.certification_hours} hours of "
                                f"professional certifications, including {_join(cert_names)}.")
    elif cert_names:
        credentials["certs"] = f"My certifications include {_join(cert_names)}."
    toolkit = tailored.skills[:5]
    if toolkit:
        credentials["skills"] = f"My toolkit includes {_join(toolkit)}."
    already = {t.lower() for t in [*focus, *toolkit]}
    more = [display_term(k) for k in ranked if k.category in ("skill", "tool")
            and display_term(k).lower() not in already][:6]
    if len(more) >= 2:
        credentials["coverage"] = f"My background also covers {_join(more)}."
    languages = [lang for lang in profile.languages if not is_placeholder(lang)]
    if languages:
        credentials["languages"] = f"I work in {_join(languages)}."

    work_auth = ""
    if profile.work_authorisation == WORK_AUTH_STATEMENT:
        place = profile.location if not is_placeholder(profile.location) else ""
        work_auth = (f"I am based in {place}: {WORK_AUTH_STATEMENT}." if place
                     else f"{WORK_AUTH_STATEMENT}.")
    close = (f"I would welcome the chance to discuss how I could contribute to {company} as "
             f"{_article(role)} {role}. Thank you for your time and consideration.")

    # Mandatory pieces first, then optional ones in priority order while the body stays
    # within 250-350 words.
    chosen = {"context": bool(context), "evidence": min(2, len(evidence_sentences))}
    # Never more than MAX_EVIDENCE_POINTS evidence points: length comes from credentials.
    optional = ["evidence3", "education", "certs", "languages", "skills", "coverage", "offer"]
    offer = "I would be glad to walk you through how I delivered these results in more detail."

    def assemble() -> list[str]:
        p1 = " ".join(x for x in (hook, context if chosen["context"] else "") if x)
        p2 = " ".join(evidence_sentences[: chosen["evidence"]])
        creds = [credentials[k] for k in ("education", "certs", "skills", "coverage",
                                          "languages") if chosen.get(k)]
        p3 = " ".join([*creds, work_auth]).strip()
        p4 = f"{offer} {close}" if chosen.get("offer") else close
        return [p for p in (p1, p2, p3, p4) if p]

    for item in optional:
        if word_count(" ".join(assemble())) >= MIN_LETTER_WORDS:
            break
        before = dict(chosen)
        if item.startswith("evidence"):
            wanted = int(item[-1])
            if len(evidence_sentences) >= wanted:
                chosen["evidence"] = wanted
        elif item in credentials or (item == "offer" and any(
                re.search(r"\d", e) for e in evidence_sentences)):
            chosen[item] = True
        if word_count(" ".join(assemble())) > MAX_LETTER_WORDS:
            chosen = before

    salutation = f"Dear Hiring Team at {company},"
    body = "\n\n".join(assemble())
    return f"{salutation}\n\n{body}\n\nKind regards,\n{profile.preferred_name}"


def write_cover_letter(tailored: TailoredResume, job: JobPosting, llm: LLMClient, *,
                       company: str, role: str, max_attempts: int = 3) -> CoverLetter:
    """Generate a letter, validate it, retry with feedback, fall back if needed."""
    profile = tailored.profile
    facts = profile.fact_texts()
    fallback = build_fallback_letter(tailored, company=company, role=role, job_text=job.text)
    evidence = "\n".join(f"- {b}" for _, b in evidence_points(tailored, limit=3)) or "(none)"
    if profile.work_authorisation == WORK_AUTH_STATEMENT:
        auth_instruction = (f'Mention work authorisation once, using exactly: '
                            f'"{WORK_AUTH_STATEMENT}". Do not explain it.')
    else:
        auth_instruction = "Do not mention work authorisation, visas or relocation."
    base_user = render_prompt(
        "cover_letter", name=profile.preferred_name, role=role, company=company,
        facts=_facts_block(profile), posting=job.text[:4000],
        keywords=_keywords_line(tailored.keywords), evidence=evidence,
        work_auth_instruction=auth_instruction,
    )
    system = render_prompt("system")

    def check(text: str) -> list[Issue]:
        return (validate_cover_letter(text, company=company, min_words=MIN_LETTER_WORDS,
                                      max_words=MAX_LETTER_WORDS)
                + letter_claims_check(text, facts, allow=[company, role],
                                      known_names=job.text))

    notes: list[str] = []
    rejected: list[tuple[int, list[Issue]]] = []
    best: tuple[str, list[Issue]] | None = None
    user = base_user
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            text = _clean_llm_text(llm.complete(
                LLMRequest(task="cover_letter", system=system, user=user, fallback=fallback)))
        except LLMError as exc:
            notes.append(f"cover_letter: provider error ({exc}); used fact-only fallback")
            break
        issues = check(text)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            rejected.append((attempt, errors))
        else:
            if best is None:
                best = (text, issues)
            if not issues or text == fallback:  # retrying cannot improve a deterministic text
                break
        feedback = "\n".join(f"- {i.message} Offending text: \"{i.excerpt}\"" if i.excerpt
                             else f"- {i.message}" for i in issues)
        user = (f"{base_user}\n\nYOUR PREVIOUS DRAFT WAS REJECTED FOR THESE REASONS:\n"
                f"{feedback}\nRewrite the whole letter and fix every point.")

    if best is not None:
        text, warnings = best
        return CoverLetter(text=text, attempts=attempts, used_fallback=text == fallback,
                           rejected=rejected, warnings=warnings, notes=notes)

    issues = check(fallback)
    errors = [i for i in issues if i.severity == "error"]
    if errors:  # the fallback is built from profile facts only, so this signals a bug
        raise RuntimeError("Fallback cover letter breaks a hard rule: "
                           + "; ".join(f"{i.code}: {i.excerpt}" for i in errors))
    notes.append(f"All {attempts} model draft(s) broke a hard rule; used the fact-only "
                 "fallback letter")
    return CoverLetter(text=fallback, attempts=attempts, used_fallback=True, rejected=rejected,
                       warnings=issues, notes=notes)


def letter_body_words(text: str) -> int:
    _, paragraphs, _ = letter_parts(text)
    return word_count(" ".join(paragraphs))
