"""LLM layer: one small interface, three providers selected by environment variables.

Selection order:
  1. JOBPIPE_PROVIDER=anthropic | openai | dry-run   (explicit choice)
  2. ANTHROPIC_API_KEY set                           -> Anthropic Messages API
  3. OPENAI_BASE_URL or OPENAI_API_KEY set           -> any OpenAI-compatible endpoint
                                                        (OpenAI, Ollama, LM Studio, vLLM...)
  4. nothing set                                     -> dry-run (deterministic, no network)

Every request carries a deterministic ``fallback``: the fact-only text the pipeline would use
if the model's output failed validation. The dry-run provider simply returns it, which makes
the whole pipeline reproducible offline and in tests.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from string import Template
from typing import Protocol

from jobpipe import resource_dir
from jobpipe.validators import WORK_AUTH_STATEMENT

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


class LLMError(RuntimeError):
    """Provider misconfigured or the request failed."""


@dataclass(frozen=True)
class LLMRequest:
    task: str  # e.g. "tailor_bullets", "summary", "cover_letter"
    system: str
    user: str
    fallback: str = ""


class LLMClient(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> str: ...


@dataclass
class DryRunClient:
    """Deterministic provider for tests, CI and offline demos.

    Returns queued ``responses`` first (to script bad drafts in tests), then each request's
    deterministic fallback. Every request is recorded in ``calls``.
    """

    responses: list[str] = field(default_factory=list)
    calls: list[LLMRequest] = field(default_factory=list)
    name: str = "dry-run"

    def complete(self, request: LLMRequest) -> str:
        self.calls.append(request)
        if self.responses:
            return self.responses.pop(0)
        return request.fallback


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LLMError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise LLMError(f"Request to {url} failed: {exc}") from exc


@dataclass
class AnthropicClient:
    api_key: str
    model: str = DEFAULT_ANTHROPIC_MODEL
    base_url: str = "https://api.anthropic.com"
    max_tokens: int = 2048
    timeout: float = 120.0
    name: str = "anthropic"

    def complete(self, request: LLMRequest) -> str:
        data = _post_json(
            f"{self.base_url.rstrip('/')}/v1/messages",
            {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": request.system,
                "messages": [{"role": "user", "content": request.user}],
            },
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            self.timeout,
        )
        text = "".join(block.get("text", "") for block in data.get("content", [])
                       if block.get("type") == "text").strip()
        if not text:
            raise LLMError(f"Anthropic returned no text (stop_reason={data.get('stop_reason')})")
        return text


@dataclass
class OpenAICompatibleClient:
    base_url: str
    model: str
    api_key: str = ""
    temperature: float = 0.3
    timeout: float = 120.0
    name: str = "openai-compatible"

    def complete(self, request: LLMRequest) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        data = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
            },
            headers,
            self.timeout,
        )
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected response from {self.base_url}: {data!r:.300}") from exc
        if not text:
            raise LLMError(f"{self.base_url} returned an empty completion")
        return text


def get_client(env: Mapping[str, str] | None = None) -> LLMClient:
    """Pick a provider from environment variables (see module docstring)."""
    env = os.environ if env is None else env
    choice = env.get("JOBPIPE_PROVIDER", "").strip().lower()
    if choice in ("dry-run", "dryrun", "mock"):
        return DryRunClient()
    if choice == "anthropic" or (not choice and env.get("ANTHROPIC_API_KEY")):
        key = env.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMError("JOBPIPE_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        return AnthropicClient(api_key=key,
                               model=env.get("ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL)
    if choice in ("openai", "openai-compatible", "ollama") or (
        not choice and (env.get("OPENAI_BASE_URL") or env.get("OPENAI_API_KEY"))
    ):
        model = env.get("MODEL", "").strip()
        if not model:
            raise LLMError("Set MODEL for the OpenAI-compatible provider (e.g. MODEL=llama3.1)")
        return OpenAICompatibleClient(
            base_url=env.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL,
            model=model,
            api_key=env.get("OPENAI_API_KEY", ""),
        )
    if choice:
        raise LLMError(f"Unknown JOBPIPE_PROVIDER '{choice}' (use anthropic, openai or dry-run)")
    return DryRunClient()


def provider_is_implicit(env: Mapping[str, str] | None = None) -> bool:
    """True when no provider was configured and dry-run was chosen by default."""
    env = os.environ if env is None else env
    return not any(env.get(k) for k in ("JOBPIPE_PROVIDER", "ANTHROPIC_API_KEY",
                                        "OPENAI_BASE_URL", "OPENAI_API_KEY"))


# --- prompts ------------------------------------------------------------------------------

def render_prompt(prompt_name: str, /, **values: str) -> str:
    """Fill prompts/<name>.txt. Missing values raise KeyError so prompts never ship with
    unfilled slots. ``work_auth`` is always available."""
    text = (resource_dir("prompts") / f"{prompt_name}.txt").read_text(encoding="utf-8")
    return Template(text).substitute({"work_auth": WORK_AUTH_STATEMENT, **values})
