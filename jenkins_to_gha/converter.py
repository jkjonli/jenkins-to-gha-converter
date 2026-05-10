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

_FENCED_BLOCK_RE = re.compile(
    r"```(?:[Yy][Aa]?[Mm][Ll]|yml)?\s*\n(?P<body>.*?)```",
    re.DOTALL,
)


def _strip_code_fences(text: str) -> str:
    """Extract the YAML from the first fenced code block in the response.

    Handles conversational preamble (e.g. "Here is the workflow:"),
    trailing commentary, and missing trailing newlines. If no fence is
    found, falls back to the full text stripped of whitespace.
    """
    fence_match = _FENCED_BLOCK_RE.search(text)
    if fence_match:
        return fence_match.group("body").strip() + "\n"
    return text.strip() + "\n"


def _find_first_nonblank_line(text: str) -> str:
    return next(
        (line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "",
    )


def _assert_workflow_shape(yaml_text: str) -> None:
    """Raise ValueError if the text does not look like a GitHub Actions workflow."""
    first_line = _find_first_nonblank_line(yaml_text)
    # `on` is a YAML 1.1 boolean, so the model may emit `'on':` or `"on":`.
    has_valid_workflow_prefix = first_line.startswith(("name:", "on:", "'on':", '"on":'))
    if not has_valid_workflow_prefix:
        raise ValueError(
            "converter output does not look like a GitHub Actions workflow "
            f"(first non-blank line: {first_line!r})"
        )


def _build_user_message(
    jenkinsfile_text: str,
    feedback: str | None,
    previous_workflow: str | None = None,
) -> str:
    parts = [
        "# Jenkinsfile (source)",
        "```groovy",
        jenkinsfile_text.rstrip("\n"),
        "```",
    ]
    if previous_workflow and feedback:
        parts.extend(
            [
                "",
                "# Previous GitHub Actions workflow (revise this)",
                "```yaml",
                previous_workflow.rstrip("\n"),
                "```",
                "",
                "# Reviewer feedback (iterate to address each point)",
                feedback.rstrip("\n"),
            ]
        )
    elif feedback:
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
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Converter system prompt not found at {_PROMPT_PATH}. "
            "Ensure the prompts/ directory is present in the project root."
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


def convert(
    jenkinsfile_text: str,
    feedback: str | None = None,
    client: LLMClient | None = None,
    system_prompt: str | None = None,
    previous_workflow: str | None = None,
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
        previous_workflow: The workflow from the prior iteration, included
            so the converter can revise it rather than regenerate from scratch.

    Returns:
        A YAML string (terminated by a newline). Shape-checked; deeper
        semantic validation is the reviewer's job.
    """
    if client is None:
        load_env()
        client = AnthropicClient()
    if system_prompt is None:
        system_prompt = load_system_prompt()
    user_prompt = _build_user_message(jenkinsfile_text, feedback, previous_workflow)
    raw_completion = client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    yaml_text = _strip_code_fences(raw_completion)
    _assert_workflow_shape(yaml_text)
    return yaml_text


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m jenkins_to_gha.converter <path/to/Jenkinsfile>", file=sys.stderr)
        raise SystemExit(2)
    src = Path(sys.argv[1]).read_text(encoding="utf-8")
    sys.stdout.write(convert(src))
