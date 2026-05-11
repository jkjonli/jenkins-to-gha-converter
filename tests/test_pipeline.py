"""Unit tests for jenkins_to_gha.pipeline (no network calls).

Run with::

    python -m unittest tests.test_pipeline
"""
from __future__ import annotations

import os
import tempfile
import unittest
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
        """Never approved — writes the latest workflow to output_path."""
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
        # Spec only requires "write the final workflow to disk" — we honour that
        # regardless of approval. Approval status is reported via PipelineResult.
        self.assertTrue(self.output_path.exists())
        content = self.output_path.read_text()
        self.assertEqual(content, VALID_WORKFLOW)

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

    def test_transcript_response_headers_include_model_and_tokens(self) -> None:
        """Response headers must surface model + token usage when available."""

        @dataclass
        class MeteredFakeClient:
            responses: list[str]
            model: str = "claude-opus-4-7"
            last_input_tokens: int | None = None
            last_output_tokens: int | None = None
            _i: int = 0

            def complete(self, system_prompt: str, user_prompt: str) -> str:
                resp = self.responses[self._i]
                self._i += 1
                # Simulate the SDK populating usage after each call.
                self.last_input_tokens = 100 + self._i
                self.last_output_tokens = 200 + self._i
                return resp

        converter = MeteredFakeClient(responses=[VALID_WORKFLOW])
        reviewer = MeteredFakeClient(
            responses=['{"approved": true, "issues": []}'],
            model="claude-haiku-4-5-20251001",
        )
        pipeline.run(
            JENKINSFILE,
            self.output_path,
            converter_client=converter,
            reviewer_client=reviewer,
        )
        content = self.transcript_path.read_text()
        self.assertIn(
            "### Converter response (model: claude-opus-4-7, "
            "input_tokens: 101, output_tokens: 201)",
            content,
        )
        self.assertIn(
            "### Reviewer response (model: claude-haiku-4-5-20251001, "
            "input_tokens: 101, output_tokens: 201)",
            content,
        )

    def test_transcript_response_headers_omit_unknown_metadata(self) -> None:
        """Fake clients with no model/tokens leave the headers unannotated."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(JENKINSFILE, self.output_path, converter_client=fake, reviewer_client=fake)
        content = self.transcript_path.read_text()
        # No parenthetical when metadata is absent.
        self.assertIn("### Converter response\n", content)
        self.assertIn("### Reviewer response\n", content)
        self.assertNotIn("### Converter response (", content)
        self.assertNotIn("### Reviewer response (", content)

    def test_transcript_contains_both_system_prompts(self) -> None:
        """The transcript must include the converter and reviewer system
        prompts verbatim so a demo viewer can see exactly what each agent
        was told."""
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
            pipeline.run(
                JENKINSFILE,
                self.output_path,
                converter_client=fake,
                reviewer_client=fake,
            )
        content = self.transcript_path.read_text()
        self.assertIn("### Converter system prompt", content)
        self.assertIn("### Reviewer system prompt", content)
        self.assertIn(converter_sys, content)
        self.assertIn(reviewer_sys, content)


class IterationModeTests(unittest.TestCase):
    """Pipeline must mark the LAST review call as the FINAL iteration and
    every earlier review call as non-final, so the reviewer can apply
    different bars (thorough vs critical-only) without a separate signal."""

    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self._tmpdir = tempfile.mkdtemp()

    @property
    def output_path(self) -> Path:
        return Path(self._tmpdir) / "workflow.yml"

    def test_single_iteration_is_final(self) -> None:
        """When max_iterations=1, the only review call is the final one."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(
            JENKINSFILE,
            self.output_path,
            max_iterations=1,
            converter_client=fake,
            reviewer_client=fake,
        )
        # calls[1] is the (only) reviewer call.
        reviewer_prompt = fake.calls[1][1]
        self.assertIn("FINAL iteration", reviewer_prompt)

    def test_only_last_review_is_final_when_loop_exhausts(self) -> None:
        """Across max_iterations review calls, only the LAST is FINAL."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix A"]}',
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix B"]}',
            VALID_WORKFLOW,
            '{"approved": false, "issues": ["Fix C"]}',
        ])
        pipeline.run(
            JENKINSFILE,
            self.output_path,
            max_iterations=3,
            converter_client=fake,
            reviewer_client=fake,
        )
        # Reviewer calls are at odd indices: 1, 3, 5.
        first_review = fake.calls[1][1]
        second_review = fake.calls[3][1]
        third_review = fake.calls[5][1]
        self.assertIn("Iterative", first_review)
        self.assertNotIn("FINAL iteration", first_review)
        self.assertIn("Iterative", second_review)
        self.assertNotIn("FINAL iteration", second_review)
        self.assertIn("FINAL iteration", third_review)
        self.assertNotIn("Iterative", third_review)

    def test_early_approval_does_not_force_final_mode(self) -> None:
        """If the reviewer approves on iteration 1 of 3, that call was
        sent in non-final (iterative) mode — the loop didn't know it
        would be the last one."""
        fake = SequentialFakeClient(responses=[
            VALID_WORKFLOW,
            '{"approved": true, "issues": []}',
        ])
        pipeline.run(
            JENKINSFILE,
            self.output_path,
            max_iterations=3,
            converter_client=fake,
            reviewer_client=fake,
        )
        reviewer_prompt = fake.calls[1][1]
        self.assertIn("Iterative", reviewer_prompt)
        self.assertNotIn("FINAL iteration", reviewer_prompt)


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


if __name__ == "__main__":
    unittest.main()
