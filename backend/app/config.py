from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Any OpenAI-compatible endpoint: Gemini free tier, local Ollama, Groq, OpenRouter...
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.8-flash"
    # Tried in order when the current model's quota is used up or it's overloaded.
    # "model" = same provider as LLM_BASE_URL; "provider:model" = another provider
    # (groq, cerebras, deepseek, openrouter, ollama, gemini), using that provider's key below.
    # Entries whose provider has no key set are skipped, so listing more is harmless.
    # Gemini's free tier gives each model its own daily quota, so more models = more runs a day.
    llm_fallbacks: str = "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,groq:openai/gpt-oss-120b"
    llm_max_tokens: int = 16000  # a 7-day plan is ~11k output tokens
    # A free model can accept a request and never answer; give up on it after this and try the next one.
    # Generous on purpose: Gemini Flash has taken ~220 s to write a full week (~5k tokens).
    llm_timeout_s: float = 300
    # Store choice, product picks and intake write a few hundred tokens; a minute and a half is plenty.
    llm_short_timeout_s: float = 90
    # A model that is silent this long is "slow": the same request also goes to the next model and the first
    # answer wins. (A healthy model can take minutes before its first byte, so waiting alone can't tell slow
    # from dead.) Small tasks hedge sooner because their answers are tiny.
    llm_hedge_after_s: float = 45
    llm_hedge_after_short_s: float = 20
    llm_stall_s: float = 60   # longest silence allowed once an answer has started streaming
    # A model to try first for everything (a label from LLM_FALLBACKS, e.g. "deepseek:deepseek-flash").
    # The others stay as backups, in their usual order.
    llm_preferred: str = ""
    # Small tasks try this provider's model first (a label prefix from LLM_FALLBACKS), then the usual order.
    llm_short_tasks_first: str = "groq:"
    # Hard stop on AI calls in one run, so a repair loop can't quietly burn a day's free quota.
    # A normal run makes 5 to 10.
    llm_max_calls_per_run: int = 40
    groq_api_key: str = ""
    cerebras_api_key: str = ""      # free trial tier, very fast: https://cloud.cerebras.ai
    # pay-as-you-go (a small one-time free grant for new accounts); DS_API_KEY is accepted as a short name
    deepseek_api_key: str = Field("", validation_alias=AliasChoices("deepseek_api_key", "ds_api_key"))
    openrouter_api_key: str = ""
    gemini_api_key: str = ""  # only needed if Gemini is a fallback rather than LLM_BASE_URL
    # Days planned per LLM call. 7 = one call per week (free tiers cap requests/day);
    # use 1 for small local models that struggle with long replies.
    llm_days_per_call: int = 7
    usda_api_key: str = ""
    profile_root: str = "profiles"
    database_path: str = "./mealcart.db"
    checkpoint_path: str = "./checkpoints.db"
    single_user_id: str = "user_default"
    # hidden: Chrome runs with no window. visible: you watch it. auto: hidden, except a window
    # opens when you need to sign in (a CAPTCHA can only be solved in a visible window).
    browser_mode: str = "auto"
    web_origins: str = "http://localhost:3000,http://127.0.0.1:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
