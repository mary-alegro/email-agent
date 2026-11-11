from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App configuration, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/app.db"

    # ERP MCP server the agent talks to. Unset = the in-process mock ERP server;
    # set to e.g. http://localhost:8000/mcp to go over streamable HTTP.
    erp_mcp_url: str | None = None

    # LLM used for extraction.
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    llm_model: str | None = None  # default: claude-opus-5-5 (anthropic); required for openai
    llm_effort: str | None = None  # anthropic: effort (default medium); openai: reasoning_effort

    # API keys. If unset, each SDK falls back to its own environment variable
    # (ANTHROPIC_API_KEY / OPENAI_API_KEY); this also lets them live in .env.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # Commitments below this model confidence are marked needs_review.
    review_confidence_threshold: float = 0.8

    # Logging: level (DEBUG, INFO, WARNING, ERROR) and format ("text" or "json").
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"


settings = Settings()
