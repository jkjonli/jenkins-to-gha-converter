"""LLM client abstraction for the Jenkins -> GitHub Actions converter.

Defines a minimal text-in/text-out interface so we can swap backends
(Anthropic now; GitHub Models and Ollama later) behind a single seam
without touching converter/reviewer code.

Running this module directly performs a round-trip sanity check against
the default backend::

    python -m src.llm_client
"""
from __future__ import annotations

import os
import sys
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv


@runtime_checkable
class LLMClient(Protocol):
    """Minimal LLM interface: a single text-in, text-out completion."""

    def complete(self, system: str, user: str) -> str:
        """Return the model's text response given a system and user message."""
        ...


class AnthropicClient:
    """LLMClient backed by Anthropic's Messages API.

    Defaults match the project plan: model ``claude-sonnet-4-6`` and
    ``max_tokens=4096``. Both can be overridden at construction time.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        api_key: str | None = None,
    ) -> None:
        # Imported lazily so importing this module doesn't require the
        # SDK if a caller only wants the Protocol type.
        import anthropic

        self.model: str = model or self.DEFAULT_MODEL
        self.max_tokens: int = max_tokens
        self._client = (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )

    def complete(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        # Claude responses are a list of content blocks; join text blocks.
        parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)


def load_env() -> None:
    """Load ``.env`` and tolerate a known autocorrect typo.

    Some editors autocorrect ``ANTHROPIC`` to ``PATHOLOGY``. If that
    alias is present and the canonical name isn't, copy it over so
    the Anthropic SDK finds the key.
    """
    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY") and os.getenv("PATHOLOGY_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["PATHOLOGY_API_KEY"]


def _main() -> int:
    load_env()
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (checked .env too)", file=sys.stderr)
        return 2
    client: LLMClient = AnthropicClient(max_tokens=32)
    reply = client.complete(
        system="You are a terse assistant. Reply with exactly one word.",
        user="Say the word 'pong' and nothing else.",
    )
    print(f"model={client.model!r}")
    print(f"reply={reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
