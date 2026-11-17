"""llm — local OpenAI-compatible LLM client.

Currently a library only — no CLI subcommand wraps it yet.
If/when a command needs it (`deeppeek llm ask`, `deeppeek llm judge`,
…) that command imports `LLMClient` from here.

Public API:

    LLMClient
    DEFAULT_LLM_URL, DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT, DEFAULT_PROBE_TIMEOUT
"""
from .client import (
    LLMClient,
    DEFAULT_LLM_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT,
    DEFAULT_PROBE_TIMEOUT,
)

__all__ = [
    "LLMClient",
    "DEFAULT_LLM_URL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_LLM_TIMEOUT",
    "DEFAULT_PROBE_TIMEOUT",
]
