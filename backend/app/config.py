from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Any OpenAI-compatible endpoint: Gemini free tier, local Ollama, Groq, OpenRouter...
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.8-flash"
    # Tried in order when the current model's quota is used up or it's overloaded.
    # "model" = same provider as LLM_BASE_URL; "provider:model" = another provider
    # (groq, openrouter, ollama, gemini), using that provider's key below.
    # Entries whose provider has no key set are skipped, so listing more is harmless.
    # Gemini's free tier gives each model its own daily quota, so more models = more runs a day.
    llm_fallbacks: str = "gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,groq:openai/gpt-oss-120b"
    llm_max_tokens: int = 16000  # a 7-day plan is ~11k output tokens
    # Hard stop on AI calls in one run, so a repair loop can't quietly burn a day's free quota.
    # A normal run makes 5 to 10.
    llm_max_calls_per_run: int = 40
    groq_api_key: str = ""
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
    web_origins: str = "http://localhost:3000,http://127.0.0.1:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
