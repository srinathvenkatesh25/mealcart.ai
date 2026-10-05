from types import SimpleNamespace

import httpx
import openai

import pytest
from pydantic import BaseModel, ValidationError

from app import db
from app.planner.llm import LLM, LLMOutputError, LLMQuotaError


class Pick(BaseModel):
    title: str
    grams: int


def response(parsed):
    message = SimpleNamespace(parsed=parsed, content=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],
                           usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.messages_seen = []
        self.models_seen = []

    async def parse(self, *, model, messages, response_format, temperature, max_tokens=None):
        self.messages_seen.append(messages)
        self.models_seen.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return response(outcome)


def fake_llm(outcomes, fallbacks=()) -> tuple[LLM, FakeCompletions]:
    completions = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLM(client=client, model="test-model", fallbacks=list(fallbacks)), completions


def quota_error() -> openai.RateLimitError:
    resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    return openai.RateLimitError("Quota exceeded. Please retry in 20h11m11.5s.", response=resp, body=None)


def overloaded() -> openai.InternalServerError:
    resp = httpx.Response(503, request=httpx.Request("POST", "https://x"))
    return openai.InternalServerError("high demand", response=resp, body=None)


def bad_output() -> ValidationError:
    try:
        Pick.model_validate({"title": "x"})
    except ValidationError as e:
        return e


@pytest.fixture(autouse=True)
async def _db():
    await db.init_db()


async def test_returns_parsed_and_logs_call():
    run_id = await db.create_run("user_default", {})
    llm, _ = fake_llm([Pick(title="Bhurji", grams=150)])
    out = await llm.structured(Pick, "sys", "user", purpose="plan", run_id=run_id)
    assert out.title == "Bhurji"
    assert (await db.get_run(run_id))["llm_cost_usd"] == 0  # free tier


async def test_retries_once_with_error_feedback():
    llm, completions = fake_llm([bad_output(), Pick(title="Dal", grams=80)])
    out = await llm.structured(Pick, "sys", "user", purpose="plan")
    assert out.title == "Dal"
    assert "not valid for the schema" in completions.messages_seen[1][-1]["content"]


async def test_gives_up_after_second_bad_reply():
    llm, _ = fake_llm([bad_output(), None])
    with pytest.raises(LLMOutputError):
        await llm.structured(Pick, "sys", "user", purpose="plan")


async def test_falls_back_to_next_model_and_stays_there():
    llm, completions = fake_llm([quota_error(), overloaded(), Pick(title="a", grams=1), Pick(title="b", grams=2)],
                                fallbacks=["model-2", "model-3"])
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "a"
    assert (await llm.structured(Pick, "s", "u", purpose="pick")).title == "b"
    assert completions.models_seen == ["test-model", "model-2", "model-3", "model-3"]


async def test_all_models_unavailable_explains_each():
    llm, _ = fake_llm([quota_error(), overloaded()], fallbacks=["model-2"])
    with pytest.raises(LLMQuotaError) as e:
        await llm.structured(Pick, "s", "u", purpose="plan")
    assert "test-model: quota used up, resets in 20h11m11s" in str(e.value)
    assert "model-2: overloaded" in str(e.value)


def test_targets_mix_providers_and_skip_missing_keys():
    from app.config import Settings
    from app.planner.llm import build_targets
    s = Settings(llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai/", llm_api_key="g",
                 llm_model="gemini-3.8-flash", groq_api_key="k", openrouter_api_key="",
                 llm_fallbacks="gemini-3.7-flash, groq:openai/gpt-oss-120b, openrouter:x/y:free, ollama:qwen3:8b")
    targets = build_targets(s)
    assert [t.label for t in targets] == ["gemini-3.8-flash", "gemini-3.7-flash", "groq:openai/gpt-oss-120b",
                                          "ollama:qwen3:8b"]  # openrouter skipped: no key
    assert [t.model for t in targets] == ["gemini-3.8-flash", "gemini-3.7-flash", "openai/gpt-oss-120b", "qwen3:8b"]
    assert str(targets[2].client.base_url).startswith("https://api.groq.com")
    assert targets[1].client is targets[0].client


def too_large() -> openai.APIStatusError:
    resp = httpx.Response(413, request=httpx.Request("POST", "https://x"))
    return openai.APIStatusError("Request too large for model on tokens per minute (TPM): "
                                 "Limit 8000, Requested 17428", response=resp, body=None)


def short_limit(seconds="7.2") -> openai.RateLimitError:
    resp = httpx.Response(429, request=httpx.Request("POST", "https://x"))
    return openai.RateLimitError(f"Rate limit reached on tokens per minute. Please try again in {seconds}s.",
                                 response=resp, body=None)


async def test_request_too_large_is_its_own_error_and_does_not_switch_model():
    from app.planner.llm import LLMTooLargeError
    llm, completions = fake_llm([too_large()], fallbacks=["model-2"])
    with pytest.raises(LLMTooLargeError, match="17428"):
        await llm.structured(Pick, "s", "u", purpose="repair")
    assert completions.models_seen == ["test-model"]  # not retried elsewhere


async def test_short_rate_limit_is_waited_out_on_the_same_model():
    llm, completions = fake_llm([short_limit("7.2"), Pick(title="ok", grams=1)], fallbacks=["model-2"])
    slept, said = [], []

    async def fake_sleep(s):
        slept.append(s)

    async def say(m):
        said.append(m)

    llm._sleep, llm.on_event = fake_sleep, say
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "ok"
    assert completions.models_seen == ["test-model", "test-model"]
    assert slept == [pytest.approx(8.2)]
    assert "rate-limited; waiting 8s" in said[0]


async def test_long_rate_limit_switches_model_and_says_so():
    llm, completions = fake_llm([quota_error(), Pick(title="ok", grams=1)], fallbacks=["model-2"])
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    await llm.structured(Pick, "s", "u", purpose="plan")
    assert completions.models_seen == ["test-model", "model-2"]
    assert said == ["test-model: quota used up, resets in 20h11m11s. Switching to model-2"]


async def test_retry_after_header_wins_over_message():
    from app.planner.llm import retry_after_seconds
    resp = httpx.Response(429, headers={"retry-after": "12"}, request=httpx.Request("POST", "https://x"))
    e = openai.RateLimitError("try again in 99s", response=resp, body=None)
    assert retry_after_seconds(e) == 12
    assert retry_after_seconds(quota_error()) == pytest.approx(72671.5)


def refused(cls, status):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://x"))
    return cls("This model is not available in your subscription tier", response=resp, body=None)


@pytest.mark.parametrize("cls, status, why", [
    (openai.PermissionDeniedError, 403, "not available on your account or plan"),
    (openai.NotFoundError, 404, "not available on your account or plan"),
    (openai.AuthenticationError, 401, "API key rejected"),
])
async def test_a_model_your_account_cannot_use_is_skipped_not_fatal(cls, status, why):
    llm, completions = fake_llm([refused(cls, status), Pick(title="ok", grams=1)], fallbacks=["model-2"])
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "ok"
    assert completions.models_seen == ["test-model", "model-2"]
    assert said == [f"test-model: {why}. Switching to model-2"]


async def test_per_run_call_budget_stops_runaway_loops():
    from app.config import get_settings
    llm, completions = fake_llm([Pick(title=str(i), grams=1) for i in range(5)])
    llm.max_calls = 3
    for _ in range(3):
        await llm.structured(Pick, "s", "u", purpose="plan")
    with pytest.raises(LLMQuotaError, match="limit of 3 AI calls"):
        await llm.structured(Pick, "s", "u", purpose="repair")
    assert len(completions.models_seen) == 3          # the fourth never reached the provider
    assert get_settings().llm_max_calls_per_run == 40
