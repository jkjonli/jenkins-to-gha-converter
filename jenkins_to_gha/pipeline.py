"""Agentic loop: converter <-> reviewer until approved or max iterations.

Public API::

    run(jenkinsfile_text, output_path, max_iterations=3,
        converter_client=None, reviewer_client=None) -> PipelineResult

CLI::

    python -m jenkins_to_gha.pipeline <Jenkinsfile> <output.yml> [--max-iterations N]
        [--converter-model MODEL] [--reviewer-model MODEL]
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .converter import convert
from .llm_client import LLMClient, load_env, AnthropicClient
from .reviewer import ReviewResult, review


@dataclass
class _Exchange:
    """A single LLM call: the prompt sent and the response received."""

    role: str  # "converter" or "reviewer"
    user_prompt: str
    response: str


@dataclass
class PipelineResult:
    """Outcome of the full converter-reviewer loop."""

    workflow: str
    approved: bool
    iterations: int
    final_review: ReviewResult


class _RecordingClient:
    """Wraps an LLMClient to record every exchange for the transcript."""

    def __init__(self, inner: LLMClient, role: str, exchanges: list[_Exchange]) -> None:
        self._inner = inner
        self._role = role
        self._exchanges = exchanges

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._inner.complete(system_prompt, user_prompt)
        self._exchanges.append(
            _Exchange(role=self._role, user_prompt=user_prompt, response=response)
        )
        return response


def run_actionlint(workflow_yaml: str) -> str | None:
    """Run actionlint on a workflow YAML string.

    Returns the stderr output if there are errors, or ``None`` if clean.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".yml", mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(workflow_yaml)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            ["actionlint", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0 and proc.stderr.strip():
            return proc.stderr.strip()
        # actionlint may also write errors to stdout
        if proc.returncode != 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return None
    except FileNotFoundError:
        print("[warning] actionlint not found, skipping lint step", file=sys.stderr)
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def run(
    jenkinsfile_text: str,
    output_path: Path | str,
    max_iterations: int = 3,
    converter_client: LLMClient | None = None,
    reviewer_client: LLMClient | None = None,
) -> PipelineResult:
    """Run the converter-reviewer loop and write the final workflow to disk.

    Args:
        jenkinsfile_text: Raw Jenkinsfile source.
        output_path: Where to write the final workflow YAML.
        max_iterations: Cap on converter-reviewer rounds (default 3).
        converter_client: LLM backend for the converter. Defaults to
            ``AnthropicClient()`` (claude-sonnet-4-6).
        reviewer_client: LLM backend for the reviewer. Defaults to
            ``AnthropicClient()``. Use a different model for adversarial
            review (e.g. ``AnthropicClient(model="claude-haiku-4-5-20251001")``).

    Returns:
        A ``PipelineResult`` with the final workflow, verdict, and iteration count.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be at least 1")

    load_env()
    if converter_client is None:
        converter_client = AnthropicClient()
    if reviewer_client is None:
        reviewer_client = AnthropicClient()

    exchanges: list[_Exchange] = []
    conv_recorder = _RecordingClient(converter_client, "converter", exchanges)
    rev_recorder = _RecordingClient(reviewer_client, "reviewer", exchanges)
    feedback: str | None = None
    previous_workflow: str | None = None

    for iteration in range(1, max_iterations + 1):
        print(f"[iteration {iteration}/{max_iterations}] Converting...", file=sys.stderr)
        workflow = convert(
            jenkinsfile_text,
            feedback=feedback,
            client=conv_recorder,
            previous_workflow=previous_workflow,
        )

        print(f"[iteration {iteration}/{max_iterations}] Linting...", file=sys.stderr)
        lint_output = run_actionlint(workflow)
        if lint_output:
            print(f"[iteration {iteration}/{max_iterations}] actionlint found issues.", file=sys.stderr)
        else:
            print(f"[iteration {iteration}/{max_iterations}] actionlint clean.", file=sys.stderr)

        print(f"[iteration {iteration}/{max_iterations}] Reviewing...", file=sys.stderr)
        result = review(jenkinsfile_text, workflow, client=rev_recorder, lint_output=lint_output)

        if result.approved:
            print(f"[iteration {iteration}/{max_iterations}] Approved.", file=sys.stderr)
            _write_output(workflow, output_path)
            _write_transcript(exchanges, output_path)
            return PipelineResult(
                workflow=workflow,
                approved=True,
                iterations=iteration,
                final_review=result,
            )

        print(
            f"[iteration {iteration}/{max_iterations}] Changes requested.",
            file=sys.stderr,
        )
        feedback = result.feedback
        previous_workflow = workflow

    # Exhausted iterations — write with .unapproved extension and warning header.
    unapproved_path = _unapproved_path(output_path)
    print(
        f"[iteration {max_iterations}/{max_iterations}] Max iterations reached, "
        f"writing unapproved workflow to {unapproved_path}",
        file=sys.stderr,
    )
    tagged_workflow = (
        "# FAILED REVIEW — not approved after "
        f"{max_iterations} iteration(s). Do not use without manual review.\n"
        + workflow
    )
    _write_output(tagged_workflow, unapproved_path)
    _write_transcript(exchanges, output_path)
    return PipelineResult(
        workflow=tagged_workflow,
        approved=False,
        iterations=max_iterations,
        final_review=result,
    )


def _unapproved_path(output_path: Path | str) -> Path:
    """Derive the unapproved output path.

    ``output/complex.yml`` -> ``output/complex.unapproved.yml``
    """
    p = Path(output_path)
    return p.with_suffix(".unapproved" + p.suffix)


def _write_output(workflow: str, output_path: Path | str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow, encoding="utf-8")


def _transcript_path(output_path: Path | str) -> Path:
    """Derive the transcript path from the workflow output path.

    ``output/complex.yml`` -> ``output/complex.transcript.md``
    """
    p = Path(output_path)
    return p.with_suffix(".transcript.md")


def _write_transcript(exchanges: list[_Exchange], output_path: Path | str) -> None:
    """Write a Markdown transcript of all converter/reviewer exchanges."""
    path = _transcript_path(output_path)
    lines: list[str] = ["# Pipeline Transcript", ""]

    iteration = 0
    for ex in exchanges:
        if ex.role == "converter":
            iteration += 1
            lines.append(f"## Iteration {iteration}")
            lines.append("")

        if ex.role == "converter":
            lines.append("### Converter prompt")
            lines.append("")
            lines.append(ex.user_prompt)
            lines.append("")
            lines.append("### Converter response")
            lines.append("")
            lines.append("```yaml")
            lines.append(ex.response.rstrip("\n"))
            lines.append("```")
            lines.append("")
        else:
            lines.append("### Reviewer prompt")
            lines.append("")
            lines.append(ex.user_prompt)
            lines.append("")
            lines.append("### Reviewer response")
            lines.append("")
            lines.append("```json")
            lines.append(ex.response.strip())
            lines.append("```")
            lines.append("")
            lines.append("---")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Convert a Jenkinsfile to GitHub Actions via converter-reviewer loop."
    )
    parser.add_argument("jenkinsfile", type=Path, help="Path to the source Jenkinsfile")
    parser.add_argument("output", type=Path, help="Path for the output workflow YAML")
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum converter-reviewer iterations (default: 3)",
    )
    parser.add_argument(
        "--converter-model",
        default=AnthropicClient.DEFAULT_MODEL,
        help=f"Model for the converter (default: {AnthropicClient.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--reviewer-model",
        default=AnthropicClient.DEFAULT_MODEL,
        help=f"Model for the reviewer (default: {AnthropicClient.DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    load_env()

    if not args.jenkinsfile.exists():
        print(f"error: Jenkinsfile not found: {args.jenkinsfile}", file=sys.stderr)
        raise SystemExit(2)

    try:
        jenkinsfile_text = args.jenkinsfile.read_text(encoding="utf-8")
        result = run(
            jenkinsfile_text,
            args.output,
            max_iterations=args.max_iterations,
            converter_client=AnthropicClient(model=args.converter_model),
            reviewer_client=AnthropicClient(model=args.reviewer_model),
        )
    except Exception as exc:
        # Lazy import to avoid hard dependency when not using CLI.
        try:
            from anthropic import APIError
        except ImportError:
            APIError = type(None)  # type: ignore[assignment,misc]

        if isinstance(exc, FileNotFoundError):
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        if isinstance(exc, APIError):
            print(f"error: API call failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
        if isinstance(exc, ValueError):
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        raise  # Unexpected errors still get a full traceback.

    transcript = _transcript_path(args.output)
    if result.approved:
        print(f"Approved after {result.iterations} iteration(s). Written to {args.output}")
        print(f"Transcript: {transcript}")
    else:
        unapproved = _unapproved_path(args.output)
        print(
            f"Not approved after {result.iterations} iteration(s). "
            f"Unapproved workflow written to {unapproved}",
            file=sys.stderr,
        )
        print(f"Transcript: {transcript}", file=sys.stderr)
        print("Remaining feedback:", file=sys.stderr)
        print(result.final_review.feedback, file=sys.stderr)
        raise SystemExit(1)
