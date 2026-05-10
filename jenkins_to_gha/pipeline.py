"""Agentic loop: converter <-> reviewer until approved or max iterations.

Public API::

    run(jenkinsfile_text, output_path, max_iterations=3,
        converter_client=None, reviewer_client=None) -> PipelineResult

CLI::

    python -m jenkins_to_gha.pipeline <Jenkinsfile> <output.yml> [--max-iterations N]
        [--model MODEL] [--dry-run]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from .converter import convert
from .llm_client import LLMClient, load_env, AnthropicClient
from .reviewer import ReviewResult, review


@dataclass
class _Exchange:
    """A single LLM call: prompts sent, response received, and client metadata."""

    role: str  # "converter" or "reviewer"
    user_prompt: str
    response: str
    system_prompt: str = ""
    model: str | None = None
    max_tokens: int | None = None


@dataclass
class PipelineResult:
    """Outcome of the full converter-reviewer loop."""

    workflow: str
    approved: bool
    iterations: int
    final_review: ReviewResult


class _RecordingClient:
    """Wraps an LLMClient to record every exchange for the transcript.

    Captures the inner client's ``model`` and ``max_tokens`` attributes
    when present (``AnthropicClient`` exposes both); test fakes that lack
    them simply record ``None``.
    """

    def __init__(self, inner: LLMClient, role: str, exchanges: list[_Exchange]) -> None:
        self._inner = inner
        self._role = role
        self._exchanges = exchanges

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        response = self._inner.complete(system_prompt, user_prompt)
        self._exchanges.append(
            _Exchange(
                role=self._role,
                user_prompt=user_prompt,
                response=response,
                system_prompt=system_prompt,
                model=getattr(self._inner, "model", None),
                max_tokens=getattr(self._inner, "max_tokens", None),
            )
        )
        return response


def run(
    jenkinsfile_text: str,
    output_path: Path | str,
    max_iterations: int = 3,
    converter_client: LLMClient | None = None,
    reviewer_client: LLMClient | None = None,
    dry_run: bool = False,
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
        dry_run: When True, print every prompt sent to the LLM and every
            response received to stdout, but do NOT write the workflow
            or transcript files. Intended for live demos.

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
        if dry_run:
            _print_exchange(exchanges[-1], iteration, max_iterations)

        print(f"[iteration {iteration}/{max_iterations}] Reviewing...", file=sys.stderr)
        result = review(jenkinsfile_text, workflow, client=rev_recorder)
        if dry_run:
            _print_exchange(exchanges[-1], iteration, max_iterations)

        if result.approved:
            print(f"[iteration {iteration}/{max_iterations}] Approved.", file=sys.stderr)
            if not dry_run:
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

    # Exhausted iterations — still write the latest workflow so the user has
    # something to inspect. Approval status is reported via PipelineResult.
    print(
        f"[iteration {max_iterations}/{max_iterations}] Max iterations reached "
        f"without approval.",
        file=sys.stderr,
    )
    if not dry_run:
        _write_output(workflow, output_path)
        _write_transcript(exchanges, output_path)
    return PipelineResult(
        workflow=workflow,
        approved=False,
        iterations=max_iterations,
        final_review=result,
    )


def _print_exchange(ex: _Exchange, iteration: int, max_iterations: int) -> None:
    """Pretty-print a single LLM exchange to stdout for --dry-run demos.

    Includes client metadata (model, max_tokens), the system prompt, the
    user prompt, and the raw response so a viewer can see *everything*
    sent to and received from the LLM.
    """
    role = ex.role.upper()
    header = f"=== [iteration {iteration}/{max_iterations}] {role} ==="
    sep = "-" * len(header)
    model = ex.model if ex.model is not None else "<unknown>"
    max_tokens = ex.max_tokens if ex.max_tokens is not None else "<unknown>"
    print(header)
    print(f"model:      {model}")
    print(f"max_tokens: {max_tokens}")
    print(f"{sep}\n--- system prompt ---\n{sep}")
    print(ex.system_prompt)
    print(f"{sep}\n--- user prompt ---\n{sep}")
    print(ex.user_prompt)
    print(f"{sep}\n--- response from LLM ---\n{sep}")
    print(ex.response)
    print(sep)
    print()


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
        "--model",
        default=AnthropicClient.DEFAULT_MODEL,
        help=f"Model used for both converter and reviewer (default: {AnthropicClient.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print every LLM prompt and response to stdout but do NOT write "
            "the workflow or transcript files. For live demos."
        ),
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
            converter_client=AnthropicClient(model=args.model),
            reviewer_client=AnthropicClient(model=args.model),
            dry_run=args.dry_run,
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
        if args.dry_run:
            print(
                f"Approved after {result.iterations} iteration(s). "
                f"[dry-run: no files written]"
            )
        else:
            print(f"Approved after {result.iterations} iteration(s). Written to {args.output}")
            print(f"Transcript: {transcript}")
    else:
        if args.dry_run:
            print(
                f"Not approved after {result.iterations} iteration(s). "
                f"[dry-run: no files written]",
                file=sys.stderr,
            )
        else:
            print(
                f"Not approved after {result.iterations} iteration(s). "
                f"Latest workflow written to {args.output}",
                file=sys.stderr,
            )
            print(f"Transcript: {transcript}", file=sys.stderr)
        print("Remaining feedback:", file=sys.stderr)
        print(result.final_review.feedback, file=sys.stderr)
        raise SystemExit(1)
