"""One structured-output call path for every LLM use in the app.

Works with any OpenAI-compatible endpoint (Gemini free tier, local Ollama,
Groq, OpenRouter...), so switching provider is a .env change.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from dataclasses import dataclass
from typing import TypeVar

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app import db
from app.config import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# A rate limit that clears within this long (e.g. a per-minute token cap) is waited out
# on the same model; a longer one (a used-up daily quota) moves on to the next model.
MAX_SHORT_WAIT_S = 75
MAX_SHORT_WAITS = 3

# USD per 1M tokens (input, output). Free tiers are 0; add a row if you switch
# to a paid model so runs.llm_cost_usd stays meaningful.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {}


class LLMOutputError(Exception):
    """The model failed to return valid structured output after a retry."""


class LLMQuotaError(Exception):
    """The provider's rate limit or free-tier quota is used up."""


class LLMTooLargeError(Exception):
    """One request is bigger than the model accepts (e.g. Groq's free tier caps a request at 8,000 tokens).

    Retrying the same request can't help; the caller must send less (see planner.repair).
    """


_RETRY_IN = re.compile(r"(?:try again|retry) in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", re.I)


def retry_after_seconds(e: openai.APIStatusError) -> float | None:
    """How long the provider says to wait: the Retry-After header, else its message text."""
    header = e.response.headers.get("retry-after") if e.response is not None else None
    try:
        if header:
            return float(header)
    except ValueError:
        pass
    m = _RETRY_IN.search(str(e))
    if not m:
        return None
    h, mi, sec = m.groups()
    return int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec)


def _fmt_wait(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return "".join([f"{h}h" if h else "", f"{m}m" if m or h else "", f"{sec}s"])


# Errors that mean "this model isn't usable for this account", not "this request is bad":
# a wrong plan tier, an unknown model name, a rejected key. Move on to the next model.
UNUSABLE = (openai.PermissionDeniedError, openai.NotFoundError, openai.AuthenticationError)


def _out_of_credit(e: Exception) -> bool:
    """402 Payment Required: a paid provider (e.g. DeepSeek) with no balance left."""
    return isinstance(e, openai.APIStatusError) and e.status_code == 402


def _why(e: Exception) -> str:
    if isinstance(e, openai.APITimeoutError):
        return "no answer in time"
    if isinstance(e, openai.APIConnectionError):
        return "couldn't connect"
    if isinstance(e, openai.InternalServerError):
        return "overloaded"
    if isinstance(e, LLMOutputError):
        return "didn't return usable output"
    if _out_of_credit(e):
        return "out of credit"
    if isinstance(e, openai.AuthenticationError):
        return "API key rejected"
    if isinstance(e, UNUSABLE):
        return "not available on your account or plan"
    wait = retry_after_seconds(e) if isinstance(e, openai.APIStatusError) else None
    return "quota used up" + (f", resets in {_fmt_wait(wait)}" if wait is not None else "")


PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini_api_key"),
    "groq": ("https://api.groq.com/openai/v1", "groq_api_key"),
    "cerebras": ("https://api.cerebras.ai/v1", "cerebras_api_key"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek_api_key"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter_api_key"),
    "ollama": ("http://localhost:11434/v1", None),
}

# Providers whose free tier limits the reply size (a context window shared by prompt and reply). A week is
# about 4-6k tokens, so these stay comfortably above it while keeping the request inside the window.
MAX_TOKENS_BY_PROVIDER = {"cerebras": 7000}


@dataclass
class Target:
    label: str
    client: AsyncOpenAI
    model: str
    json_schema_supported: bool = True
    max_tokens: int | None = None      # provider-specific cap on the reply, if its free tier needs one


def _client(base_url: str, api_key: str) -> AsyncOpenAI:
    # No library retries: rate limits are waited out or switched in LLM.structured, and an overloaded
    # or silent model moves to the next target. Without an explicit timeout the library waits
    # 10 minutes per attempt (×3 with retries), which could leave a run on "Planning…" for 30 minutes.
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "none", max_retries=0,
                       timeout=get_settings().llm_timeout_s)


def build_targets(s=None) -> list[Target]:
    """Primary model from LLM_BASE_URL/LLM_API_KEY/LLM_MODEL, then LLM_FALLBACKS in order."""
    s = s or get_settings()
    primary = _client(s.llm_base_url, s.llm_api_key)
    targets = [Target(s.llm_model, primary, s.llm_model)]
    for entry in (e.strip() for e in s.llm_fallbacks.split(",")):
        provider, _, model = entry.partition(":")
        if not entry or entry == s.llm_model:
            continue
        if provider in PROVIDERS and model:
            base_url, key_field = PROVIDERS[provider]
            key = getattr(s, key_field) if key_field else "ollama"
            if base_url == s.llm_base_url and not key:
                key = s.llm_api_key  # e.g. gemini:... when Gemini is already the primary
            if key_field and not key:
                continue  # no key configured for that provider: skip it
            targets.append(Target(entry, _client(base_url, key), model, max_tokens=MAX_TOKENS_BY_PROVIDER.get(provider)))
        else:  # bare model name (may itself contain ':' e.g. qwen3:8b) on the primary provider
            targets.append(Target(entry, primary, entry))
    return targets


# Models that just failed (no answer, overloaded, out of quota) are tried last for a while, across runs, so a
# dead model isn't the first thing every new plan waits on. In memory: it resets when the server restarts.
COOLDOWN_S = 600
MAX_PARALLEL = 3          # most models asked about the same request at once
_cooldown: dict[str, float] = {}


def _cool(label: str) -> None:
    _cooldown[label] = time.monotonic() + COOLDOWN_S


def _is_cool(label: str) -> bool:
    return _cooldown.get(label, 0.0) > time.monotonic()


# Calls that write a whole week (or part of one) are "long": the main model order and a long timeout.
# Everything else (store choice, product picks, intake) is "short": small replies, a short timeout,
# and its own model order with LLM_SHORT_TASKS_FIRST in front (Groq answers small requests in seconds).
LONG_PURPOSES = {"plan", "repair", "swap"}


def _lane(purpose: str) -> str:
    return "long" if purpose in LONG_PURPOSES else "short"


class LLM:
    def __init__(self, client: AsyncOpenAI | None = None, model: str | None = None,
                 fallbacks: list[str] | None = None):
        s = get_settings()
        if client is not None:  # tests: one client, fallbacks are model names on it
            names = [model or s.llm_model] + [m for m in (fallbacks or []) if m != model]
            self.targets = [Target(n, client, n) for n in names]
        else:
            self.targets = build_targets(s)
        self.max_tokens = s.llm_max_tokens
        self.timeout_long = s.llm_timeout_s
        self.timeout_short = s.llm_short_timeout_s
        self.hedge_after_s = s.llm_hedge_after_s
        self.hedge_after_short_s = s.llm_hedge_after_short_s
        self.stall_s = s.llm_stall_s
        self.max_calls = s.llm_max_calls_per_run
        self.calls = 0
        # Each lane keeps its own order and its own position, so a slow Gemini on planning
        # doesn't decide which model picks products, and vice versa.
        everything = list(range(len(self.targets)))
        cooled = [i for i in everything if _is_cool(self.targets[i].label)]
        everything = [i for i in everything if i not in cooled] + cooled        # recently failed models go last
        self._skipped = [self.targets[i].label for i in cooled] if len(cooled) < len(everything) else []
        want = s.llm_preferred.strip()
        top = [i for i in everything if want and self.targets[i].label.startswith(want) and i not in cooled]
        everything = top + [i for i in everything if i not in top]              # the preferred model goes first
        first = s.llm_short_tasks_first.strip()
        short_first = [i for i in everything if first and self.targets[i].label.startswith(first)
                       and i not in cooled and i not in top]
        self._order = {"long": everything,
                       "short": top + short_first + [i for i in everything if i not in top + short_first]}
        self._pos = {"long": 0, "short": 0}
        self._lane = "long"
        self._failed: set[int] = set()  # models that failed this run (quota, hung, refused): both lanes skip them
        # Told about model switches and waits, so the page can explain a slow or odd run.
        self.on_event: Callable[[str], Awaitable[None]] | None = None
        self._sleep = asyncio.sleep

    @property
    def _current(self) -> int:
        order, pos = self._order[self._lane], self._pos[self._lane]
        while pos + 1 < len(order) and order[pos] in self._failed:
            pos += 1
        self._pos[self._lane] = pos
        return order[pos]

    @property
    def model(self) -> str:
        return self.targets[self._current].model

    @property
    def client(self) -> AsyncOpenAI:
        return self.targets[self._current].client

    async def structured(
        self,
        schema: type[T],
        system: str,
        user: str,
        *,
        purpose: str,
        run_id: str | None = None,
        temperature: float = 0.3,
    ) -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self._lane = _lane(purpose)
        if self._skipped:
            await self._say(f"Trying {', '.join(self._skipped)} last: it failed to answer recently")
            self._skipped = []
        unavailable: list[str] = []
        waits = 0
        while True:
            self.calls += 1
            if self.calls > self.max_calls:
                raise LLMQuotaError(f"This run reached its limit of {self.max_calls} AI calls, which protects "
                                    "your free quota. Start a new plan, or raise LLM_MAX_CALLS_PER_RUN in .env.")
            try:
                return await self._structured_once(schema, messages, purpose, run_id, temperature)
            except openai.RateLimitError as e:
                delay = retry_after_seconds(e)
                if delay is not None and delay <= MAX_SHORT_WAIT_S and waits < MAX_SHORT_WAITS:
                    waits += 1
                    await self._say(f"{self.targets[self._current].label} is rate-limited; "
                                    f"waiting {_fmt_wait(delay + 1)} and trying again")
                    await self._sleep(delay + 1)
                    continue
                await self._switch(e, unavailable)
            except (openai.InternalServerError, openai.APIConnectionError, *UNUSABLE) as e:
                # APIConnectionError includes APITimeoutError: a model that doesn't answer in time.
                await self._switch(e, unavailable)
            except LLMOutputError as e:
                # This model can't produce usable output for this request: try the next one, if there is one.
                if not any(i not in self._failed for i in self._order[self._lane][self._pos[self._lane] + 1:]):
                    raise
                await self._switch(e, unavailable)
            except openai.APIStatusError as e:
                if _out_of_credit(e):
                    await self._switch(e, unavailable)
                    continue
                if e.status_code == 413 and self._lane == "short":
                    await self._switch(e, unavailable)  # a small task can simply use a roomier model
                    continue
                if e.status_code == 413:
                    raise LLMTooLargeError(
                        f"{self.targets[self._current].label} won't accept a request this large: {str(e)[:200]}"
                    ) from e
                raise

    async def _say(self, message: str) -> None:
        log.warning(message)
        if self.on_event:
            await self.on_event(message)

    async def _switch(self, e: Exception, unavailable: list[str]) -> None:
        """Move to the next model, or raise LLMQuotaError when none is left."""
        failed = self._current
        label = self.targets[failed].label
        unavailable.append(f"{label}: {_why(e)}")
        self._failed.add(failed)
        if not (isinstance(e, openai.APIStatusError) and e.status_code == 413):
            _cool(label)
        order = self._order[self._lane]
        if all(i in self._failed for i in order[self._pos[self._lane]:]):
            raise LLMQuotaError("no model available — " + "; ".join(unavailable)
                                + ". Wait, or add entries to LLM_FALLBACKS in .env.") from e
        self._pos[self._lane] += 1  # stays on the working target for the rest of the run
        await self._say(f"{label}: {_why(e)}. Switching to {self.targets[self._current].label}")

    async def _structured_once(self, schema: type[T], messages: list[dict], purpose: str,
                               run_id: str | None, temperature: float) -> T:
        for attempt in range(2):
            try:
                return await self._call(schema, messages, purpose, run_id, temperature)
            except (ValidationError, json.JSONDecodeError, openai.LengthFinishReasonError, LLMOutputError,
                    openai.APIError) as e:
                if isinstance(e, (openai.APIStatusError, openai.APIConnectionError)):
                    raise                       # rate limits, overload, timeouts: handled by the caller
                # What's left is the provider reporting that the reply wasn't valid JSON (e.g. Groq's
                # "Failed to generate JSON") or a reply that doesn't fit the schema: ask once more, then move on.
                if attempt == 1:
                    raise LLMOutputError(f"{purpose}: invalid structured output after retry: {e}") from e
                messages = messages + [{
                    "role": "user",
                    "content": f"Your previous reply was not valid for the schema: {str(e)[:500]}. "
                               "Reply again with only valid JSON matching the schema.",
                }]
        raise AssertionError("unreachable")

    def _timeout_error(self, idx: int) -> openai.APITimeoutError:
        base = str(getattr(self.targets[idx].client, "base_url", "https://ai"))
        return openai.APITimeoutError(request=httpx.Request("POST", base))

    @staticmethod
    def _switchable(e: Exception) -> bool:
        """Errors that mean "this model can't answer right now" (as opposed to a bad request or bad output)."""
        return (isinstance(e, (openai.RateLimitError, openai.InternalServerError, openai.APIConnectionError, *UNUSABLE))
                or _out_of_credit(e))

    async def _call(self, schema: type[T], messages: list[dict], purpose: str, run_id: str | None,
                    temperature: float) -> T:
        """One attempt, hedged and cascading: if the model is silent for hedge_after seconds, the next model
        is asked too; if that is also silent, the next one after another hedge_after, up to MAX_PARALLEL at
        once. Whichever answers first wins and the rest are cancelled. A slow model that is still working is
        not penalised, and dead ones cost seconds each instead of the full timeout in turn."""
        timeout = self.timeout_long if purpose in LONG_PURPOSES else self.timeout_short
        hedge_after = self.hedge_after_s if purpose in LONG_PURPOSES else self.hedge_after_short_s
        order = self._order[self._lane]
        first = self._current
        tasks: dict[asyncio.Task, int] = {}
        errors: dict[int, Exception] = {}

        def start(idx: int) -> asyncio.Task:
            t = asyncio.create_task(self._attempt(idx, schema, messages, purpose, run_id, temperature, timeout))
            tasks[t] = idx
            return t

        def next_idx() -> int | None:
            last = max(order.index(i) for i in tasks.values())
            return next((i for i in order[last + 1:] if i not in self._failed), None)

        def mark_failed(skip: int | None = None) -> None:
            for idx, err in errors.items():
                if idx != skip and self._switchable(err):
                    self._failed.add(idx)
                    _cool(self.targets[idx].label)

        try:
            pending = {start(first)}
            while pending:
                done, pending = await asyncio.wait(pending, timeout=hedge_after, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    idx = tasks[t]
                    try:
                        result = t.result()
                    except Exception as e:  # noqa: BLE001 - classified below
                        errors[idx] = e
                        continue
                    mark_failed()                              # hedged models that errored: skip them from now on
                    if idx != first:                           # a hedge won: stay on it for the rest of the run
                        self._pos[self._lane] = order.index(idx)
                    return result
                if not done and len(pending) < MAX_PARALLEL and (nxt := next_idx()) is not None:
                    waiting = ", ".join(self.targets[i].label for t, i in tasks.items() if t in pending)  # in asked order
                    await self._say(f"{waiting} is slow to answer; also asking {self.targets[nxt].label} "
                                    "and using whichever answers first")
                    pending.add(start(nxt))
            mark_failed(skip=first)                            # all failed: remember the hedged ones, then report
            raise errors[first] if first in errors else next(iter(errors.values()))
        finally:
            losers = [t for t in tasks if not t.done()]
            for t in losers:
                t.cancel()
            if losers:   # let the cancelled requests close their connections before moving on
                await asyncio.gather(*losers, return_exceptions=True)

    async def _attempt(self, idx: int, schema: type[T], messages: list[dict], purpose: str, run_id: str | None,
                       temperature: float, timeout: float) -> T:
        try:
            # A hard total deadline per model. The HTTP timeout alone only limits the gap between bytes,
            # so a service that keeps a connection open without answering could hold a run for minutes.
            async with asyncio.timeout(timeout):
                return await self._call_once(idx, schema, messages, purpose, run_id, temperature, timeout)
        except TimeoutError as e:
            raise self._timeout_error(idx) from e

    async def _watch(self, chunks, first_wait: float, idx: int) -> None:
        """Consume a stream, giving up if it goes silent mid-answer (stall_s between pieces)."""
        it = chunks.__aiter__()
        wait = first_wait
        while True:
            try:
                await asyncio.wait_for(it.__anext__(), wait)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise self._timeout_error(idx) from None
            wait = self.stall_s

    async def _call_once(self, idx: int, schema: type[T], messages: list[dict], purpose: str, run_id: str | None,
                         temperature: float, timeout: float) -> T:
        target = self.targets[idx]
        started = time.monotonic()
        usage_opts = {"stream_options": {"include_usage": True}}
        if target.json_schema_supported:
            try:
                async with target.client.chat.completions.stream(
                    model=target.model, messages=messages, response_format=schema, temperature=temperature,
                    max_tokens=target.max_tokens or self.max_tokens, timeout=timeout, **usage_opts,
                ) as stream:
                    await self._watch(stream, timeout, idx)
                    resp = await stream.get_final_completion()
            except openai.BadRequestError as e:
                if "response_format" not in str(e) and "schema" not in str(e):
                    raise
                target.json_schema_supported = False  # endpoint can't do json_schema; use json_object
            else:
                await self._log(resp, purpose, run_id, target.model, started)
                parsed = resp.choices[0].message.parsed
                if parsed is None:
                    raise LLMOutputError("empty or refused reply")
                return parsed

        schema_hint = json.dumps(schema.model_json_schema())
        stream = await target.client.chat.completions.create(
            model=target.model,
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=target.max_tokens or self.max_tokens,
            timeout=timeout,
            stream=True,
            messages=messages + [{"role": "user", "content": f"Reply with JSON matching this schema: {schema_hint}"}],
            **usage_opts,
        )
        text, usage = [], None

        async def collect():
            nonlocal usage
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    text.append(chunk.choices[0].delta.content)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                yield chunk

        try:
            await self._watch(collect(), timeout, idx)
        finally:
            await stream.close()
        await self._log(SimpleNamespace(usage=usage), purpose, run_id, target.model, started)
        return schema.model_validate_json("".join(text))

    async def _log(self, resp, purpose: str, run_id: str | None, model: str, started: float) -> None:
        latency_ms = round((time.monotonic() - started) * 1000)
        usage = resp.usage
        tin, tout = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        pin, pout = PRICES_PER_MTOK.get(model, (0.0, 0.0))
        await db.log_llm_call(run_id, purpose, model, tin, tout, (tin * pin + tout * pout) / 1e6, latency_ms)
