"""Converter agent: Jenkinsfile -> GitHub Actions YAML via an LLM.

Public API::

    convert(jenkinsfile_text, feedback=None, client=None) -> str

The function loads the system prompt from ``prompts/converter_system.md``,
sends the Jenkinsfile (plus optional reviewer feedback) to the configured
LLM, post-processes the response to strip stray Markdown fences, and
verifies the output has the shape of a GitHub Actions workflow.
"""
from __future__ import annotations

import re
from pathlib import Path

from .llm_client import AnthropicClient, LLMClient, load_env

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "converter_system.md"

_FENCED_WHOLE_RE = re.compile(
    r"^\s*```(?:[Yy][Aa]?[Mm][Ll]|yml)?\s*\n(?P<body>.*?)\n```\s*\Z",
    re.DOTALL,
)


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ```yaml ... ``` fence if the whole
    response is wrapped in one. Otherwise return the text unchanged
    (but stripped of surrounding whitespace)."""
    stripped_text = text.strip()
    fence_match = _FENCED_WHOLE_RE.match(stripped_text)
    if fence_match:
        stripped_text = fence_match.group("body").strip()
    return stripped_text + "\n"


def _find_first_nonblank_line(text: str) -> str:
    return next(
        (line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "",
    )


def _assert_workflow_shape(yaml_text: str) -> None:
    """Raise ValueError if the text does not look like a GitHub Actions workflow."""
    first_line = _find_first_nonblank_line(yaml_text)
    # `on` is a YAML 1.1 boolean, so the model may emit `'on':` or `"on":`.
    has_valid_trigger = first_line.startswith(("name:", "on:", "'on':", '"on":'))
    if not has_valid_trigger:
        raise ValueError(
            "converter output does not look like a GitHub Actions workflow "
            f"(first non-blank line: {first_line!r})"
        )


def _build_user_message(jenkinsfile_text: str, feedback: str | None) -> str:
    parts = [
        "# Jenkinsfile (source)",
        "```groovy",
        jenkinsfile_text.rstrip("\n"),
        "```",
    ]
    if feedback:
        parts.extend(
            [
                "",
                "# Reviewer feedback (iterate to address each point)",
                feedback.rstrip("\n"),
            ]
        )
    return "\n".join(parts)


def load_system_prompt() -> str:
    """Read the converter system prompt from disk."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def convert(
    jenkinsfile_text: str,
    feedback: str | None = None,
    client: LLMClient | None = None,
    system_prompt: str | None = None,
) -> str:
    """Convert Jenkinsfile text to a GitHub Actions workflow YAML string.

    Args:
        jenkinsfile_text: Raw Jenkinsfile source.
        feedback: Optional reviewer feedback to incorporate on re-runs.
        client: LLM backend. Defaults to ``AnthropicClient()``.
        system_prompt: Optional system prompt override. When omitted, the
            packaged ``prompts/converter_system.md`` is loaded from disk.
            Injecting an explicit string lets callers (and tests) avoid
            filesystem coupling.

    Returns:
        A YAML string (terminated by a newline). Shape-checked but not
        actionlint-validated (that is the reviewer's job).
    """
    if client is None:
        load_env()
        client = AnthropicClient()
    if system_prompt is None:
        system_prompt = load_system_prompt()
    user_prompt = _build_user_message(jenkinsfile_text, feedback)
    raw_completion = client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    yaml_text = _strip_code_fences(raw_completion)
    _assert_workflow_shape(yaml_text)
    return yaml_text


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m src.converter <path/to/Jenkinsfile>", file=sys.stderr)
        raise SystemExit(2)
    src = Path(sys.argv[1]).read_text(encoding="utf-8")
    sys.stdout.write(convert(src))
