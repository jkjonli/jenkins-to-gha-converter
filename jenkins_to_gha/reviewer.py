"""Reviewer agent: inspects a GitHub Actions workflow against the source Jenkinsfile.

Public API::

    review(jenkinsfile_text, workflow_yaml, client=None, system_prompt=None) -> ReviewResult

The function loads the system prompt from ``prompts/reviewer_system.md``,
sends both the original Jenkinsfile and the generated workflow to the LLM,
and parses the response into a structured verdict.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .llm_client import AnthropicClient, LLMClient, load_env

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "reviewer_system.md"


@dataclass
class ReviewResult:
    """Outcome of a review: approved or changes requested with feedback."""

    approved: bool
    feedback: str


_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def _parse_verdict(response: str) -> ReviewResult:
    """Parse the LLM's JSON response into a ReviewResult.

    Accepts raw JSON or JSON wrapped in Markdown fences.
    """
    text = response.strip()

    # Strip optional Markdown fences.
    fence_match = _JSON_BLOCK_RE.search(text)
    if fence_match:
        text = fence_match.group("body").strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Model didn't return valid JSON — treat the whole response as
        # unapproved feedback so the loop can still function.
        return ReviewResult(approved=False, feedback=text)

    approved = bool(data.get("approved", False))
    raw_issues = data.get("issues") or []
    issues = [str(i) for i in raw_issues if i is not None]
    feedback = "\n".join(f"- {issue}" for issue in issues) if issues else ""
    return ReviewResult(approved=approved, feedback=feedback)


def _build_user_message(jenkinsfile_text: str, workflow_yaml: str) -> str:
    parts = [
        "# Jenkinsfile (source)",
        "```groovy",
        jenkinsfile_text.rstrip("\n"),
        "```",
        "",
        "# GitHub Actions workflow (generated)",
        "```yaml",
        workflow_yaml.rstrip("\n"),
        "```",
    ]
    return "\n".join(parts)


def load_system_prompt() -> str:
    """Read the reviewer system prompt from disk."""
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Reviewer system prompt not found at {_PROMPT_PATH}. "
            "Ensure the prompts/ directory is present in the project root."
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


def review(
    jenkinsfile_text: str,
    workflow_yaml: str,
    client: LLMClient | None = None,
    system_prompt: str | None = None,
) -> ReviewResult:
    """Review a generated GitHub Actions workflow against its source Jenkinsfile.

    Args:
        jenkinsfile_text: Raw Jenkinsfile source.
        workflow_yaml: Generated GitHub Actions workflow YAML.
        client: LLM backend. Defaults to ``AnthropicClient()``.
        system_prompt: Optional system prompt override.

    Returns:
        A ``ReviewResult`` with ``approved`` flag and ``feedback`` text.
    """
    if client is None:
        load_env()
        client = AnthropicClient()
    if system_prompt is None:
        system_prompt = load_system_prompt()
    user_prompt = _build_user_message(jenkinsfile_text, workflow_yaml)
    raw_response = client.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    return _parse_verdict(raw_response)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print(
            "usage: python -m jenkins_to_gha.reviewer <Jenkinsfile> <workflow.yml>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    jenkinsfile = Path(sys.argv[1]).read_text(encoding="utf-8")
    workflow = Path(sys.argv[2]).read_text(encoding="utf-8")
    result = review(jenkinsfile, workflow)
    if result.approved:
        print("APPROVED")
    else:
        print("CHANGES REQUESTED")
        print(result.feedback)
    raise SystemExit(0 if result.approved else 1)
