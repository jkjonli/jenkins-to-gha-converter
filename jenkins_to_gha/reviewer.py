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


def _format_finding(finding: dict) -> str:
    """Format a single v2/v3 reviewer finding dict into a readable line.

    The reviewer emits objects shaped like::

        {"id": "V4", "severity": "blocking", "where": "...",
         "what": "...", "why": "...", "fix": "..."}

    The converter sees this line verbatim under "Reviewer feedback", so
    the format prioritises what the converter needs to act: rubric id,
    severity, location, problem, and the concrete fix. ``why`` is
    intentionally dropped to keep the line compact — the converter
    doesn't need to re-derive the rubric justification.
    """
    rid = finding.get("id", "V?")
    sev = finding.get("severity", "blocking")
    where = finding.get("where", "")
    what = finding.get("what", "")
    fix = finding.get("fix", "")
    head = f"- [{rid}/{sev}] {where}: {what}" if where else f"- [{rid}/{sev}] {what}"
    if fix:
        return f"{head}\n  Fix: {fix}"
    return head


def _parse_verdict(response: str) -> ReviewResult:
    """Parse the LLM's JSON response into a ReviewResult.

    Tolerates: bare JSON, single Markdown fence, doubled Markdown fences
    (observed with Opus 4-6), and chain-of-thought responses where the
    model emits multiple verdicts before settling on a final answer.
    The LAST parseable JSON object wins — that is the model's settled
    verdict, not its first instinct.

    Accepts both the v2/v3 reviewer-prompt shape (``findings`` array of
    objects with ``id``/``severity``/``where``/``what``/``why``/``fix``)
    and the legacy v1 shape (``issues`` array of strings). When both
    keys are present, ``findings`` wins.
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
        # Prefer the v2/v3 `findings` shape; fall back to v1 `issues` for
        # backward compatibility with any caller still using the old prompt.
        raw_findings = data.get("findings") or data.get("issues") or []
        feedback_lines: list[str] = []
        for item in raw_findings:
            if isinstance(item, dict):
                feedback_lines.append(_format_finding(item))
            elif item is not None:
                feedback_lines.append(f"- {item}")
        feedback = "\n".join(feedback_lines)
        return ReviewResult(approved=approved, feedback=feedback)

    # No parseable JSON object found — treat the whole response as
    # unapproved feedback so the loop can still function.
    return ReviewResult(approved=False, feedback=text)


_ITERATIVE_MODE_BLOCK = (
    "# Review mode: Iterative (non-final)\n"
    "\n"
    "There will be another converter pass after this one. Be thorough:\n"
    "raise both blocking AND advisory findings against the V1-V11 rubric,\n"
    "even when the workflow is functionally fine — this iteration's\n"
    "purpose is to drive improvement. Set `approved: false` if you have\n"
    "ANY findings of any severity. Approve only when `findings: []`."
)

_FINAL_MODE_BLOCK = (
    "# Review mode: FINAL iteration\n"
    "\n"
    "This is the LAST review pass. After this verdict the workflow ships\n"
    "as-is — there is no chance to address advisories. Therefore:\n"
    "\n"
    "- Raise ONLY `severity: blocking` findings — issues that would cause\n"
    "  a runtime failure or a demonstrable behavioural divergence from the\n"
    "  Jenkinsfile (the kind of thing covered by V1-V11 at blocking level).\n"
    "- Do NOT raise advisory findings. Do NOT reject for stylistic or\n"
    "  could-be-better reasons. The bar here is: \"will this fail when it\n"
    "  runs?\", not \"could this be cleaner?\".\n"
    "- Set `approved: true` unless there is at least one blocking finding."
)


def _build_user_message(
    jenkinsfile_text: str,
    workflow_yaml: str,
    is_final_iteration: bool = False,
) -> str:
    """Compose the reviewer user message.

    The mode block at the top tells the reviewer whether this is the
    final iteration (critical-only) or an earlier iteration (thorough,
    drives improvement). The block is the only signal the reviewer has
    of where it is in the loop.
    """
    mode_block = _FINAL_MODE_BLOCK if is_final_iteration else _ITERATIVE_MODE_BLOCK
    parts = [
        mode_block,
        "",
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
    is_final_iteration: bool = False,
) -> ReviewResult:
    """Review a generated GitHub Actions workflow against its source Jenkinsfile.

    Args:
        jenkinsfile_text: Raw Jenkinsfile source.
        workflow_yaml: Generated GitHub Actions workflow YAML.
        client: LLM backend. Defaults to ``AnthropicClient()``.
        system_prompt: Optional system prompt override.
        is_final_iteration: When True, the user prompt instructs the
            reviewer to flag only blocking findings and approve otherwise.
            When False (default), the reviewer is asked to be thorough
            and surface advisories so the converter can iterate.

    Returns:
        A ``ReviewResult`` with ``approved`` flag and ``feedback`` text.
    """
    if client is None:
        load_env()
        client = AnthropicClient()
    if system_prompt is None:
        system_prompt = load_system_prompt()
    user_prompt = _build_user_message(
        jenkinsfile_text, workflow_yaml, is_final_iteration=is_final_iteration
    )
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
