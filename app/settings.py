"""Runtime settings loaded from environment (see .env.example)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct"
    openrouter_fallback_model: str = "meta-llama/llama-3.1-70b-instruct"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
