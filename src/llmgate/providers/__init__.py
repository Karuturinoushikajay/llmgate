"""Provider adapters. Each one speaks a vendor API and returns OpenAI shapes."""

from __future__ import annotations

import httpx

from llmgate.config import Settings
from llmgate.providers.anthropic import AnthropicProvider
from llmgate.providers.base import ChatProvider
from llmgate.providers.gemini import GeminiProvider
from llmgate.providers.ollama import OllamaProvider
from llmgate.providers.openai import OpenAIProvider


def build_providers(settings: Settings, http: httpx.AsyncClient) -> dict[str, ChatProvider]:
    return {
        "openai": OpenAIProvider(settings.openai_api_key, settings.openai_base_url, http),
        "anthropic": AnthropicProvider(
            settings.anthropic_api_key,
            settings.anthropic_base_url,
            http,
        ),
        "gemini": GeminiProvider(settings.gemini_api_key, settings.gemini_base_url, http),
        "ollama": OllamaProvider(settings.ollama_base_url, http),
    }
