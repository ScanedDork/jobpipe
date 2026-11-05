from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from jobpipe.keywords import parse_posting
from jobpipe.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROFILE = ROOT / "profile" / "profile.example.yaml"
TEMPLATE_PROFILE = ROOT / "profile" / "profile.template.yaml"
SAMPLE_POSTING = ROOT / "examples" / "posting_acme.txt"


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Tests never reach a real LLM: provider keys are removed, dry-run is forced and any
    attempt to open a network connection fails loudly."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "MODEL", "JOBPIPE_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JOBPIPE_PROVIDER", "dry-run")

    def _blocked(*args, **kwargs):
        raise AssertionError("Network access attempted during tests")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)


@pytest.fixture
def profile():
    return load_profile(EXAMPLE_PROFILE)


@pytest.fixture
def template_profile():
    return load_profile(TEMPLATE_PROFILE)


@pytest.fixture
def posting_text():
    return SAMPLE_POSTING.read_text(encoding="utf-8")


@pytest.fixture
def job(posting_text):
    return parse_posting(posting_text, company="Acme")
