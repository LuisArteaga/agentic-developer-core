#!/usr/bin/env python3
"""Unit tests for scripts/review.py findings and reasoning parsing."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make review.py importable from the same directory.
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))

import review  # noqa: E402


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
            'Draft: I think there is a bug.\n{"severity": "bug", "message": "maybe"}\n'
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

        self.assertIn("<findings>", review.SYSTEM_PROMPT_SYNTAX_LINT)
        self.assertIn("<reasoning>", review.SYSTEM_PROMPT_TEST_COVERAGE)


def _build_judges_data(
    statuses,
    findings=None,
    errors=None,
    reasoning="ok",
    fallbacks=None,
    final_models=None,
):
    """Helper to assemble a judges_data dict for build_review_body tests."""
    data = {}
    for key in review.JUDGE_KEYS:
        data[key] = {
            "name": review.JUDGE_DISPLAY_NAMES[key],
            "prompt": review.JUDGE_PROMPTS[key],
            "status": statuses.get(key, "PASS"),
            "reasoning": reasoning,
            "findings": (findings or {}).get(key, []),
            "error": (errors or {}).get(key, None),
            "used_fallback": (fallbacks or {}).get(key, False),
            "final_model": (final_models or {}).get(key, None),
        }
    return data


class BuildReviewBodyTests(unittest.TestCase):
    def test_build_review_body_all_pass(self):
        """AC: all 4 PASS -> hidden block lists 4 'KEY: PASS' lines + summary table."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        data = _build_judges_data(statuses)
        body = review.build_review_body(data)
        self.assertIn("### 🤖 Automated LLM PR Judges Summary", body)
        self.assertIn("| Judge | Status | Details |", body)
        for key in review.JUDGE_KEYS:
            self.assertIn(f"{key}: PASS\n", body)
        self.assertIn("<!-- llm-pr-review-verdicts", body)
        self.assertIn("-->", body)

    def test_build_review_body_mixed(self):
        """AC: one FAIL (findings), one NEEDS REVIEW (error), two PASS -> exact statuses."""
        statuses = {
            "syntax_lint": "PASS",
            "test_coverage": "FAIL",
            "architecture": "PASS",
            "security": "NEEDS REVIEW",
        }
        findings = {"test_coverage": ["error|[Q1] missing tests"]}
        errors = {"security": "boom"}
        data = _build_judges_data(statuses, findings=findings, errors=errors)
        body = review.build_review_body(data)
        self.assertIn("syntax_lint: PASS\n", body)
        self.assertIn("test_coverage: FAIL\n", body)
        self.assertIn("architecture: PASS\n", body)
        self.assertIn("security: NEEDS REVIEW\n", body)
        self.assertIn("1 violation found.", body)
        self.assertIn("Check failed to run: boom", body)

    def test_build_review_body_empty_diff(self):
        """AC: empty-diff outcome -> 4 PASS lines."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        data = _build_judges_data(statuses, reasoning="")
        body = review.build_review_body(data)
        for key in review.JUDGE_KEYS:
            self.assertIn(f"{key}: PASS\n", body)


class ClipChunkTests(unittest.TestCase):
    def test_clip_chunk_under_budget_unchanged(self):
        """AC: chunk under budget is returned unchanged."""
        chunk = "short chunk content"
        self.assertEqual(review.clip_chunk(chunk, 1000), chunk)

    def test_clip_chunk_over_budget_appends_note(self):
        """AC: chunk > budget chars clipped with NOTE appended."""
        budget = 100
        chunk = "x" * (budget + 50)
        result = review.clip_chunk(chunk, budget)
        self.assertTrue(result.startswith("x" * budget))
        self.assertIn(f"[NOTE: diff truncated to {budget} chars", result)
        self.assertIn("return NEEDS REVIEW if you cannot fully evaluate", result)


class SplitDiffByFileTests(unittest.TestCase):
    def test_empty_diff_returns_empty_list(self):
        """AC: empty or whitespace-only diff → empty list."""
        self.assertEqual(review.split_diff_by_file(""), [])
        self.assertEqual(review.split_diff_by_file("   \n  "), [])

    def test_single_file_diff(self):
        """AC: single file diff → one (filename, section) pair."""
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+added\n"
            " line3\n"
        )
        chunks = review.split_diff_by_file(diff)
        self.assertEqual(len(chunks), 1)
        filename, section = chunks[0]
        self.assertEqual(filename, "foo.py")
        self.assertIn("diff --git a/foo.py b/foo.py", section)

    def test_multi_file_diff_preserves_order(self):
        """AC: multi-file diff → chunks in natural git diff order."""
        diff = (
            "diff --git a/alpha.py b/alpha.py\n"
            "--- a/alpha.py\n"
            "+++ b/alpha.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "diff --git a/beta.py b/beta.py\n"
            "--- a/beta.py\n"
            "+++ b/beta.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        chunks = review.split_diff_by_file(diff)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "alpha.py")
        self.assertEqual(chunks[1][0], "beta.py")

    def test_renamed_file_extracts_destination(self):
        """AC: renamed file → destination filename (b/ path)."""
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "+y\n"
        )
        chunks = review.split_diff_by_file(diff)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "new_name.py")

    def test_new_file_mode(self):
        """AC: new file mode → included as a chunk with correct filename."""
        diff = (
            "diff --git a/new_file.py b/new_file.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new_file.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line1\n"
            "+line2\n"
        )
        chunks = review.split_diff_by_file(diff)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], "new_file.py")


class PackIntoBatchesTests(unittest.TestCase):
    def test_empty_chunks_returns_empty_list(self):
        """AC: no chunks → no batches."""
        self.assertEqual(review.pack_into_batches([], 1000), [])

    def test_single_chunk_under_budget_one_batch(self):
        """AC: single chunk under budget → one batch."""
        chunks = [("foo.py", "diff --git ...")]
        batches = review.pack_into_batches(chunks, 1000)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], "diff --git ...")

    def test_multiple_small_files_pack_into_one_batch(self):
        """AC: multiple small files fit in one batch."""
        chunks = [
            ("a.py", "section_a"),
            ("b.py", "section_b"),
        ]
        batches = review.pack_into_batches(chunks, 1000)
        self.assertEqual(len(batches), 1)
        self.assertIn("section_a", batches[0])
        self.assertIn("section_b", batches[0])

    def test_overflow_creates_new_batch(self):
        """AC: adding a file that overflows starts a new batch."""
        chunks = [
            ("a.py", "x" * 60),
            ("b.py", "x" * 60),  # 60 + 1 + 60 = 121 > 100
        ]
        batches = review.pack_into_batches(chunks, 100)
        self.assertEqual(len(batches), 2)

    def test_oversized_single_file_clipped(self):
        """AC: a single file exceeding budget is clipped and gets its own batch."""
        big_section = "x" * 200
        chunks = [("big.py", big_section)]
        batches = review.pack_into_batches(chunks, 100)
        self.assertEqual(len(batches), 1)
        self.assertIn("[NOTE: diff truncated to 100 chars", batches[0])

    def test_oversized_file_flushes_current_batch(self):
        """AC: oversized file flushes the in-progress batch before clipping."""
        chunks = [
            ("a.py", "small"),
            ("big.py", "x" * 200),
            ("b.py", "small2"),
        ]
        batches = review.pack_into_batches(chunks, 100)
        # batch 1: "small", batch 2: clipped big, batch 3: "small2"
        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0], "small")
        self.assertIn("[NOTE: diff truncated", batches[1])
        self.assertEqual(batches[2], "small2")


class ProviderPayloadTests(unittest.TestCase):
    def test_build_openrouter_provider_with_routing(self):
        """AC: routing list -> order lowercased, allow_fallbacks False."""
        provider = review.build_openrouter_provider(["Together", "SiliconFlow"])
        self.assertEqual(
            provider, {"order": ["together", "siliconflow"], "allow_fallbacks": False}
        )

    def test_build_openrouter_provider_no_routing(self):
        """AC: routing None -> no provider payload."""
        self.assertIsNone(review.build_openrouter_provider(None))
        self.assertIsNone(review.build_openrouter_provider([]))

    def test_build_payload_merges_options_and_temperature(self):
        """AC: options merged as top-level keys; temperature present; provider from routing."""
        payload = review.build_payload(
            "m",
            [{"role": "user", "content": "hi"}],
            ["DeepInfra"],
            0.2,
            {"thinking": "max"},
        )
        self.assertEqual(payload["model"], "m")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["thinking"], "max")
        self.assertEqual(
            payload["provider"], {"order": ["deepinfra"], "allow_fallbacks": False}
        )

    def test_build_payload_no_routing_no_provider_key(self):
        """AC: routing None -> no 'provider' key in payload."""
        payload = review.build_payload("m", [], None, 0.0, None)
        self.assertNotIn("provider", payload)


class RunJudgeTests(unittest.TestCase):
    def _build_llm_response(self, content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    def test_run_judge_llm_failure_returns_needs_review(self):
        """AC: LLM call raising -> status 'NEEDS REVIEW' with error captured."""

        def raising_caller(judge_key, prompt, diff, api_key):
            raise RuntimeError("LLM down")

        status, reasoning, findings, error, used_fb, final_m = review.run_judge(
            "security",
            review.SYSTEM_PROMPT_SECURITY,
            "diff",
            "key",
            llm_caller=raising_caller,
        )
        self.assertEqual(status, "NEEDS REVIEW")
        self.assertEqual(findings, [])
        self.assertEqual(error, "LLM down")
        self.assertIn("Exception encountered: LLM down", reasoning)
        self.assertFalse(used_fb)

    def test_run_judge_pass_normalizes_uppercase(self):
        """AC: a Pass verdict normalizes to uppercase 'PASS'."""

        def passing_caller(judge_key, prompt, diff, api_key):
            return self._build_llm_response(
                "<reasoning>r</reasoning><findings></findings>"
            ), {"used_fallback": False, "final_model": "test-model", "attempt_count": 1}

        status, reasoning, findings, error, used_fb, final_m = review.run_judge(
            "syntax_lint",
            review.SYSTEM_PROMPT_SYNTAX_LINT,
            "diff",
            "key",
            llm_caller=passing_caller,
        )
        self.assertEqual(status, "PASS")
        self.assertIsNone(error)
        self.assertFalse(used_fb)
        self.assertEqual(final_m, "test-model")

    def test_run_judge_propagates_fallback_metadata(self):
        """AC: used_fallback=True from llm_caller propagates through run_judge."""

        def fallback_caller(judge_key, prompt, diff, api_key):
            return self._build_llm_response(
                "<reasoning>r</reasoning><findings></findings>"
            ), {
                "used_fallback": True,
                "final_model": "fallback-model",
                "attempt_count": 3,
            }

        status, reasoning, findings, error, used_fb, final_m = review.run_judge(
            "architecture",
            review.SYSTEM_PROMPT_ARCH,
            "diff",
            "key",
            llm_caller=fallback_caller,
        )
        self.assertEqual(status, "PASS")
        self.assertTrue(used_fb)
        self.assertEqual(final_m, "fallback-model")

    def test_run_judge_empty_diff_short_circuits_pass(self):
        """AC: empty diff → PASS without LLM call."""

        def never_called(judge_key, prompt, diff, api_key):
            raise AssertionError("LLM caller should not be invoked for empty diff")

        status, reasoning, findings, error, used_fb, final_m = review.run_judge(
            "security",
            review.SYSTEM_PROMPT_SECURITY,
            "",
            "key",
            llm_caller=never_called,
        )
        self.assertEqual(status, "PASS")
        self.assertEqual(findings, [])
        self.assertIsNone(error)
        self.assertFalse(used_fb)

    def test_run_judge_fast_path_single_call(self):
        """AC: diff under budget → one LLM call (fast path), no aggregation."""
        call_count = [0]

        def passing_caller(judge_key, prompt, diff, api_key):
            call_count[0] += 1
            return self._build_llm_response(
                "<reasoning>r</reasoning><findings></findings>"
            ), {"used_fallback": False, "final_model": "m", "attempt_count": 1}

        status, _, findings, error, _, _ = review.run_judge(
            "syntax_lint",
            review.SYSTEM_PROMPT_SYNTAX_LINT,
            "short diff",
            "key",
            llm_caller=passing_caller,
        )
        self.assertEqual(status, "PASS")
        self.assertEqual(call_count[0], 1)

    def test_run_judge_multi_batch_all_pass_aggregates_pass(self):
        """AC: multi-batch with all chunks PASS → judge PASS."""
        with patch.dict(os.environ, {"REVIEW_BATCH_BUDGET_CHARS": "50"}):
            diff = (
                "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
            )

            def passing_caller(judge_key, prompt, diff, api_key):
                return self._build_llm_response(
                    "<reasoning>r</reasoning><findings></findings>"
                ), {"used_fallback": False, "final_model": "m", "attempt_count": 1}

            status, _, findings, error, _, _ = review.run_judge(
                "syntax_lint",
                review.SYSTEM_PROMPT_SYNTAX_LINT,
                diff,
                "key",
                llm_caller=passing_caller,
            )
            self.assertEqual(status, "PASS")
            self.assertEqual(findings, [])
            self.assertIsNone(error)

    def test_run_judge_multi_batch_one_fail_aggregates_fail(self):
        """AC: multi-batch with one chunk FAIL → judge FAIL, findings concatenated."""
        with patch.dict(os.environ, {"REVIEW_BATCH_BUDGET_CHARS": "50"}):
            diff = (
                "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
            )
            responses = [
                self._build_llm_response(
                    "<reasoning>ok</reasoning><findings></findings>"
                ),
                self._build_llm_response(
                    "<reasoning>bad</reasoning><findings>\n"
                    '{"severity": "bug", "message": "issue found"}\n'
                    "</findings>"
                ),
            ]
            call_idx = [0]

            def mixed_caller(judge_key, prompt, diff, api_key):
                idx = min(call_idx[0], len(responses) - 1)
                call_idx[0] += 1
                return responses[idx], {
                    "used_fallback": False,
                    "final_model": "m",
                    "attempt_count": 1,
                }

            status, _, findings, error, _, _ = review.run_judge(
                "test_coverage",
                review.SYSTEM_PROMPT_TEST_COVERAGE,
                diff,
                "key",
                llm_caller=mixed_caller,
            )
            self.assertEqual(status, "FAIL")
            self.assertEqual(len(findings), 1)
            self.assertIn("issue found", findings[0])

    def test_run_judge_multi_batch_one_needs_review_aggregates_needs_review(self):
        """AC: multi-batch with one chunk NEEDS REVIEW → judge NEEDS REVIEW."""
        with patch.dict(os.environ, {"REVIEW_BATCH_BUDGET_CHARS": "50"}):
            diff = (
                "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
            )
            responses = [
                self._build_llm_response(
                    "<reasoning>ok</reasoning><findings></findings>"
                ),
                self._build_llm_response(""),  # empty → NEEDS REVIEW
            ]
            call_idx = [0]

            def mixed_caller(judge_key, prompt, diff, api_key):
                idx = min(call_idx[0], len(responses) - 1)
                call_idx[0] += 1
                return responses[idx], {
                    "used_fallback": False,
                    "final_model": "m",
                    "attempt_count": 1,
                }

            status, _, findings, error, _, _ = review.run_judge(
                "security",
                review.SYSTEM_PROMPT_SECURITY,
                diff,
                "key",
                llm_caller=mixed_caller,
            )
            self.assertEqual(status, "NEEDS REVIEW")

    def test_run_judge_multi_batch_one_exception_aggregates_needs_review(self):
        """AC: multi-batch with one chunk raising → judge NEEDS REVIEW with error."""
        with patch.dict(os.environ, {"REVIEW_BATCH_BUDGET_CHARS": "50"}):
            diff = (
                "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
            )
            call_idx = [0]

            def mixed_caller(judge_key, prompt, diff, api_key):
                call_idx[0] += 1
                if call_idx[0] == 1:
                    return self._build_llm_response(
                        "<reasoning>ok</reasoning><findings></findings>"
                    ), {"used_fallback": False, "final_model": "m", "attempt_count": 1}
                raise RuntimeError("chunk 2 failed")

            status, _, findings, error, _, _ = review.run_judge(
                "architecture",
                review.SYSTEM_PROMPT_ARCH,
                diff,
                "key",
                llm_caller=mixed_caller,
            )
            self.assertEqual(status, "NEEDS REVIEW")
            self.assertIsNotNone(error)
            self.assertIn("chunk 2 failed", error)

    def test_run_judge_multi_batch_fallback_propagates(self):
        """AC: any chunk using fallback → used_fallback=True, final_model=fallback."""
        with patch.dict(os.environ, {"REVIEW_BATCH_BUDGET_CHARS": "50"}):
            diff = (
                "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
                "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
            )
            call_idx = [0]

            def mixed_caller(judge_key, prompt, diff, api_key):
                call_idx[0] += 1
                if call_idx[0] == 1:
                    return self._build_llm_response(
                        "<reasoning>r</reasoning><findings></findings>"
                    ), {
                        "used_fallback": False,
                        "final_model": "primary",
                        "attempt_count": 1,
                    }
                return self._build_llm_response(
                    "<reasoning>r</reasoning><findings></findings>"
                ), {
                    "used_fallback": True,
                    "final_model": "fallback-m",
                    "attempt_count": 3,
                }

            status, _, _, _, used_fb, final_m = review.run_judge(
                "security",
                review.SYSTEM_PROMPT_SECURITY,
                diff,
                "key",
                llm_caller=mixed_caller,
            )
            self.assertTrue(used_fb)
            self.assertEqual(final_m, "fallback-m")


class EmptyContentHelperTests(unittest.TestCase):
    def test_is_empty_content_empty_string(self):
        """AC: empty string content -> True."""
        raw = json.dumps({"choices": [{"message": {"content": ""}}]})
        self.assertTrue(review._is_empty_content(raw))

    def test_is_empty_content_whitespace_only(self):
        """AC: whitespace-only content -> True."""
        raw = json.dumps({"choices": [{"message": {"content": "  \n  "}}]})
        self.assertTrue(review._is_empty_content(raw))

    def test_is_empty_content_non_empty(self):
        """AC: non-empty content -> False."""
        raw = json.dumps({"choices": [{"message": {"content": "hello"}}]})
        self.assertFalse(review._is_empty_content(raw))


class CallLlmForReviewTests(unittest.TestCase):
    """Tests for the layered retry + fallback logic in call_llm_for_review.

    These tests mock _call_with_api_retry to control the response sequence
    without making real HTTP calls.
    """

    def _build_response(self, content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_first_attempt_non_empty_no_retry(self, mock_tracer, mock_cfg, mock_retry):
        """AC: non-empty content on first attempt -> no fallback, attempt_count=1."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": "fallback-model",
        }
        good_resp = self._build_response(
            "<reasoning>r</reasoning><findings></findings>"
        )
        mock_retry.return_value = good_resp

        body, metadata = review.call_llm_for_review(
            "syntax_lint", "sys prompt", "diff", "key"
        )
        self.assertEqual(body, good_resp)
        self.assertFalse(metadata["used_fallback"])
        self.assertEqual(metadata["final_model"], "primary-model")
        self.assertEqual(metadata["attempt_count"], 1)
        self.assertEqual(mock_retry.call_count, 1)

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_empty_then_nudge_succeeds(self, mock_tracer, mock_cfg, mock_retry):
        """AC: empty first, non-empty on nudge -> 2 attempts, no fallback."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": "fallback-model",
        }
        empty_resp = self._build_response("")
        good_resp = self._build_response(
            "<reasoning>r</reasoning><findings></findings>"
        )
        mock_retry.side_effect = [empty_resp, good_resp]

        body, metadata = review.call_llm_for_review(
            "syntax_lint", "sys prompt", "diff", "key"
        )
        self.assertEqual(body, good_resp)
        self.assertFalse(metadata["used_fallback"])
        self.assertEqual(metadata["final_model"], "primary-model")
        self.assertEqual(metadata["attempt_count"], 2)

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_empty_twice_then_fallback_succeeds(
        self, mock_tracer, mock_cfg, mock_retry
    ):
        """AC: two empties then fallback succeeds -> used_fallback=True, 3 attempts."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": "fallback-model",
        }
        empty_resp = self._build_response("")
        good_resp = self._build_response(
            "<reasoning>r</reasoning><findings></findings>"
        )
        mock_retry.side_effect = [empty_resp, empty_resp, good_resp]

        body, metadata = review.call_llm_for_review(
            "syntax_lint", "sys prompt", "diff", "key"
        )
        self.assertEqual(body, good_resp)
        self.assertTrue(metadata["used_fallback"])
        self.assertEqual(metadata["final_model"], "fallback-model")
        self.assertEqual(metadata["attempt_count"], 3)

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_empty_all_three_returns_empty(self, mock_tracer, mock_cfg, mock_retry):
        """AC: all 3 attempts empty -> returns empty body (evaluate_response will
        produce NEEDS REVIEW)."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": "fallback-model",
        }
        empty_resp = self._build_response("")
        mock_retry.side_effect = [empty_resp, empty_resp, empty_resp]

        body, metadata = review.call_llm_for_review(
            "syntax_lint", "sys prompt", "diff", "key"
        )
        self.assertTrue(review._is_empty_content(body))
        self.assertTrue(metadata["used_fallback"])
        self.assertEqual(metadata["final_model"], "fallback-model")
        self.assertEqual(metadata["attempt_count"], 3)

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_no_fallback_model_skips_attempt_3(self, mock_tracer, mock_cfg, mock_retry):
        """AC: no fallback_model configured -> only 2 attempts, returns empty."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.0,
            "options": None,
            "fallback_model": None,
        }
        empty_resp = self._build_response("")
        mock_retry.side_effect = [empty_resp, empty_resp]

        body, metadata = review.call_llm_for_review(
            "syntax_lint", "sys prompt", "diff", "key"
        )
        self.assertTrue(review._is_empty_content(body))
        self.assertFalse(metadata["used_fallback"])
        self.assertEqual(metadata["attempt_count"], 2)

    @patch("review._call_with_api_retry")
    @patch("review.resolve_model_config")
    @patch("review.get_tracer")
    def test_fallback_uses_no_routing_no_options(
        self, mock_tracer, mock_cfg, mock_retry
    ):
        """AC: fallback call uses routing=None, options=None, temperature=0.0."""
        from telemetry import DummyTracer

        mock_tracer.return_value = DummyTracer()
        mock_cfg.return_value = {
            "model": "primary-model",
            "routing": ["Together"],
            "temperature": 0.5,
            "options": {"thinking": "max"},
            "fallback_model": "fallback-model",
        }
        empty_resp = self._build_response("")
        good_resp = self._build_response(
            "<reasoning>r</reasoning><findings></findings>"
        )
        mock_retry.side_effect = [empty_resp, empty_resp, good_resp]

        review.call_llm_for_review("security", "sys", "diff", "key")

        # Third call (fallback) should have routing=None, options=None, temp=0.0
        third_call = mock_retry.call_args_list[2]
        # _call_with_api_retry(model, messages, api_key, routing, temperature, options)
        self.assertIsNone(third_call.args[3])  # routing
        self.assertIsNone(third_call.args[5])  # options
        self.assertEqual(third_call.args[4], 0.0)  # temperature


class FallbackIndicatorTests(unittest.TestCase):
    def test_fallback_indicator_shown_when_used(self):
        """AC: used_fallback=True -> visible fallback notice in review body."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        fallbacks = {"security": True}
        final_models = {"security": "z-ai/glm-5.2"}
        data = _build_judges_data(
            statuses, fallbacks=fallbacks, final_models=final_models
        )
        body = review.build_review_body(data)
        self.assertIn("⚠️ **Fallback Model Used**", body)
        self.assertIn("z-ai/glm-5.2", body)

    def test_no_fallback_indicator_when_not_used(self):
        """AC: used_fallback=False -> no fallback notice in review body."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        data = _build_judges_data(statuses)
        body = review.build_review_body(data)
        self.assertNotIn("Fallback Model Used", body)

    def test_fallback_indicator_only_for_specific_judge(self):
        """AC: only the judge that used fallback shows the indicator."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        fallbacks = {"test_coverage": True}
        final_models = {"test_coverage": "z-ai/glm-5.2"}
        data = _build_judges_data(
            statuses, fallbacks=fallbacks, final_models=final_models
        )
        body = review.build_review_body(data)
        # Should appear in the test_coverage section
        self.assertIn("Fallback Model Used", body)
        # Count occurrences — should be exactly 1
        self.assertEqual(body.count("Fallback Model Used"), 1)


if __name__ == "__main__":
    unittest.main()
