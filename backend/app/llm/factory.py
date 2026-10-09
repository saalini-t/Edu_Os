from __future__ import annotations

from app.config import Settings
from app.llm.backends import OllamaBackend, OpenAICompatBackend
from app.llm.base import LLMProvider
from app.llm.fake import FakeLLMProvider
from app.llm.prompted import PromptedProvider


def build_provider(settings: Settings) -> LLMProvider:
    """The only place a provider is chosen. Credentials come from the server environment and are never exposed."""
    if settings.llm_provider == "fake":
        return FakeLLMProvider()
    if settings.llm_provider == "ollama":
        return PromptedProvider(OllamaBackend(settings.llm_base_url or "http://localhost:11434", settings.llm_model,
                                              settings.llm_temperature, settings.llm_timeout_s))
    if settings.llm_provider == "openai_compatible":
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
        return PromptedProvider(OpenAICompatBackend(settings.llm_base_url, settings.llm_model, key,
                                                    settings.llm_temperature, settings.llm_timeout_s))
    raise ValueError(f"unknown LLM provider {settings.llm_provider!r}")
