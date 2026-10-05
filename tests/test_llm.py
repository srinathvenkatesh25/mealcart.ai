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
