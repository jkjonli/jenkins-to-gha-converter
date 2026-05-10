"""Reviewer agent: inspects a GitHub Actions workflow against the source Jenkinsfile.

Public API::

    review(jenkinsfile_text, workflow_yaml, client=None, system_prompt=None) -> ReviewResult

The function loads the system prompt from ``prompts/reviewer_system.md``,
sends both the original Jenkinsfile and the generated workflow to the LLM,
and parses the response into a structured verdict.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .llm_client import AnthropicClient, LLMClient, load_env

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "reviewer_system.md"


@dataclass
class ReviewResult:
    """Outcome of a review: approved or changes requested with feedback."""

    approved: bool
    feedback: str


def _find_json_objects(text: str) -> list[str]:
    """Return every top-level ``{...}`` substring in ``text``, in order.

    Scans with a brace-depth counter that respects JSON string literals
    (so braces inside quoted strings do not confuse the scanner). This
    is more robust than a regex against the kinds of responses Claude
    actually emits: doubled Markdown fences (``` ```json\\n```json\\n{...} ```),
    multi-verdict chain-of-thought, and prose interleaved with JSON.
    """
    results: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    results.append(text[start : i + 1])
                    start = -1
    return results


def _parse_verdict(response: str) -> ReviewResult:
    """Parse the LLM's JSON response into a ReviewResult.

    Tolerates: bare JSON, single Markdown fence, doubled Markdown fences
    (observed with Opus 4-6), and chain-of-thought responses where the
    model emits multiple verdicts before settling on a final answer.
    The LAST parseable JSON object wins — that is the model's settled
    verdict, not its first instinct.
    """
    text = response.strip()

    # Walk every candidate JSON object from last to first; the last
    # parseable dict is the model's final answer.
    for candidate in reversed(_find_json_objects(text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        approved = bool(data.get("approved", False))
        raw_issues = data.get("issues") or []
        issues = [str(i) for i in raw_issues if i is not None]
        feedback = "\n".join(f"- {issue}" for issue in issues) if issues else ""
        return ReviewResult(approved=approved, feedback=feedback)

    # No parseable JSON object found — treat the whole response as
    # unapproved feedback so the loop can still function.
    return ReviewResult(approved=False, feedback=text)


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
