"""Candidate profile: the single source of truth for every fact in generated documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobpipe.validators import WORK_AUTH_STATEMENT, check_work_authorisation


class ProfileError(ValueError):
    """The profile file is missing, malformed or breaks a hard rule."""


def is_placeholder(value: Any) -> bool:
    """Empty values and anything containing TODO are placeholders, never rendered."""
    if value is None:
        return True
    text = str(value).strip()
    return not text or "TODO" in text


def _clean(values: list[str]) -> list[str]:
    return [v for v in values if not is_placeholder(v)]


@dataclass
class Experience:
    title: str
    company: str
    start: str = ""
    end: str = ""
    location: str = ""
    description: str = ""
    bullets: list[str] = field(default_factory=list)

    @property
    def facts(self) -> list[str]:
        return _clean([self.title, self.company, self.start, self.end, self.location,
                       self.description, *self.bullets])

    @property
    def context(self) -> list[str]:
        """Facts about the role itself (not its bullets), allowed in any of its bullets."""
        return _clean([self.title, self.company, self.start, self.end, self.location,
                       self.description])

    @property
    def real_bullets(self) -> list[str]:
        return _clean(self.bullets)

    @property
    def dates(self) -> str:
        start = "" if is_placeholder(self.start) else self.start
        end = "" if is_placeholder(self.end) else self.end
        return f"{start} – {end}" if start and end else start or end


@dataclass
class Education:
    degree: str
    institution: str
    location: str = ""
    year: str = ""


@dataclass
class Certification:
    name: str
    issuer: str = ""
    year: str = ""


@dataclass
class Profile:
    full_name: str
    preferred_name: str
    headline: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    linkedin: str = ""
    website: str = ""
    work_authorisation: str = ""
    target_roles: list[str] = field(default_factory=list)
    summary: str = ""
    experience: list[Experience] = field(default_factory=list)
    education: list[Education] = field(default_factory=list)
    certification_hours: str = ""
    certifications: list[Certification] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    source: Path | None = None

    def contact_items(self) -> list[str]:
        return _clean([self.location, self.email, self.phone, self.linkedin, self.website])

    def fact_texts(self) -> list[str]:
        """Every statement the generator may draw on. Target roles are aspirations, not
        facts, so they are deliberately excluded."""
        texts = _clean([self.full_name, self.preferred_name, self.headline, self.location,
                        self.summary, self.certification_hours])
        for exp in self.experience:
            texts += exp.facts
        for edu in self.education:
            texts += _clean([edu.degree, edu.institution, edu.location, edu.year])
        for cert in self.certifications:
            texts += _clean([cert.name, cert.issuer, cert.year])
        texts += _clean(self.skills) + _clean(self.languages)
        if self.work_authorisation:
            texts.append(self.work_authorisation)
        return texts

    def placeholders(self) -> list[str]:
        """Dotted paths of fields still holding TODO placeholders."""
        found = []

        def visit(path: str, value: Any) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    visit(f"{path}.{k}" if path else str(k), v)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    visit(f"{path}[{i}]", v)
            elif isinstance(value, str) and "TODO" in value:
                found.append(path)

        visit("", self._raw)
        return found

    _raw: dict = field(default_factory=dict, repr=False)


_TOP_LEVEL_KEYS = {
    "name", "headline", "location", "contact", "work_authorisation", "target_roles", "summary",
    "experience", "education", "certification_hours", "certifications", "skills", "languages",
}


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProfileError(f"'{where}' must be a list")
    return [_str(v) for v in value]


def _records(value: Any, where: str, required: tuple[str, ...], allowed: tuple[str, ...]
             ) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ProfileError(f"'{where}' must be a list of mappings")
    for i, item in enumerate(value):
        unknown = set(item) - set(allowed)
        if unknown:
            raise ProfileError(f"{where}[{i}] has unknown keys: {', '.join(sorted(unknown))}")
        for key in required:
            if not _str(item.get(key)):
                raise ProfileError(f"{where}[{i}] is missing '{key}'")
    return value


def parse_profile(data: Any, source: Path | None = None) -> Profile:
    if not isinstance(data, dict):
        raise ProfileError("Profile must be a YAML mapping")
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ProfileError(f"Unknown profile keys: {', '.join(sorted(unknown))}")

    name = data.get("name") or {}
    if not isinstance(name, dict):
        raise ProfileError("'name' must have 'full' and 'preferred'")
    preferred = _str(name.get("preferred")) or _str(name.get("full"))
    if is_placeholder(preferred):
        raise ProfileError("name.preferred is required (it is used in output file names)")

    contact = data.get("contact") or {}
    if not isinstance(contact, dict):
        raise ProfileError("'contact' must be a mapping")

    work_auth = _str(data.get("work_authorisation"))
    if work_auth and work_auth != WORK_AUTH_STATEMENT:
        raise ProfileError(
            f"work_authorisation must be exactly '{WORK_AUTH_STATEMENT}' or empty "
            f"(got '{work_auth}')"
        )

    exp_keys = ("title", "company", "location", "start", "end", "description", "bullets")
    experience = [
        Experience(
            title=_str(e.get("title")), company=_str(e.get("company")),
            start=_str(e.get("start")), end=_str(e.get("end")),
            location=_str(e.get("location")), description=_str(e.get("description")),
            bullets=_str_list(e.get("bullets"), f"experience[{i}].bullets"),
        )
        for i, e in enumerate(_records(data.get("experience"), "experience",
                                       ("title", "company"), exp_keys))
    ]
    education = [
        Education(degree=_str(e.get("degree")), institution=_str(e.get("institution")),
                  location=_str(e.get("location")), year=_str(e.get("year")))
        for e in _records(data.get("education"), "education", ("degree", "institution"),
                          ("degree", "institution", "location", "year"))
    ]
    certifications = [
        Certification(name=_str(c.get("name")), issuer=_str(c.get("issuer")),
                      year=_str(c.get("year")))
        for c in _records(data.get("certifications"), "certifications", ("name",),
                          ("name", "issuer", "year"))
    ]

    profile = Profile(
        full_name=_str(name.get("full")) or preferred,
        preferred_name=preferred,
        headline=_str(data.get("headline")),
        location=_str(data.get("location")),
        email=_str(contact.get("email")),
        phone=_str(contact.get("phone")),
        linkedin=_str(contact.get("linkedin")),
        website=_str(contact.get("website")),
        work_authorisation=work_auth,
        target_roles=_str_list(data.get("target_roles"), "target_roles"),
        summary=_str(data.get("summary")),
        experience=experience,
        education=education,
        certification_hours=_str(data.get("certification_hours")),
        certifications=certifications,
        skills=_str_list(data.get("skills"), "skills"),
        languages=_str_list(data.get("languages"), "languages"),
        source=source,
        _raw=data,
    )

    # Hard rule 2 at the source: no other work-authorisation wording or relocation reasons.
    facts = [t for t in profile.fact_texts() if t != WORK_AUTH_STATEMENT]
    issues = check_work_authorisation("\n".join(facts))
    if issues:
        details = "; ".join(f"{i.message} -> '{i.excerpt}'" for i in issues)
        raise ProfileError(f"Profile breaks the work-authorisation rule: {details}")
    return profile


def load_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.is_file():
        raise ProfileError(
            f"Profile not found: {path}. Copy profile/profile.template.yaml to "
            "profile/profile.yaml and fill it in, or pass --profile "
            "profile/profile.example.yaml to try the demo."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProfileError(f"{path} is not valid YAML: {exc}") from exc
    return parse_profile(data, source=path)
