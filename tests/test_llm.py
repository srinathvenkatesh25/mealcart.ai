import asyncio
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


class FakeStream:
    """What client.chat.completions.stream(...) returns: a context manager that yields chunks."""

    def __init__(self, final, delays=(), start_delay=0.0):
        self.final, self.delays, self.start_delay = final, delays, start_delay

    async def __aenter__(self):
        if self.start_delay:
            await asyncio.sleep(self.start_delay)      # the request is sent here; nothing comes back yet
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._chunks()

    async def _chunks(self):
        for delay in self.delays:
            await asyncio.sleep(delay)
            yield "chunk"

    async def get_final_completion(self):
        return self.final


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.messages_seen = []
        self.models_seen = []
        self.timeouts_seen = []
        self.chunk_delays = ()      # seconds before each chunk of the next answer

    def stream(self, *, model, messages, response_format, temperature, max_tokens=None, timeout=None,
               stream_options=None):
        self.messages_seen.append(messages)
        self.timeouts_seen.append(timeout)
        self.models_seen.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(response(outcome), self.chunk_delays)


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
    from app.planner import llm as llm_module
    llm_module._cooldown.clear()          # a model that "failed" in one test must not be skipped in the next
    await db.init_db()


async def test_returns_parsed_and_logs_call():
    run_id = await db.create_run("user_default", {})
    llm, _ = fake_llm([Pick(title="Bhurji", grams=150)])
    out = await llm.structured(Pick, "sys", "user", purpose="plan", run_id=run_id)
    assert out.title == "Bhurji"
    assert (await db.get_run(run_id))["llm_cost_usd"] == 0  # free tier
    model = (await db.llm_usage(run_id))["models"][0]
    assert model["model"] == "test-model" and model["avg_latency_ms"] is not None and model["avg_latency_ms"] >= 0


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


async def test_small_tasks_try_groq_first_and_planning_keeps_its_own_order():
    llm, completions = fake_llm([Pick(title="p", grams=1), Pick(title="s", grams=1), Pick(title="k", grams=1)],
                                fallbacks=["gemini-b", "groq:fast"])
    await llm.structured(Pick, "s", "u", purpose="plan")
    await llm.structured(Pick, "s", "u", purpose="store")
    await llm.structured(Pick, "s", "u", purpose="pick")
    assert completions.models_seen == ["test-model", "groq:fast", "groq:fast"]


async def test_small_task_too_large_for_groq_moves_on_instead_of_failing():
    resp = httpx.Response(413, request=httpx.Request("POST", "https://x"))
    too_big = openai.APIStatusError("Request too large", response=resp, body=None)
    llm, completions = fake_llm([too_big, Pick(title="ok", grams=1)], fallbacks=["groq:fast"])
    assert (await llm.structured(Pick, "s", "u", purpose="pick")).title == "ok"
    assert completions.models_seen == ["groq:fast", "test-model"]


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


async def test_a_preferred_model_goes_first_for_everything(monkeypatch):
    from app.config import Settings
    monkeypatch.setattr("app.planner.llm.get_settings",
                        lambda: Settings(_env_file=None, llm_preferred="groq:fast", llm_short_tasks_first="groq:"))
    llm, completions = fake_llm([Pick(title="p", grams=1), Pick(title="s", grams=1)],
                                fallbacks=["model-2", "groq:fast"])
    await llm.structured(Pick, "s", "u", purpose="plan")
    await llm.structured(Pick, "s", "u", purpose="pick")
    assert completions.models_seen == ["groq:fast", "groq:fast"]            # planning and picks both start there
    assert [llm.targets[i].label for i in llm._order["long"]] == ["groq:fast", "test-model", "model-2"]


async def test_a_paid_provider_with_no_credit_is_skipped_not_fatal():
    resp = httpx.Response(402, request=httpx.Request("POST", "https://x"))
    no_credit = openai.APIStatusError("Insufficient Balance", response=resp, body=None)
    llm, completions = fake_llm([no_credit, Pick(title="ok", grams=1)], fallbacks=["model-2"])
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "ok"
    assert completions.models_seen == ["test-model", "model-2"]
    assert said == ["test-model: out of credit. Switching to model-2"]


def test_the_deepseek_key_can_be_named_ds_api_key(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("DS_API_KEY", "short-name")
    assert Settings(_env_file=None).deepseek_api_key == "short-name"
    monkeypatch.delenv("DS_API_KEY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "long-name")
    assert Settings(_env_file=None).deepseek_api_key == "long-name"


def test_cerebras_and_deepseek_are_providers_and_cerebras_gets_a_reply_cap():
    from app.config import Settings
    from app.planner.llm import build_targets
    s = Settings(_env_file=None, llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai/", llm_api_key="g",
                 llm_model="gemini-3.8-flash", cerebras_api_key="c", deepseek_api_key="d", groq_api_key="",
                 llm_fallbacks="cerebras:gpt-oss-120b, deepseek:deepseek-chat, groq:openai/gpt-oss-120b")
    targets = build_targets(s)
    assert [t.label for t in targets] == ["gemini-3.8-flash", "cerebras:gpt-oss-120b", "deepseek:deepseek-chat"]  # no groq key
    assert str(targets[1].client.base_url).startswith("https://api.cerebras.ai")
    assert str(targets[2].client.base_url).startswith("https://api.deepseek.com")
    assert [t.max_tokens for t in targets] == [None, 7000, None]


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


async def test_model_that_never_answers_times_out_and_switches():
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://x"))
    llm, completions = fake_llm([timeout, Pick(title="ok", grams=1)], fallbacks=["model-2"])
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    out = await llm.structured(Pick, "s", "u", purpose="plan")
    assert out.title == "ok"
    assert completions.models_seen == ["test-model", "model-2"]
    assert said == ["test-model: no answer in time. Switching to model-2"]


async def test_short_tasks_get_the_short_timeout():
    llm, completions = fake_llm([Pick(title="a", grams=1), Pick(title="b", grams=1)])
    await llm.structured(Pick, "s", "u", purpose="plan")
    await llm.structured(Pick, "s", "u", purpose="store")
    assert completions.timeouts_seen == [300, 90]


class PerModel(FakeCompletions):
    """Each model gets its own behaviour: {model: (start_delay, outcome_or_exception)}."""

    def __init__(self, behaviour):
        super().__init__([])
        self.behaviour = behaviour
        self.cancelled = []

    def stream(self, *, model, **kw):
        self.models_seen.append(model)
        delay, outcome = self.behaviour[model]
        outer = self

        class S(FakeStream):
            async def __aenter__(self):
                try:
                    return await super().__aenter__()
                except asyncio.CancelledError:
                    outer.cancelled.append(model)
                    raise

        if isinstance(outcome, Exception):
            class Boom(S):
                async def __aenter__(self):
                    await asyncio.sleep(delay)
                    raise outcome
            return Boom(None)
        return S(response(outcome), start_delay=delay)


def hedged_llm(behaviour, fallbacks):
    completions = PerModel(behaviour)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions), base_url="https://x")
    llm = LLM(client=client, model="test-model", fallbacks=fallbacks)
    llm.hedge_after_s = 0.05
    llm.timeout_long = 2
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    return llm, completions, said


async def test_a_silent_model_is_beaten_by_the_next_one_and_cancelled():
    llm, completions, said = hedged_llm(
        {"test-model": (30, Pick(title="never", grams=1)), "model-2": (0.01, Pick(title="hedge", grams=1))},
        ["model-2"])
    started = asyncio.get_event_loop().time()
    out = await llm.structured(Pick, "s", "u", purpose="plan")
    assert out.title == "hedge"
    assert asyncio.get_event_loop().time() - started < 1           # not the 30 s the dead model would take
    assert completions.models_seen == ["test-model", "model-2"]
    assert completions.cancelled == ["test-model"]                  # the dead request was dropped
    assert said == ["test-model is slow to answer; also asking model-2 and using whichever answers first"]
    assert llm.model == "model-2"                                   # stays on the one that answered


async def test_a_slow_but_working_model_still_wins_if_it_answers_first():
    llm, completions, said = hedged_llm(
        {"test-model": (0.12, Pick(title="slow primary", grams=1)), "model-2": (0.5, Pick(title="hedge", grams=1))},
        ["model-2"])
    out = await llm.structured(Pick, "s", "u", purpose="plan")
    assert out.title == "slow primary"
    assert completions.models_seen == ["test-model", "model-2"]
    assert completions.cancelled == ["model-2"]
    assert llm.model == "test-model"                                # the primary wasn't penalised


async def test_a_fast_model_is_never_hedged():
    llm, completions, said = hedged_llm({"test-model": (0.0, Pick(title="quick", grams=1)),
                                         "model-2": (0.0, Pick(title="x", grams=1))}, ["model-2"])
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "quick"
    assert completions.models_seen == ["test-model"] and said == []


async def test_if_the_hedged_model_errors_the_slow_primary_can_still_answer():
    llm, completions, _ = hedged_llm(
        {"test-model": (0.15, Pick(title="primary", grams=1)), "model-2": (0.01, overloaded())}, ["model-2"])
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "primary"


async def test_silent_primary_then_overloaded_hedge_still_reaches_a_working_model():
    # The incident: the first model never answers, the model it is hedged to says "overloaded" at once, and
    # the first model finally times out. The run must move on to the next model, not stall or retry the dead one.
    llm, completions, said = hedged_llm(
        {"test-model": (30, Pick(title="never", grams=1)),
         "model-2": (0.0, overloaded()),
         "model-3": (0.01, Pick(title="third", grams=1))},
        ["model-2", "model-3"])
    llm.timeout_long = 0.4
    out = await asyncio.wait_for(llm.structured(Pick, "s", "u", purpose="plan"), 6)
    assert out.title == "third"
    assert completions.models_seen.count("model-2") == 1          # the overloaded model is not asked twice
    assert said[0] == "test-model is slow to answer; also asking model-2 and using whichever answers first"


def provider_json_failure() -> openai.APIError:
    return openai.APIError("Failed to generate JSON. Please adjust your prompt.",
                           request=httpx.Request("POST", "https://x"), body=None)


async def test_a_provider_that_cannot_produce_valid_json_is_retried_then_skipped_not_fatal():
    # Groq reported "Failed to generate JSON" for a week plan; the run used to stop with an APIError.
    llm, completions = fake_llm([provider_json_failure(), provider_json_failure(), Pick(title="ok", grams=1)],
                                fallbacks=["model-2"])
    said = []

    async def say(m):
        said.append(m)

    llm.on_event = say
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "ok"
    assert completions.models_seen == ["test-model", "test-model", "model-2"]           # one retry, then the next model
    assert said == ["test-model: didn't return usable output. Switching to model-2"]
    assert "not valid for the schema" in completions.messages_seen[1][-1]["content"]    # the retry says what went wrong


async def test_unusable_output_from_the_only_model_still_raises_the_output_error():
    from app.planner.llm import LLMOutputError
    llm, _ = fake_llm([provider_json_failure(), provider_json_failure()])
    with pytest.raises(LLMOutputError):
        await llm.structured(Pick, "s", "u", purpose="plan")


async def test_two_silent_models_are_both_hedged_so_a_third_answers_within_seconds():
    llm, completions, said = hedged_llm(
        {"test-model": (30, Pick(title="never", grams=1)), "model-2": (30, Pick(title="never", grams=1)),
         "model-3": (0.01, Pick(title="third", grams=1))}, ["model-2", "model-3"])
    started = asyncio.get_event_loop().time()
    out = await llm.structured(Pick, "s", "u", purpose="plan")
    assert out.title == "third"
    assert asyncio.get_event_loop().time() - started < 2            # not 2 x the timeout, one after the other
    assert completions.models_seen == ["test-model", "model-2", "model-3"]
    assert said[1] == ("test-model, model-2 is slow to answer; also asking model-3 and using whichever answers first")


async def test_a_model_that_failed_is_tried_last_by_the_next_run_for_a_while():
    llm, completions = fake_llm([overloaded(), Pick(title="ok", grams=1)], fallbacks=["model-2"])
    await llm.structured(Pick, "s", "u", purpose="plan")
    assert completions.models_seen == ["test-model", "model-2"]

    later, completions2 = fake_llm([Pick(title="ok", grams=1)], fallbacks=["model-2"])      # the next run
    said = []

    async def say(m):
        said.append(m)

    later.on_event = say
    await later.structured(Pick, "s", "u", purpose="plan")
    assert completions2.models_seen == ["model-2"]                 # the model that just failed is not tried first
    assert said == ["Trying test-model last: it failed to answer recently"]


async def test_a_model_that_goes_silent_mid_answer_is_dropped():
    llm, completions = fake_llm([Pick(title="x", grams=1), Pick(title="ok", grams=1)], fallbacks=["model-2"])
    first = completions.stream

    def stream(**kw):
        s = first(**kw)
        if kw["model"] == "test-model":
            s.delays = (0.01, 5)        # one chunk, then silence
        return s

    completions.stream = stream
    llm.stall_s = 0.05
    llm.hedge_after_s = 30              # the stall rule, not the hedge, must catch this
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "ok"
    assert completions.models_seen == ["test-model", "model-2"]


async def test_a_slow_model_that_keeps_sending_is_not_dropped():
    llm, completions = fake_llm([Pick(title="slow but working", grams=1)])
    completions.chunk_delays = (0.04, 0.04, 0.04, 0.04)      # 0.16 s in total, never silent for long
    llm.stall_s = 0.1
    assert (await llm.structured(Pick, "s", "u", purpose="plan")).title == "slow but working"
    assert completions.models_seen == ["test-model"]


def test_clients_have_a_timeout():
    from app.planner.llm import _client
    client = _client("https://x", "k")
    assert client.timeout == 300
    assert client.max_retries == 0


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
