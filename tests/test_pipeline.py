"""Unit tests for jenkins_to_gha.pipeline (no network calls).

Run with::

    python -m unittest tests.test_pipeline
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from jenkins_to_gha import pipeline


VALID_WORKFLOW = """\
name: CI
on:
  push:
    branches: [ main ]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make
"""

VALID_WORKFLOW_V2 = """\
name: CI
on:
  push:
    branches: [ main ]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build
        run: make
"""

JENKINSFILE = "pipeline { agent any; stages { stage('Build') { steps { sh 'make' } } } }"


@dataclass
class SequentialFakeClient:
    """Returns responses from a list in order. Simulates multi-turn conversation."""

    responses: list[str]
    _call_index: int = 0
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        response = self.responses[self._call_index]
        self._call_index += 1
        return response


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._tmpdir = tempfile.mkdtemp()

    @property
    def output_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.yml"

    def test_approved_first_try(self) -> None:
        """Converter produces good output, reviewer approves immediately."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,  # converter
            '{"approved": true, "issues": []}',      # reviewer
        ])
        result = pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        self.assertTrue(result.approved)
        self.assertEqual(result.iterations, 1)
        self.assertTrue(self.output_path.exists())
        self.assertIn("name: CI", self.output_path.read_text())

    def test_approved_after_one_revision(self) -> None:
        """First attempt rejected, second attempt approved."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,                            # converter (attempt 1)
            '{"approved": false, "issues": ["Add step name"]}',     # reviewer (attempt 1)
            VALID_WORKFLOW_V2,                          # converter (attempt 2, with feedback)
            '{"approved": true, "issues": []}',                                 # reviewer (attempt 2)
        ])
        result = pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        self.assertTrue(result.approved)
        self.assertEqual(result.iterations, 2)
        # Verify feedback AND previous workflow were passed to the converter on retry
        second_converter_call = fake.calls[2]  # index 2 = second converter call
        self.assertIn("Add step name", second_converter_call[1])
        self.assertIn("Previous GitHub Actions workflow", second_converter_call[1])
        self.assertIn("name: CI", second_converter_call[1])

    def test_exhausts_max_iterations(self) -> None:
        """Never approved — writes to .unapproved path with warning header."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,                             # converter (1)
            '{"approved": false, "issues": ["Fix A"]}',              # reviewer (1)
            VALID_WORKFLOW,                             # converter (2)
            '{"approved": false, "issues": ["Fix B"]}',              # reviewer (2)
        ])
        result = pipeline.run(
            JENKINSFILE, self.output_path, max_iterations=2, converter_client=fake, reviewer_client=fake
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.iterations, 2)
        self.assertIn("- Fix B", result.final_review.feedback)
        # Must NOT write to the approved output path
        self.assertFalse(self.output_path.exists())
        # Must write to .unapproved path with warning header
        unapproved = self.output_path.with_suffix(".unapproved.yml")
        self.assertTrue(unapproved.exists())
        content = unapproved.read_text()
        self.assertTrue(content.startswith("# FAILED REVIEW"))
        self.assertIn("name: CI", content)

    def test_single_iteration_cap(self) -> None:
        """max_iterations=1 means one convert+review, no retry."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Missing checkout"]}',
        ])
        result = pipeline.run(
            JENKINSFILE, self.output_path, max_iterations=1, converter_client=fake, reviewer_client=fake
        )
        self.assertFalse(result.approved)
        self.assertEqual(result.iterations, 1)
        self.assertEqual(len(fake.calls), 2)  # 1 convert + 1 review

    def test_zero_iterations_raises(self) -> None:
        """max_iterations=0 must fail fast, not crash with UnboundLocalError."""
        fake = SequentialFakeClient(responses=[])
        with self.assertRaises(ValueError):
            pipeline.run(JENKINSFILE, self.output_path, max_iterations=0, converter_client=fake, reviewer_client=fake)

    def test_output_written_to_disk(self) -> None:
        """Verify file is actually written with correct content."""
        fake = SequentialFakeClient(responses=[VALID_WORKFLOW, '{"approved": true, "issues": []}'])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        content = self.output_path.read_text(encoding="utf-8")
        self.assertEqual(content, VALID_WORKFLOW)

    def test_creates_parent_directories(self) -> None:
        """Output path with non-existent parent dirs should be created."""
        nested = Path(self._tmpdir) / "deep" / "nested" / "workflow.yml"
        fake = SequentialFakeClient(responses=[VALID_WORKFLOW, '{"approved": true, "issues": []}'])
        pipeline.run(JENKINSFILE, nested, converter_client=fake, reviewer_client=fake)
        self.assertTrue(nested.exists())


class TranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._tmpdir = tempfile.mkdtemp()

    @property
    def output_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.yml"

    @property
    def transcript_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.transcript.md"

    def test_transcript_written_on_approval(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        self.assertTrue(self.transcript_path.exists())

    def test_transcript_written_on_exhaustion(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix A"]}',
        ])
        pipeline.run(JENKINSFILE, self.output_path, max_iterations=1, converter_client=fake, reviewer_client=fake)
        self.assertTrue(self.transcript_path.exists())

    def test_transcript_contains_iteration_headers(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix A"]}',
            VALID_WORKFLOW_V2,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        content = self.transcript_path.read_text()
        self.assertIn("## Iteration 1", content)
        self.assertIn("## Iteration 2", content)

    def test_transcript_contains_converter_and_reviewer_sections(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        content = self.transcript_path.read_text()
        self.assertIn("### Converter prompt", content)
        self.assertIn("### Converter response", content)
        self.assertIn("### Reviewer prompt", content)
        self.assertIn("### Reviewer response", content)

    def test_transcript_contains_actual_exchanges(self) -> None:
        reviewer_json = '{"approved": true, "issues": []}'
        fake = SequentialFakeClient(responses=[VALID_WORKFLOW, reviewer_json])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        content = self.transcript_path.read_text()
        self.assertIn("name: CI", content)  # converter response
        self.assertIn('"approved": true', content)  # reviewer response
        self.assertIn(JENKINSFILE, content)  # jenkinsfile in prompts


class SeparateClientsTests(unittest.TestCase):
    """Verify converter and reviewer use independent LLM clients."""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._tmpdir = tempfile.mkdtemp()

    @property
    def output_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.yml"

    def test_converter_and_reviewer_use_separate_clients(self) -> None:
        """Each client only receives the calls for its role."""
        converter_fake = SequentialFakeClient(responses=[VALID_WORKFLOW])
        reviewer_fake = SequentialFakeClient(responses=['{"approved": true, "issues": []}'])
        pipeline.run(
            JENKINSFILE,
            self.output_path,
            converter_client=converter_fake,
            reviewer_client=reviewer_fake,
        )
        self.assertEqual(len(converter_fake.calls), 1)
        self.assertEqual(len(reviewer_fake.calls), 1)
        # Converter gets the Jenkinsfile
        self.assertIn(JENKINSFILE, converter_fake.calls[0][1])
        # Reviewer gets the generated workflow
        self.assertIn("name: CI", reviewer_fake.calls[0][1])


class DryRunTests(unittest.TestCase):
    """Verify --dry-run prints exchanges and skips file writes."""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._tmpdir = tempfile.mkdtemp()

    @property
    def output_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.yml"

    def _run_dry(self, fake: SequentialFakeClient, **kwargs) -> tuple[pipeline.PipelineResult, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = pipeline.run(
                JENKINSFILE,
                self.output_path,
                converter_client=fake,
                reviewer_client=fake,
                dry_run=True,
                **kwargs,
            )
        return result, buf.getvalue()

    def test_dry_run_does_not_write_workflow_file(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        result, _ = self._run_dry(fake)
        self.assertTrue(result.approved)
        self.assertEqual(result.iterations, 1)
        self.assertFalse(self.output_path.exists())

    def test_dry_run_does_not_write_transcript(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        self._run_dry(fake)
        transcript = self.output_path.with_suffix(".transcript.md")
        self.assertFalse(transcript.exists())

    def test_dry_run_does_not_write_unapproved_on_exhaustion(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix A"]}',
        ])
        result, _ = self._run_dry(fake, max_iterations=1)
        self.assertFalse(result.approved)
        self.assertFalse(self.output_path.exists())
        self.assertFalse(self.output_path.with_suffix(".unapproved.yml").exists())
        self.assertFalse(self.output_path.with_suffix(".transcript.md").exists())

    def test_dry_run_prints_converter_prompt_and_response(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        _, out = self._run_dry(fake)
        # Section markers
        self.assertIn("CONVERTER", out)
        self.assertIn("REVIEWER", out)
        self.assertIn("system prompt", out)
        self.assertIn("user prompt", out)
        self.assertIn("response from LLM", out)
        # The actual Jenkinsfile (in converter user prompt) and response (workflow) appear
        self.assertIn(JENKINSFILE, out)
        self.assertIn("name: CI", out)
        # The reviewer's JSON response appears
        self.assertIn('"approved": true', out)

    def test_dry_run_prints_system_prompts(self) -> None:
        """Both converter and reviewer system prompts are printed verbatim."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        converter_sys = "SYSTEM-CONV-MARKER You are a converter."
        reviewer_sys = "SYSTEM-REV-MARKER You are a reviewer."
        with mock.patch(
            "jenkins_to_gha.converter.load_system_prompt", return_value=converter_sys
        ), mock.patch(
            "jenkins_to_gha.reviewer.load_system_prompt", return_value=reviewer_sys
        ):
            _, out = self._run_dry(fake)
        self.assertIn(converter_sys, out)
        self.assertIn(reviewer_sys, out)

    def test_dry_run_prints_model_and_max_tokens(self) -> None:
        """Model name and max_tokens of the underlying client are surfaced."""

        @dataclass
        class FakeWithMeta:
            responses: list[str]
            model: str = "fake-model-x1"
            max_tokens: int = 1234
            _i: int = 0
            calls: list[tuple[str, str]] = field(default_factory=list)

            def complete(self, system_prompt: str, user_prompt: str) -> str:
                self.calls.append((system_prompt, user_prompt))
                r = self.responses[self._i]
                self._i += 1
                return r

        fake = FakeWithMeta(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        _, out = self._run_dry(fake)
        self.assertIn("fake-model-x1", out)
        self.assertIn("1234", out)

    def test_dry_run_handles_clients_without_model_metadata(self) -> None:
        """Fakes lacking model/max_tokens still produce readable output."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        _, out = self._run_dry(fake)
        # Falls back to a sentinel rather than raising AttributeError
        self.assertIn("<unknown>", out)

    def test_dry_run_prints_each_iteration(self) -> None:
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix A"]}',
            VALID_WORKFLOW_V2,
            '{"approved": true, "issues": []}',
        ])
        _, out = self._run_dry(fake)
        self.assertIn("[iteration 1/3]", out)
        self.assertIn("[iteration 2/3]", out)
        # Both converter responses present
        self.assertIn("- run: make", out)
        self.assertIn("name: Build", out)


if __name__ == "__main__":
    unittest.main()
