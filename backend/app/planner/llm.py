"""One structured-output call path for every LLM use in the app.

Works with any OpenAI-compatible endpoint (Gemini free tier, local Ollama,
Groq, OpenRouter...), so switching provider is a .env change.
"""

import json
import re
from dataclasses import dataclass
from typing import TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app import db
from app.config import get_settings

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). Free tiers are 0; add a row if you switch
# to a paid model so runs.llm_cost_usd stays meaningful.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {}


class LLMOutputError(Exception):
    """The model failed to return valid structured output after a retry."""


class LLMQuotaError(Exception):
    """The provider's rate limit or free-tier quota is used up."""


def _why(e: Exception) -> str:
    if isinstance(e, openai.InternalServerError):
        return "overloaded"
    wait = re.search(r"retry in ((?:\d+h)?(?:\d+m)?\d+)(?:\.\d+)?s", str(e))
    return "quota used up" + (f", resets in {wait.group(1)}s" if wait else "")


PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini_api_key"),
    "groq": ("https://api.groq.com/openai/v1", "groq_api_key"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter_api_key"),
    "ollama": ("http://localhost:11434/v1", None),
}


@dataclass
class Target:
    label: str
    client: AsyncOpenAI
    model: str
    json_schema_supported: bool = True


def _client(base_url: str, api_key: str) -> AsyncOpenAI:
    # Retries short 429/5xx blips with backoff before we move to the next target.
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "none", max_retries=2)


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
            targets.append(Target(entry, _client(base_url, key), model))
        else:  # bare model name (may itself contain ':' e.g. qwen3:8b) on the primary provider
            targets.append(Target(entry, primary, entry))
    return targets


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
        self._current = 0

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
        unavailable: list[str] = []
        while True:
            try:
                return await self._structured_once(schema, messages, purpose, run_id, temperature)
            except (openai.RateLimitError, openai.InternalServerError) as e:
                unavailable.append(f"{self.targets[self._current].label}: {_why(e)}")
                if self._current + 1 >= len(self.targets):
                    raise LLMQuotaError("no model available — " + "; ".join(unavailable)
                                        + ". Wait, or add entries to LLM_FALLBACKS in .env.") from e
                self._current += 1  # stays on the working target for the rest of the run

    async def _structured_once(self, schema: type[T], messages: list[dict], purpose: str,
                               run_id: str | None, temperature: float) -> T:
        for attempt in range(2):
            try:
                return await self._call(schema, messages, purpose, run_id, temperature)
            except (ValidationError, json.JSONDecodeError, openai.LengthFinishReasonError, LLMOutputError) as e:
                if attempt == 1:
                    raise LLMOutputError(f"{purpose}: invalid structured output after retry: {e}") from e
                messages = messages + [{
                    "role": "user",
                    "content": f"Your previous reply was not valid for the schema: {str(e)[:500]}. "
                               "Reply again with only valid JSON matching the schema.",
                }]
        raise AssertionError("unreachable")

    async def _call(self, schema: type[T], messages: list[dict], purpose: str, run_id: str | None,
                    temperature: float) -> T:
        target = self.targets[self._current]
        if target.json_schema_supported:
            try:
                resp = await self.client.chat.completions.parse(
                    model=self.model, messages=messages, response_format=schema, temperature=temperature,
                    max_tokens=self.max_tokens,
                )
            except openai.BadRequestError as e:
                if "response_format" not in str(e) and "schema" not in str(e):
                    raise
                target.json_schema_supported = False  # endpoint can't do json_schema; use json_object
            else:
                await self._log(resp, purpose, run_id)
                parsed = resp.choices[0].message.parsed
                if parsed is None:
                    raise LLMOutputError("empty or refused reply")
                return parsed

        schema_hint = json.dumps(schema.model_json_schema())
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens,
            messages=messages + [{"role": "user", "content": f"Reply with JSON matching this schema: {schema_hint}"}],
        )
        await self._log(resp, purpose, run_id)
        return schema.model_validate_json(resp.choices[0].message.content or "")

    async def _log(self, resp, purpose: str, run_id: str | None) -> None:
        usage = resp.usage
        tin, tout = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        pin, pout = PRICES_PER_MTOK.get(self.model, (0.0, 0.0))
        await db.log_llm_call(run_id, purpose, self.model, tin, tout, (tin * pin + tout * pout) / 1e6)
