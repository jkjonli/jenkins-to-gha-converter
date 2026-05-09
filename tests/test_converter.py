"""Unit tests for src.converter post-processing (no network calls).

Run with either::

    python -m unittest tests.test_converter
    python -m tests.test_converter
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field

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
    def test_accepts_name_first(self) -> None:
        converter._assert_workflow_shape(VALID_WORKFLOW)

    def test_accepts_on_first(self) -> None:
        converter._assert_workflow_shape("on:\n  push: {}\n")

    def test_accepts_quoted_on_first(self) -> None:
        converter._assert_workflow_shape("'on':\n  push: {}\n")
        converter._assert_workflow_shape('"on":\n  push: {}\n')

    def test_skips_leading_comments(self) -> None:
        converter._assert_workflow_shape("# generated\nname: CI\n")

    def test_rejects_non_workflow(self) -> None:
        with self.assertRaises(ValueError):
            converter._assert_workflow_shape("jobs:\n  build: {}\n")
        with self.assertRaises(ValueError):
            converter._assert_workflow_shape("Sure, here is your workflow:\nname: CI\n")


class ConvertTests(unittest.TestCase):
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
        # System prompt should contain a key phrase from our authored file.
        self.assertIn("GitHub Actions", fake.last_system_prompt)
        self.assertIn("Mapping table", fake.last_system_prompt)


if __name__ == "__main__":
    unittest.main()
