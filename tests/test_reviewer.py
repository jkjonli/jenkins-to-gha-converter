"""Unit tests for jenkins_to_gha.reviewer (no network calls).

Run with either::

    python -m unittest tests.test_reviewer
    python -m tests.test_reviewer
"""
from __future__ import annotations

import os
import unittest
from dataclasses import dataclass, field
from unittest import mock

from jenkins_to_gha import reviewer


@dataclass
class FakeClient:
    """Test double that returns a canned response and records calls."""

    response: str
    last_system_prompt: str = ""
    last_user_prompt: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.calls.append((system_prompt, user_prompt))
        return self.response


SAMPLE_JENKINSFILE = "pipeline { agent any; stages { stage('Build') { steps { sh 'make' } } } }"

SAMPLE_WORKFLOW = """\
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


class ParseVerdictTests(unittest.TestCase):
    def test_approved_json(self) -> None:
        result = reviewer._parse_verdict('{"approved": true, "issues": []}')
        self.assertTrue(result.approved)
        self.assertEqual(result.feedback, "")

    def test_not_approved_json(self) -> None:
        raw = '{"approved": false, "issues": ["Fix checkout step", "Add needs: build"]}'
        result = reviewer._parse_verdict(raw)
        self.assertFalse(result.approved)
        self.assertIn("Fix checkout step", result.feedback)
        self.assertIn("Add needs: build", result.feedback)

    def test_approved_with_whitespace(self) -> None:
        result = reviewer._parse_verdict('  {"approved": true, "issues": []}  \n')
        self.assertTrue(result.approved)

    def test_json_in_markdown_fence(self) -> None:
        raw = '```json\n{"approved": false, "issues": ["Missing checkout"]}\n```'
        result = reviewer._parse_verdict(raw)
        self.assertFalse(result.approved)
        self.assertIn("Missing checkout", result.feedback)

    def test_json_in_bare_fence(self) -> None:
        raw = '```\n{"approved": true, "issues": []}\n```'
        result = reviewer._parse_verdict(raw)
        self.assertTrue(result.approved)

    def test_freeform_response_treated_as_feedback(self) -> None:
        raw = "The workflow has problems:\n- Missing checkout"
        result = reviewer._parse_verdict(raw)
        self.assertFalse(result.approved)
        self.assertIn("Missing checkout", result.feedback)

    def test_issues_formatted_as_bullet_list(self) -> None:
        raw = '{"approved": false, "issues": ["Issue A", "Issue B"]}'
        result = reviewer._parse_verdict(raw)
        self.assertEqual(result.feedback, "- Issue A\n- Issue B")

    def test_null_issues(self) -> None:
        result = reviewer._parse_verdict('{"approved": true, "issues": null}')
        self.assertTrue(result.approved)
        self.assertEqual(result.feedback, "")

    def test_missing_issues_key(self) -> None:
        result = reviewer._parse_verdict('{"approved": false}')
        self.assertFalse(result.approved)
        self.assertEqual(result.feedback, "")

    def test_missing_approved_key(self) -> None:
        result = reviewer._parse_verdict('{"issues": ["Fix X"]}')
        self.assertFalse(result.approved)
        self.assertIn("Fix X", result.feedback)

    def test_non_string_issues(self) -> None:
        result = reviewer._parse_verdict('{"approved": false, "issues": [42, null, "Fix Y"]}')
        self.assertFalse(result.approved)
        self.assertIn("42", result.feedback)
        self.assertIn("Fix Y", result.feedback)
        self.assertNotIn("None", result.feedback)

    def test_empty_json_object(self) -> None:
        result = reviewer._parse_verdict('{}')
        self.assertFalse(result.approved)
        self.assertEqual(result.feedback, "")


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        env_patch = mock.patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_review_approved(self) -> None:
        fake = FakeClient(response='{"approved": true, "issues": []}')
        result = reviewer.review(SAMPLE_JENKINSFILE, SAMPLE_WORKFLOW, client=fake)
        self.assertTrue(result.approved)
        self.assertEqual(result.feedback, "")

    def test_review_changes_requested(self) -> None:
        fake = FakeClient(
            response='{"approved": false, "issues": ["Add checkout@v4"]}'
        )
        result = reviewer.review(SAMPLE_JENKINSFILE, SAMPLE_WORKFLOW, client=fake)
        self.assertFalse(result.approved)
        self.assertIn("checkout@v4", result.feedback)

    def test_review_passes_both_inputs(self) -> None:
        fake = FakeClient(response='{"approved": true, "issues": []}')
        reviewer.review(SAMPLE_JENKINSFILE, SAMPLE_WORKFLOW, client=fake)
        self.assertIn(SAMPLE_JENKINSFILE, fake.last_user_prompt)
        self.assertIn("```groovy", fake.last_user_prompt)
        self.assertIn("```yaml", fake.last_user_prompt)
        self.assertIn("name: CI", fake.last_user_prompt)

    def test_review_loads_system_prompt_from_disk(self) -> None:
        fake = FakeClient(response='{"approved": true, "issues": []}')
        reviewer.review(SAMPLE_JENKINSFILE, SAMPLE_WORKFLOW, client=fake)
        self.assertEqual(fake.last_system_prompt, reviewer.load_system_prompt())

    def test_review_accepts_injected_system_prompt(self) -> None:
        fake = FakeClient(response='{"approved": true, "issues": []}')
        custom = "TEST-ONLY: review this workflow."
        reviewer.review(
            SAMPLE_JENKINSFILE, SAMPLE_WORKFLOW, client=fake, system_prompt=custom
        )
        self.assertEqual(fake.last_system_prompt, custom)
        self.assertNotEqual(fake.last_system_prompt, reviewer.load_system_prompt())

if __name__ == "__main__":
    unittest.main()
