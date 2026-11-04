"""Hard-rule validators and ATS constants.

Hard rules (each enforced here and covered by tests):
  1. Cover letters never open with "I am writing to apply for" or close variants.
  2. Work authorisation is only ever stated as WORK_AUTH_STATEMENT, with no relocation
     reasons or personal relationships.
  3. Output file names follow {Name}_Resume_{Company}.docx / {Name}_CoverLetter_{Company}.docx
     / {Company}_ats_report.md, with every part sanitised for the filesystem.
  4. Generated text may only use facts present in the profile (the "claims check").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jobpipe.keywords import ACTION_VERBS, NUMBER_WORDS, find_terms

WORK_AUTH_STATEMENT = "EU work authorised, residency in process — no sponsorship required"

STANDARD_FONTS = {
    "arial", "calibri", "cambria", "garamond", "georgia", "helvetica", "times new roman",
    "verdana", "tahoma", "trebuchet ms", "book antiqua", "palatino linotype", "aptos",
    "segoe ui", "liberation sans", "liberation serif",
}

# Section key -> heading names ATS parsers reliably recognise.
STANDARD_HEADINGS: dict[str, set[str]] = {
    "summary": {"summary", "professional summary", "profile", "professional profile",
                "career summary", "executive summary"},
    "experience": {"experience", "professional experience", "work experience",
                   "employment history", "work history", "career history",
                   "relevant experience"},
    "education": {"education", "education and training", "academic background"},
    "skills": {"skills", "key skills", "core skills", "technical skills", "core competencies",
               "skills and tools"},
    "certifications": {"certifications", "certificates", "licenses and certifications",
                       "licences and certifications", "certifications and training",
                       "training", "professional development"},
    "languages": {"languages", "language skills"},
    "other": {"projects", "volunteering", "volunteer experience", "awards", "publications",
              "interests", "references", "additional information"},
}

PLACEHOLDER_RE = re.compile(
    r"\bTODO\b|\[(?:company|company name|your name|name|role|position|hiring manager|x+)\]|"
    r"\{\{?\s*\w+\s*\}?\}|lorem ipsum|\bXX+%?",
    re.I,
)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "error"  # "error" blocks generation output; "warning" is reported only
    excerpt: str = ""


def _norm(text: str) -> str:
    return (text.replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"'))


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    right_candidates = [i for i in (text.find(".", end), text.find("\n", end)) if i != -1]
    right = min(right_candidates) + 1 if right_candidates else len(text)
    return " ".join(text[left:right].split())[:200]


# --- Rule 1: cover letter openings -------------------------------------------------------

_BANNED_OPENINGS = [
    r"i\s*(?:am|'m)\s+(?:now\s+)?writing\b",
    r"i\s+write\s+(?:to|in|with|regarding|because)\b",
    r"i(?:\s+would|'d)\s+(?:like|love)\s+to\s+(?:apply|express|submit|be\s+considered|"
    r"put\s+(?:myself|my\s+name)|introduce|present)\b",
    r"i\s+(?:wish|want|hope|intend)\s+to\s+(?:apply|express|submit|be\s+considered)\b",
    r"i\s*(?:am|'m)\s+(?:hereby\s+|now\s+)?(?:applying|submitting|reaching\s+out|"
    r"contacting\s+you)\b",
    r"i\s*(?:am|'m)\s+(?:very\s+|so\s+|truly\s+|really\s+|most\s+)?(?:excited|pleased|"
    r"delighted|thrilled|eager|keen|happy|glad|honou?red)\s+to\s+(?:apply|submit|express|"
    r"write|present|put)\b",
    r"i\s*(?:am|'m)\s+(?:very\s+|really\s+|highly\s+|extremely\s+)?interested\s+in\s+"
    r"(?:applying|the\b|your\b|this\b|joining|a\b)",
    r"(?:please\s+)?(?:accept|consider|find)\s+(?:this|my|the\s+enclosed|attached)\b",
    r"please\s+(?:accept|consider|find)\b",
    r"(?:this|my)\s+(?:letter|application)\s+(?:is|serves)\b",
    r"with\s+(?:this|the\s+present)\s+(?:letter|application)\b",
    r"enclosed\s+(?:please\s+find|is|you\s+will\s+find)\b",
    r"(?:it\s+is\s+)?with\s+(?:great\s+|much\s+)?(?:enthusiasm|interest|pleasure|excitement)\b",
    r"allow\s+me\s+to\s+(?:introduce|express|apply|present)\b",
    r"i\s+(?:am\s+)?(?:applying|apply)\s+for\b",
    r"in\s+(?:response|reply|reference)\s+to\s+your\s+(?:job\s+)?(?:advert|advertisement|"
    r"posting|listing|vacancy)",
]
_BANNED_OPENING_RE = re.compile(r"^(?:" + "|".join(_BANNED_OPENINGS) + r")", re.I)
_SALUTATION_RE = re.compile(
    r"^(?:dear|hello|hi|hey|greetings|good\s+(?:morning|afternoon)|to\s+whom)\b[^\n,:!]*[,:!]?",
    re.I,
)
_SIGNOFF_RE = re.compile(
    r"^(?:kind|best|warm|with\s+best)?\s*regards\b|^(?:yours\s+(?:sincerely|faithfully|truly))|"
    r"^sincerely\b|^best(?:\s+wishes)?,?$|^thank\s+you,?$|^many\s+thanks,?$",
    re.I,
)


def letter_parts(text: str) -> tuple[str, list[str], str]:
    """Split letter text into (salutation, body paragraphs, sign-off block)."""
    lines = [ln.strip() for ln in _norm(text).replace("\r\n", "\n").split("\n")]
    salutation = ""
    body_lines: list[str] = []
    signoff_lines: list[str] = []
    for line in lines:
        if signoff_lines:
            signoff_lines.append(line)
            continue
        if not salutation and not any(body_lines) and line:
            m = _SALUTATION_RE.match(line)
            if m:
                salutation = m.group(0).strip()
                rest = line[m.end():].strip()
                if rest:
                    body_lines.append(rest)
                continue
        if line and _SIGNOFF_RE.match(line) and len(line.split()) <= 4:
            signoff_lines.append(line)
            continue
        body_lines.append(line)
    paragraphs, current = [], []
    for line in body_lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return salutation, paragraphs, "\n".join(x for x in signoff_lines if x)


def opening_sentence(text: str) -> str:
    _, paragraphs, _ = letter_parts(text)
    if not paragraphs:
        return ""
    first = paragraphs[0].lstrip("\"' ")
    m = re.search(r"(?<=[.!?])\s", first)
    return first[: m.start()] if m else first


def check_cover_letter_opening(text: str) -> list[Issue]:
    sentence = opening_sentence(text)
    if sentence and _BANNED_OPENING_RE.match(sentence):
        return [Issue(
            "banned_opening",
            "Cover letter opens with a banned formula (e.g. 'I am writing to apply for'). "
            "Open with a specific hook about the company and role instead.",
            excerpt=sentence[:160],
        )]
    return []


# --- Rule 2: work authorisation and relocation reasons -----------------------------------

_WORK_AUTH_RE = re.compile(
    r"\bwork(?:ing)?[\s-]+(?:permit|visa|authori[sz](?:ation|ed|sation|ised)|rights?)\b"
    r"|\bauthori[sz]ed\s+to\s+work\b|\b(?:right|permission|eligib(?:le|ility)|entitled|"
    r"allowed)\s+to\s+work\s+(?:in|within|across)\b|\bvisas?\b|\bresiden(?:cy|ce)\b"
    r"|\bresident\s+permit\b|\bpermanent\s+resident\b|\bblue\s+card\b|\bgreen\s+card\b"
    r"|\bcitizen(?:ship)?\b|\bemployment\s+(?:pass|permit|authori[sz]ation)\b"
    r"|\b(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:required|needed|necessary)\b"
    r"|\b(?:require|requires|need|needs|requiring)\s+(?:no\s+|any\s+)?(?:visa\s+)?sponsorship\b"
    r"|\bwithout\s+(?:visa\s+)?sponsorship\b|\bno\s+sponsorship\b|\bsponsor\s+(?:me|my)\b",
    re.I,
)
_RELOCATION_RE = re.compile(
    r"\brelocat\w*"
    r"|\b(?:moved|moving|came|emigrated|immigrated|move)\s+(?:here\s+|back\s+)?to\s+"
    r"(?:prague|brno|ostrava|the\s+czech|czech|czechia|europe|the\s+eu|germany|berlin|munich|"
    r"vienna|austria|the\s+netherlands|amsterdam)\b"
    r"|\b(?:my|our)\s+(?:wife|husband|spouse|girlfriend|boyfriend|fianc[eé]e?|marriage|"
    r"children|kids|parents|in-laws)\b"
    r"|\bmy\s+(?:partner|family)\b|\b(?:family|personal)\s+reasons\b|\bgot\s+married\b"
    r"|\bmarried\s+(?:to|a)\b",
    re.I,
)


def check_work_authorisation(text: str) -> list[Issue]:
    """Flag any work-authorisation wording other than the exact statement, and any
    relocation reasons or personal relationships."""
    scrubbed = _norm(text).replace(WORK_AUTH_STATEMENT, " " * len(WORK_AUTH_STATEMENT))
    issues: list[Issue] = []
    seen: set[tuple[str, str]] = set()
    for regex, code, message in (
        (_WORK_AUTH_RE, "work_auth_phrasing",
         f"Work authorisation must be stated exactly as '{WORK_AUTH_STATEMENT}' and "
         "not paraphrased."),
        (_RELOCATION_RE, "relocation_reason",
         "Do not mention relocation, reasons for moving or personal relationships."),
    ):
        for m in regex.finditer(scrubbed):
            excerpt = _sentence_around(scrubbed, m.start(), m.end())
            if (code, excerpt) in seen:
                continue
            seen.add((code, excerpt))
            issues.append(Issue(code, message, excerpt=excerpt))
    return issues


def check_placeholders(text: str) -> list[Issue]:
    return [
        Issue("placeholder", "Unfilled placeholder text.", excerpt=_sentence_around(
            text, m.start(), m.end()))
        for m in PLACEHOLDER_RE.finditer(text)
    ]


def word_count(text: str) -> int:
    return sum(1 for token in text.split() if re.search(r"[A-Za-z0-9À-ɏ]", token))


def validate_cover_letter(text: str, *, company: str = "", min_words: int = 250,
                          max_words: int = 350) -> list[Issue]:
    issues = check_cover_letter_opening(text) + check_work_authorisation(text)
    issues += check_placeholders(text)
    salutation, paragraphs, _ = letter_parts(text)
    words = word_count(" ".join(paragraphs))
    if not min_words <= words <= max_words:
        issues.append(Issue("length", f"Body is {words} words; aim for {min_words}-{max_words}.",
                            severity="warning"))
    if company and company.lower() not in _norm(text).lower():
        issues.append(Issue("no_company_hook",
                            f"The letter never names {company}; the hook must be specific.",
                            severity="warning"))
    if re.search(r"to whom it may concern|dear sir or madam", text, re.I):
        issues.append(Issue("generic_salutation", "Generic salutation; address the hiring "
                            "team or a named person.", severity="warning"))
    return issues


# --- Rule 3: output file names -----------------------------------------------------------

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                     *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename_part(text: str, max_length: int = 60) -> str:
    """Make a name safe for file names on Windows, macOS and Linux (ASCII, no separators)."""
    text = text.replace("&", " and ")
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "_", ascii_text)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-.")
    cleaned = cleaned[:max_length].rstrip("_-")
    if not cleaned:
        raise ValueError(f"{text!r} does not contain any usable characters for a file name")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned += "_"
    return cleaned


def output_filenames(preferred_name: str, company: str) -> dict[str, str]:
    name = sanitize_filename_part(preferred_name)
    comp = sanitize_filename_part(company)
    return {
        "resume": f"{name}_Resume_{comp}.docx",
        "cover": f"{name}_CoverLetter_{comp}.docx",
        "report": f"{comp}_ats_report.md",
    }


_PART = r"[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*"
FILENAME_PATTERNS = {
    "resume": re.compile(rf"^{_PART}_Resume_{_PART}\.docx$"),
    "cover": re.compile(rf"^{_PART}_CoverLetter_{_PART}\.docx$"),
    "report": re.compile(rf"^{_PART}_ats_report\.md$"),
}


def check_output_filename(path: str | Path, kind: str, *, expected: str | None = None
                          ) -> list[Issue]:
    name = Path(path).name
    if expected is not None and name != expected:
        return [Issue("filename", f"File name should be '{expected}'.", excerpt=name)]
    if not FILENAME_PATTERNS[kind].match(name):
        label = {"resume": "{Name}_Resume_{Company}.docx",
                 "cover": "{Name}_CoverLetter_{Company}.docx",
                 "report": "{Company}_ats_report.md"}[kind]
        return [Issue("filename", f"File name does not follow {label}.", excerpt=name)]
    return []


# --- Rule 4: claims check ----------------------------------------------------------------

STOPWORDS = frozenset(
    """
    a an and are as at be been being but by for from had has have he her his i if in
    into is it its me my of on or our over she so than that the their them they this
    those to under up via was we were which while who will with within without you your
    per both each more most other such then there these through during after before
    about also all any can could did do does just not only own same should too very
    would including across key multiple new end-to-end successfully role team teams work
    worked working daily weekly year years month months quarter quarters time use used
    using
    """.split()
)
_NUMBER_RE = re.compile(r"\d[\d,.]*")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9&+.\-]*[A-Za-z0-9])")


def _stem(word: str) -> str:
    w = word.lower().strip("'-")
    for suffix in ("ing", "ed", "es", "s", "ly"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def _numbers(text: str) -> set[str]:
    nums = set()
    for m in _NUMBER_RE.finditer(text):
        token = m.group(0).rstrip(".,").replace(",", "")
        if token:
            nums.add(token)
    for word in _WORD_RE.findall(text.lower()):
        if word in NUMBER_WORDS:
            nums.add(NUMBER_WORDS[word])
    return nums


def _number_words(text: str) -> set[str]:
    """Numeric facts in text, both digits and words, for reference lookups."""
    return _numbers(text) | {w for w in _WORD_RE.findall(text.lower()) if w in NUMBER_WORDS}


def _content_stems(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text)
            if w.lower() not in STOPWORDS and len(w) > 2}


def _proper_nouns(text: str) -> set[str]:
    out = set()
    for sentence in re.split(r"(?<=[.!?:;])\s+|\n+", text):
        sentence = sentence.strip().lstrip("-•*\"'([ ")
        for m in _PROPER_RE.finditer(sentence):
            if m.start() == 0:
                continue
            out.add(m.group(1))
        for m in re.finditer(r"\b[A-Z]{2,}[A-Za-z0-9]*\b", sentence):
            out.add(m.group(0))
    return out


def claims_check(
    text: str,
    reference: Iterable[str],
    *,
    allow: Iterable[str] = (),
    known_names: str = "",
    check_novelty: bool = False,
    max_new_ratio: float = 0.3,
) -> list[Issue]:
    """Compare generated ``text`` with ``reference`` facts and flag unsupported claims.

    ``allow`` lists literal strings that are not claims (the target company and role) and are
    removed before checking. ``known_names`` is extra text whose proper nouns may be named
    (e.g. the job posting), without making its numbers or skills claimable.

    Flags (all severity "error"):
      - numbers (digits or number words) not present in the reference;
      - known tools / skills / titles / languages not present in the reference;
      - proper nouns not present in the reference;
      - with ``check_novelty``: too many content words that appear nowhere in the reference.
    """
    ref_text = "\n".join(reference)
    allowed_text = known_names
    issues: list[Issue] = []
    excerpt = " ".join(text.split())[:160]

    scrubbed = text
    for a in sorted(allow, key=len, reverse=True):
        if a:
            scrubbed = re.sub(re.escape(a), " ", scrubbed, flags=re.I)

    ref_numbers = _number_words(ref_text)
    for num in sorted(_numbers(scrubbed) - ref_numbers):
        issues.append(Issue("unsupported_number",
                            f"Number '{num}' does not appear in the profile.", excerpt=excerpt))

    ref_terms = find_terms(ref_text)
    for canonical, hit in find_terms(scrubbed).items():
        if canonical not in ref_terms:
            issues.append(Issue("unsupported_term",
                                f"'{hit}' is not in the profile, so it cannot be claimed.",
                                excerpt=excerpt))

    ref_lower = (ref_text + "\n" + allowed_text).lower()
    reported = {i.message.split("'")[1].lower() for i in issues if i.code == "unsupported_term"}
    for noun in sorted(_proper_nouns(scrubbed)):
        if noun.lower() in ref_lower or noun.lower() in STOPWORDS or noun.lower() in reported:
            continue
        if noun.lower() in ACTION_VERBS:
            continue
        issues.append(Issue("unsupported_name",
                            f"'{noun}' does not appear in the profile.", excerpt=excerpt))

    if check_novelty:
        generated = _content_stems(scrubbed) - {_stem(v) for v in ACTION_VERBS}
        known = _content_stems(ref_text)
        new = sorted(generated - known)
        if generated and len(new) > max(2, max_new_ratio * len(generated)):
            issues.append(Issue("unsupported_content",
                                "Rephrasing adds content not found in the profile: "
                                + ", ".join(new[:8]), excerpt=excerpt))
    return issues


_FIRST_PERSON_RE = re.compile(r"\b(?:I|I'm|I've|I'd|my|me|myself)\b")


def letter_claims_check(text: str, reference: Iterable[str], *, allow: Iterable[str] = (),
                        known_names: str = "") -> list[Issue]:
    """Claims check for cover letters: only first-person sentences make claims about the
    candidate, so sentences about the company or role are not checked."""
    reference = list(reference)
    allow = list(allow)
    _, paragraphs, _ = letter_parts(text)
    issues: list[Issue] = []
    for paragraph in paragraphs:
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            if _FIRST_PERSON_RE.search(_norm(sentence)):
                issues += claims_check(sentence, reference, allow=allow, known_names=known_names)
    return issues
