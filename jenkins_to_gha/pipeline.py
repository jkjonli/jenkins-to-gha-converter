"""Agentic loop: converter <-> reviewer until approved or max iterations.

Public API::

    run(jenkinsfile_text, output_path, max_iterations=3,
        converter_client=None, reviewer_client=None) -> PipelineResult

CLI::

    python -m jenkins_to_gha.pipeline <Jenkinsfile> <output.yml> [--max-iterations N]
        [--converter-model MODEL] [--reviewer-model MODEL]
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .converter import convert
from .llm_client import LLMClient, load_env, AnthropicClient
from .reviewer import ReviewResult, review


_MINIMAL_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "converter_system_minimal.md"
)


def _load_minimal_system_prompt() -> str:
    """Read the deliberately minimal converter system prompt used by the
    ``--less-than-ideal`` demo on iteration 1.

    The minimal prompt omits guidance that would normally prevent
    fidelity bugs (env scope, parallelism, container blocks,
    pipeline-post terminal jobs, artifact naming, execution-model
    differences). The model produces multiple natural defects that the
    reviewer catches; iteration 2 (using the full prompt + reviewer
    feedback) corrects them. This is preferred over a single-defect
    sabotage instruction because the reviewer too easily hallucinates
    a single missing element into its `checked` walkthrough and
    approves anyway.
    """
    if not _MINIMAL_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Minimal converter prompt not found at {_MINIMAL_PROMPT_PATH}. "
            "Required for --less-than-ideal demo mode."
        )
    return _MINIMAL_PROMPT_PATH.read_text(encoding="utf-8")


@dataclass
class _Exchange:
    """A single LLM call: role, prompts sent, response received, and
    best-effort metadata (model name + token usage) snapshotted from the
    client after the call. Metadata fields are ``None`` for clients that
    don't expose them (e.g. test fakes).
    """

    role: str  # "converter" or "reviewer"
    system_prompt: str
    user_prompt: str
    response: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


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
            _Exchange(
                role=self._role,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response=response,
                model=getattr(self._inner, "model", None),
                input_tokens=getattr(self._inner, "last_input_tokens", None),
                output_tokens=getattr(self._inner, "last_output_tokens", None),
            )
        )
        return response


def run(
    jenkinsfile_text: str,
    output_path: Path | str,
    max_iterations: int = 3,
    converter_client: LLMClient | None = None,
    reviewer_client: LLMClient | None = None,
    less_than_ideal: bool = False,
) -> PipelineResult:
    """Run the converter-reviewer loop and write the final workflow to disk.

    Args:
        jenkinsfile_text: Raw Jenkinsfile source.
        output_path: Where to write the final workflow YAML.
        max_iterations: Cap on converter-reviewer rounds (default 3).
        converter_client: LLM backend for the converter. Defaults to
            ``AnthropicClient()`` (claude-opus-4-7).
        reviewer_client: LLM backend for the reviewer. Defaults to
            ``AnthropicClient()``. Use a different model for adversarial
            review (e.g. ``AnthropicClient(model="claude-haiku-4-5-20251001")``).
        less_than_ideal: Demo flag. When ``True``, iteration 1 of the
            converter receives a deliberately minimal system prompt
            (loaded from ``prompts/converter_system_minimal.md``) that
            omits guidance on env scope, parallelism, container blocks,
            pipeline-post terminal jobs, artifact naming, and
            execution-model differences. The model produces multiple
            natural defects that the reviewer catches; iteration 2
            (using the full disk prompt + reviewer feedback) corrects
            them. Useful for end-to-end demos of the feedback loop.

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
    # Load the minimal prompt once so a disk read failure surfaces
    # immediately, not on iteration 1.
    minimal_prompt = _load_minimal_system_prompt() if less_than_ideal else None

    for iteration in range(1, max_iterations + 1):
        first_iteration = iteration == 1
        iteration_system_prompt = (
            minimal_prompt if (first_iteration and less_than_ideal) else None
        )
        prompt_tag = (
            " (minimal prompt)" if iteration_system_prompt is not None else ""
        )
        print(
            f"[iteration {iteration}/{max_iterations}] Converting{prompt_tag}...",
            file=sys.stderr,
        )
        workflow = convert(
            jenkinsfile_text,
            feedback=feedback,
            client=conv_recorder,
            previous_workflow=previous_workflow,
            system_prompt=iteration_system_prompt,
        )

        print(f"[iteration {iteration}/{max_iterations}] Reviewing...", file=sys.stderr)
        result = review(jenkinsfile_text, workflow, client=rev_recorder)

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

    # Exhausted iterations — still write the latest workflow so the user has
    # something to inspect. Approval status is reported via PipelineResult.
    print(
        f"[iteration {max_iterations}/{max_iterations}] Max iterations reached "
        f"without approval.",
        file=sys.stderr,
    )
    _write_output(workflow, output_path)
    _write_transcript(exchanges, output_path)
    return PipelineResult(
        workflow=workflow,
        approved=False,
        iterations=max_iterations,
        final_review=result,
    )


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


def _response_header_suffix(exchange: _Exchange) -> str:
    """Build the parenthetical ``(model: X, input_tokens: N, ...)`` suffix
    for a response header. Returns an empty string when no metadata is
    available so clients that don't surface model/usage (e.g. test fakes)
    produce clean, unannotated headers.
    """
    fields: list[str] = []
    if exchange.model is not None:
        fields.append(f"model: {exchange.model}")
    if exchange.input_tokens is not None:
        fields.append(f"input_tokens: {exchange.input_tokens}")
    if exchange.output_tokens is not None:
        fields.append(f"output_tokens: {exchange.output_tokens}")
    if not fields:
        return ""
    return " (" + ", ".join(fields) + ")"


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
            lines.append("### Converter system prompt")
            lines.append("")
            lines.append(ex.system_prompt.rstrip("\n"))
            lines.append("")
            lines.append("### Converter prompt")
            lines.append("")
            lines.append(ex.user_prompt)
            lines.append("")
            lines.append(f"### Converter response{_response_header_suffix(ex)}")
            lines.append("")
            lines.append("```yaml")
            lines.append(ex.response.rstrip("\n"))
            lines.append("```")
            lines.append("")
        else:
            lines.append("### Reviewer system prompt")
            lines.append("")
            lines.append(ex.system_prompt.rstrip("\n"))
            lines.append("")
            lines.append("### Reviewer prompt")
            lines.append("")
            lines.append(ex.user_prompt)
            lines.append("")
            lines.append(f"### Reviewer response{_response_header_suffix(ex)}")
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
        help=f"Model used for the converter agent (default: {AnthropicClient.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--reviewer-model",
        default=AnthropicClient.DEFAULT_MODEL,
        help=f"Model used for the reviewer agent (default: {AnthropicClient.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--less-than-ideal",
        action="store_true",
        help=(
            "Demo mode: iteration 1 uses a deliberately minimal converter "
            "system prompt (prompts/converter_system_minimal.md) that omits "
            "guidance on env scope, parallelism, container blocks, post-job "
            "handling, artifact naming, and execution-model differences. The "
            "model produces multiple natural defects the reviewer catches; "
            "iteration 2 reverts to the full prompt and corrects them based "
            "on reviewer feedback. Requires --max-iterations >= 2 to "
            "demonstrate convergence."
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
            converter_client=AnthropicClient(model=args.converter_model),
            reviewer_client=AnthropicClient(model=args.reviewer_model),
            less_than_ideal=args.less_than_ideal,
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
        print(
            f"Not approved after {result.iterations} iteration(s). "
            f"Latest workflow written to {args.output}",
            file=sys.stderr,
        )
        print(f"Transcript: {transcript}", file=sys.stderr)
        print("Remaining feedback:", file=sys.stderr)
        print(result.final_review.feedback, file=sys.stderr)
        raise SystemExit(1)
