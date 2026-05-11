"""LLM client abstraction for the Jenkins -> GitHub Actions converter.

Defines a minimal text-in/text-out interface so we can swap backends
(Anthropic now; GitHub Models and Ollama later) behind a single seam
without touching converter/reviewer code.

Running this module directly performs a round-trip sanity check against
the default backend::

    python -m jenkins_to_gha.llm_client
"""
from __future__ import annotations

import os
import sys
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv


@runtime_checkable
class LLMClient(Protocol):
    """Minimal LLM interface: a single text-in, text-out completion."""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Return the model's text response given a system and user prompt."""
        ...


class AnthropicClient:
    """LLMClient backed by Anthropic's Messages API.

    Defaults match the project plan: model ``claude-opus-4-7`` and
    ``max_tokens=4096``. Both can be overridden at construction time.
    """

    DEFAULT_MODEL = "claude-opus-4-7"
    DEFAULT_MAX_TOKENS = 4096
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_TIMEOUT = 120.0

    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        # Imported lazily so importing this module doesn't require the
        # SDK if a caller only wants the Protocol type.
        import anthropic

        self.model: str = model or self.DEFAULT_MODEL
        self.max_tokens: int = max_tokens
        # Token usage from the most recent ``complete()`` call. ``None``
        # until the first call. The pipeline's recording layer reads these
        # to annotate transcript headers with model + token context.
        self.last_input_tokens: int | None = None
        self.last_output_tokens: int | None = None
        self._client = anthropic.Anthropic(
            api_key=api_key or None,
            max_retries=max_retries,
            timeout=timeout,
        )

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Snapshot token usage (best-effort; absent on some error paths).
        usage = getattr(response, "usage", None)
        self.last_input_tokens = getattr(usage, "input_tokens", None)
        self.last_output_tokens = getattr(usage, "output_tokens", None)
        # Claude responses are a list of content blocks; join text blocks.
        parts: list[str] = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)


def load_env() -> None:
    """Load ``.env`` so the Anthropic SDK can find ``ANTHROPIC_API_KEY``."""
    load_dotenv()


def _main() -> int:
    load_env()
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (checked .env too)", file=sys.stderr)
        return 2
    client: LLMClient = AnthropicClient(max_tokens=32)
    reply = client.complete(
        system_prompt="You are a terse assistant. Reply with exactly one word.",
        user_prompt="Say the word 'pong' and nothing else.",
    )
    print(f"model={client.model!r}")
    print(f"reply={reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
