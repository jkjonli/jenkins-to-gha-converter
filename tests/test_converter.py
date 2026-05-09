"""Unit tests for src.converter post-processing (no network calls).

Run with either::

    python -m unittest tests.test_converter
    python -m tests.test_converter
"""
from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from unittest import mock

from src import converter


@dataclass
class FakeClient:
    """Test double for LLMClient that returns a canned response and
    records the last (system_prompt, user_prompt) pair it was called with."""

    response: str
    last_system_prompt: str = ""
    last_user_prompt: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.calls.append((system_prompt, user_prompt))
        return self.response


VALID_WORKFLOW = """name: CI
on:
  push:
    branches: [ main ]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./gradlew --no-daemon clean build
"""


class StripFencesTests(unittest.TestCase):
    def test_strips_yaml_fence(self) -> None:
        raw = "```yaml\n" + VALID_WORKFLOW.rstrip() + "\n```"
        out = converter._strip_code_fences(raw)
        self.assertTrue(out.startswith("name: CI"))
        self.assertTrue(out.endswith("\n"))
        self.assertNotIn("```", out)

    def test_strips_bare_fence(self) -> None:
        raw = "```\n" + VALID_WORKFLOW.rstrip() + "\n```\n"
        out = converter._strip_code_fences(raw)
        self.assertTrue(out.startswith("name: CI"))
        self.assertNotIn("```", out)

    def test_preserves_plain_yaml(self) -> None:
        out = converter._strip_code_fences(VALID_WORKFLOW)
        self.assertEqual(out.rstrip() + "\n", VALID_WORKFLOW)


class ShapeAssertionTests(unittest.TestCase):
    """Parameterized via unittest.subTest (Meszaros: avoid Test Code Duplication
    while keeping the stdlib-only constraint of this project)."""

    VALID_FIRST_LINES = [
        ("name first", VALID_WORKFLOW),
        ("on first", "on:\n  push: {}\n"),
        ("single-quoted on first", "'on':\n  push: {}\n"),
        ("double-quoted on first", '"on":\n  push: {}\n'),
        ("comment then name", "# generated\nname: CI\n"),
    ]

    INVALID_FIRST_LINES = [
        ("jobs first (no name/on)", "jobs:\n  build: {}\n"),
        ("prose preface", "Sure, here is your workflow:\nname: CI\n"),
        ("empty string", ""),
        ("whitespace only", "   \n\n\t\n"),
    ]

    def test_accepts_valid_first_lines(self) -> None:
        for label, yaml_text in self.VALID_FIRST_LINES:
            with self.subTest(case=label):
                # AAA: Arrange (yaml_text); Act (call); Assert (must not raise).
                try:
                    converter._assert_workflow_shape(yaml_text)
                except ValueError as exc:
                    self.fail(
                        f"_assert_workflow_shape unexpectedly raised on {label!r}: {exc}"
                    )

    def test_rejects_invalid_first_lines(self) -> None:
        for label, yaml_text in self.INVALID_FIRST_LINES:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    converter._assert_workflow_shape(yaml_text)


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        # Defensive isolation: convert() falls back to load_env() when no
        # client is injected, which mutates os.environ. patch.dict snapshots
        # the current state and restores it on cleanup, regardless of what
        # any test (or load_env) does in between. clear=False keeps PATH/
        # HOME/etc. visible to anything that needs them.
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_convert_returns_stripped_yaml(self) -> None:
        fake = FakeClient(response="```yaml\n" + VALID_WORKFLOW.rstrip() + "\n```")
        out = converter.convert("pipeline { agent any }", client=fake)
        self.assertTrue(out.startswith("name: CI"))
        self.assertNotIn("```", out)

    def test_convert_passes_jenkinsfile_to_user_message(self) -> None:
        fake = FakeClient(response=VALID_WORKFLOW)
        jf = "pipeline { agent any; stages { stage('X') { steps { sh 'echo hi' } } } }"
        converter.convert(jf, client=fake)
        self.assertIn(jf, fake.last_user_prompt)
        self.assertIn("```groovy", fake.last_user_prompt)
        self.assertNotIn("Reviewer feedback", fake.last_user_prompt)

    def test_convert_includes_feedback_when_provided(self) -> None:
        fake = FakeClient(response=VALID_WORKFLOW)
        converter.convert("pipeline { agent any }", feedback="- Use checkout@v4", client=fake)
        self.assertIn("Reviewer feedback", fake.last_user_prompt)
        self.assertIn("- Use checkout@v4", fake.last_user_prompt)

    def test_convert_rejects_non_workflow_response(self) -> None:
        fake = FakeClient(response="Sorry, I cannot do that.")
        with self.assertRaises(ValueError):
            converter.convert("pipeline { agent any }", client=fake)

    def test_convert_loads_system_prompt_from_disk(self) -> None:
        fake = FakeClient(response=VALID_WORKFLOW)
        converter.convert("pipeline { agent any }", client=fake)
        # Assert the disk-loaded prompt is forwarded verbatim. We compare
        # against load_system_prompt() rather than hard-coded English
        # phrases so prompt rewordings do not break this test.
        self.assertEqual(fake.last_system_prompt, converter.load_system_prompt())

    def test_convert_accepts_injected_system_prompt(self) -> None:
        """DI seam: an explicit system_prompt must be passed through verbatim
        without touching the disk-loaded prompt."""
        fake = FakeClient(response=VALID_WORKFLOW)
        custom_prompt = "TEST-ONLY system prompt: emit a valid workflow."
        converter.convert(
            "pipeline { agent any }",
            client=fake,
            system_prompt=custom_prompt,
        )
        self.assertEqual(fake.last_system_prompt, custom_prompt)
        self.assertNotEqual(fake.last_system_prompt, converter.load_system_prompt())

    # ---- Edge cases (Ostrand & Balcer 1988: Category-Partition) -----------

    def test_convert_handles_empty_jenkinsfile(self) -> None:
        """convert(\"\") must propagate the empty source to the user prompt and
        return whatever shape-valid YAML the (faked) LLM produces, not crash."""
        fake = FakeClient(response=VALID_WORKFLOW)
        out = converter.convert("", client=fake)
        self.assertTrue(out.startswith("name: CI"))
        # The empty groovy block must still be present in the user prompt so
        # the LLM can see that the source was intentionally empty.
        self.assertIn("```groovy\n\n```", fake.last_user_prompt)

    def test_convert_treats_empty_feedback_like_none(self) -> None:
        """feedback=\"\" is falsy and must not introduce a Reviewer feedback
        section, identical to the feedback=None default."""
        fake = FakeClient(response=VALID_WORKFLOW)
        converter.convert("pipeline { agent any }", feedback="", client=fake)
        self.assertNotIn("Reviewer feedback", fake.last_user_prompt)

    def test_convert_rejects_whitespace_only_response(self) -> None:
        """A degenerate LLM response (whitespace only) must surface as
        ValueError from the shape check, not silently succeed."""
        fake = FakeClient(response="   \n\n\t\n")
        with self.assertRaises(ValueError):
            converter.convert("pipeline { agent any }", client=fake)


if __name__ == "__main__":
    unittest.main()
