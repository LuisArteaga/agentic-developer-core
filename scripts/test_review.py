#!/usr/bin/env python3
"""Unit tests for scripts/review.py findings and reasoning parsing."""

import json
import sys
import unittest
from pathlib import Path

# Make review.py importable from the same directory.
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))

import review


class ParseFindingsTests(unittest.TestCase):
    def _build_response(self, content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    def test_valid_finding_inside_block_retracted_outside_is_ignored(self):
        """AC: valid JSON inside <findings>; retracted JSON/text outside is ignored."""
        raw = self._build_response(
            "Hmm, I see something.\n"
            '{"severity": "bug", "message": "not really a bug, retracted"}\n'
            "Wait, that's wrong. Let me reconsider.\n"
            "<reasoning>This is the reasoning</reasoning>\n"
            "<findings>\n"
            '{"severity": "security", "message": "hardcoded secret in config"}\n'
            "</findings>\n"
        )
        verdict, reasoning, findings = review.evaluate_response(raw)
        self.assertEqual(verdict, "Fail")
        self.assertEqual(reasoning, "This is the reasoning")
        self.assertEqual(findings, ["security|hardcoded secret in config"])

    def test_empty_findings_block_passes(self):
        """AC: empty <findings> block results in Pass."""
        raw = self._build_response(
            "<reasoning>Some thinking text</reasoning>\n<findings>\n</findings>\n"
        )
        verdict, reasoning, findings = review.evaluate_response(raw)
        self.assertEqual(verdict, "Pass")
        self.assertEqual(reasoning, "Some thinking text")
        self.assertEqual(findings, [])

    def test_missing_tags_needs_review(self):
        """Edge case: no <findings> and no <reasoning> tags -> treat as Needs Review."""
        raw = self._build_response(
            "Draft: I think there is a bug.\n"
            '{"severity": "bug", "message": "maybe"}\n'
        )
        verdict, reasoning, findings = review.evaluate_response(raw)
        self.assertEqual(verdict, "Needs Review")
        self.assertIn("Response lacks both", reasoning)
        self.assertEqual(findings, [])

    def test_multiple_findings_blocks_uses_last(self):
        """Edge case: multiple blocks; only last block is parsed."""
        raw = self._build_response(
            "<reasoning>thinking</reasoning>\n"
            "<findings>\n"
            '{"severity": "bug", "message": "first block"}\n'
            "</findings>\n"
            "Wait, let me correct.\n"
            "<findings>\n"
            '{"severity": "bug", "message": "second block"}\n'
            "</findings>\n"
        )
        verdict, reasoning, findings = review.evaluate_response(raw)
        self.assertEqual(verdict, "Fail")
        self.assertEqual(findings, ["bug|second block"])

    def test_malformed_xml_no_closing_tag_extracts_to_end(self):
        """Edge case: missing closing tag extracts from opening to end."""
        raw = self._build_response(
            "<reasoning>reasoning block without close\n"
            "<findings>\n"
            '{"severity": "bug", "message": "no closing tag"}\n'
            "Some trailing text"
        )
        verdict, reasoning, findings = review.evaluate_response(raw)
        self.assertEqual(verdict, "Fail")
        self.assertEqual(findings, ["bug|no closing tag"])


class SystemPromptTests(unittest.TestCase):
    def test_prompt_instructs_findings_xml_block(self):
        """AC: system prompts instruct LLM to wrap findings and reasoning in tags."""
        self.assertIn("<findings>", review.SYSTEM_PROMPT_SECURITY)
        self.assertIn("</findings>", review.SYSTEM_PROMPT_SECURITY)
        self.assertIn("<reasoning>", review.SYSTEM_PROMPT_SECURITY)
        self.assertIn("</reasoning>", review.SYSTEM_PROMPT_SECURITY)

        self.assertIn("<findings>", review.SYSTEM_PROMPT_ARCH)
        self.assertIn("</findings>", review.SYSTEM_PROMPT_ARCH)
        self.assertIn("<reasoning>", review.SYSTEM_PROMPT_ARCH)
        self.assertIn("</reasoning>", review.SYSTEM_PROMPT_ARCH)


if __name__ == "__main__":
    unittest.main()
