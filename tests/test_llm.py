import pytest

from jobpipe import llm
from jobpipe.llm import (
    AnthropicClient,
    DryRunClient,
    LLMError,
    LLMRequest,
    OpenAICompatibleClient,
    get_client,
    provider_is_implicit,
    render_prompt,
)
from jobpipe.validators import WORK_AUTH_STATEMENT

REQ = LLMRequest(task="summary", system="SYS", user="USER", fallback="FALLBACK")


def test_dry_run_is_deterministic_and_scriptable():
    client = DryRunClient()
    assert client.complete(REQ) == client.complete(REQ) == "FALLBACK"
    scripted = DryRunClient(responses=["first", "second"])
    assert [scripted.complete(REQ) for _ in range(3)] == ["first", "second", "FALLBACK"]
    assert len(scripted.calls) == 3


@pytest.mark.parametrize("env, expected", [
    ({}, DryRunClient),
    ({"JOBPIPE_PROVIDER": "dry-run", "ANTHROPIC_API_KEY": "k"}, DryRunClient),
    ({"ANTHROPIC_API_KEY": "k"}, AnthropicClient),
    ({"ANTHROPIC_API_KEY": "k", "OPENAI_BASE_URL": "http://x/v1", "MODEL": "m"},
     AnthropicClient),
    ({"OPENAI_BASE_URL": "http://localhost:11434/v1", "MODEL": "llama3.1"},
     OpenAICompatibleClient),
    ({"OPENAI_API_KEY": "sk", "MODEL": "gpt-4o-mini"}, OpenAICompatibleClient),
    ({"JOBPIPE_PROVIDER": "ollama", "OPENAI_BASE_URL": "http://localhost:11434/v1",
      "MODEL": "llama3.1"}, OpenAICompatibleClient),
])
def test_provider_selection(env, expected):
    assert isinstance(get_client(env), expected)


def test_provider_configuration_details():
    anthropic = get_client({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "claude-haiku-5-5"})
    assert anthropic.model == "claude-haiku-5-5"
    assert get_client({"ANTHROPIC_API_KEY": "k"}).model == llm.DEFAULT_ANTHROPIC_MODEL
    ollama = get_client({"OPENAI_BASE_URL": "http://localhost:11434/v1", "MODEL": "llama3.1"})
    assert (ollama.base_url, ollama.model, ollama.api_key) == (
        "http://localhost:11434/v1", "llama3.1", "")
    openai = get_client({"OPENAI_API_KEY": "sk", "MODEL": "gpt-4o-mini"})
    assert openai.base_url == llm.DEFAULT_OPENAI_BASE_URL


@pytest.mark.parametrize("env, message", [
    ({"JOBPIPE_PROVIDER": "anthropic"}, "ANTHROPIC_API_KEY"),
    ({"OPENAI_BASE_URL": "http://localhost:11434/v1"}, "MODEL"),
    ({"JOBPIPE_PROVIDER": "gemini"}, "Unknown JOBPIPE_PROVIDER"),
])
def test_provider_misconfiguration(env, message):
    with pytest.raises(LLMError, match=message):
        get_client(env)


def test_implicit_dry_run_detection():
    assert provider_is_implicit({}) is True
    assert provider_is_implicit({"JOBPIPE_PROVIDER": "dry-run"}) is False


def test_anthropic_request_shape(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        return {"content": [{"type": "text", "text": " hello "}], "stop_reason": "end_turn"}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    assert AnthropicClient(api_key="key", model="m").complete(REQ) == "hello"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "key"
    assert seen["headers"]["anthropic-version"]
    assert seen["body"]["system"] == "SYS"
    assert seen["body"]["messages"] == [{"role": "user", "content": "USER"}]


def test_openai_compatible_request_shape(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=headers)
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(llm, "_post_json", fake_post)
    client = OpenAICompatibleClient(base_url="http://localhost:11434/v1/", model="llama3.1")
    assert client.complete(REQ) == "hi"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["headers"] == {}  # Ollama needs no key
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}


def test_empty_or_malformed_responses_raise(monkeypatch):
    monkeypatch.setattr(llm, "_post_json", lambda *a: {"content": []})
    with pytest.raises(LLMError):
        AnthropicClient(api_key="k").complete(REQ)
    monkeypatch.setattr(llm, "_post_json", lambda *a: {"error": "nope"})
    with pytest.raises(LLMError):
        OpenAICompatibleClient(base_url="http://x", model="m").complete(REQ)


def test_network_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="Network access"):
        AnthropicClient(api_key="k").complete(REQ)


def test_prompts_carry_the_hard_rules():
    system = render_prompt("system")
    assert WORK_AUTH_STATEMENT in system
    assert "I am writing to apply for" in system
    assert "never add" in system.lower()
    letter = render_prompt("cover_letter", name="N", role="R", company="C", facts="F",
                           posting="P", keywords="K", evidence="E", work_auth_instruction="W")
    assert "NEVER open with" in letter and "250-350 words" in letter


def test_prompt_with_missing_value_fails_loudly():
    with pytest.raises(KeyError):
        render_prompt("summary", role="R")
