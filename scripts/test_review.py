#!/usr/bin/env python3
"""Unit tests for scripts/review.py findings and reasoning parsing."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class JudgeNeutralityTests(unittest.TestCase):
    """Tests for the shared Judge Neutrality preamble (issue #61, ADR-0014
    enhancement). Verifies the neutrality frame is prepended to every judge
    prompt that reaches the LLM, without coupling it to the judge-specific
    ``augment_judge_prompt`` dispatch."""

    def test_neutrality_constant_has_judge_neutrality_header(self):
        """AC: the shared preamble carries the labeled neutrality section."""
        self.assertIn(
            "=== 0. JUDGE NEUTRALITY ===", review.JUDGE_NEUTRALITY_INSTRUCTIONS
        )

    def test_every_judge_prompt_prepends_neutrality(self):
        """AC: each JUDGE_PROMPTS entry starts with the neutrality preamble,
        so the frame precedes the judge-specific criteria for every judge."""
        for key in review.JUDGE_KEYS:
            self.assertTrue(
                review.JUDGE_PROMPTS[key].startswith(
                    review.JUDGE_NEUTRALITY_INSTRUCTIONS
                ),
                f"judge {key} prompt is missing the neutrality preamble",
            )

    def test_base_criteria_constants_untouched_by_neutrality(self):
        """AC: the raw criteria constants remain the judge-specific criteria
        (neutrality is layered on via JUDGE_PROMPTS, not baked into the
        constants) — keeps augment_judge_prompt's contract intact."""
        for base_prompt in (
            review.SYSTEM_PROMPT_SYNTAX_LINT,
            review.SYSTEM_PROMPT_TEST_COVERAGE,
            review.SYSTEM_PROMPT_ARCH,
            review.SYSTEM_PROMPT_SECURITY,
        ):
            self.assertNotIn("=== 0. JUDGE NEUTRALITY ===", base_prompt)

    def test_neutrality_covers_each_named_bias(self):
        """AC: the preamble names every bias it mitigates (metadata/ID,
        anchoring, ADR over-weighting, position-within-findings)."""
        text = review.JUDGE_NEUTRALITY_INSTRUCTIONS
        self.assertIn("author", text.lower())
        self.assertIn("automated agent", text.lower())
        self.assertIn("intended", text.lower())
        self.assertIn("compliance", text.lower())
        self.assertIn("position", text.lower())


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


class VerifyPythonSyntaxTests(unittest.TestCase):
    """Tests for the deterministic py_compile pre-check (ADR-0025)."""

    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.test_dir.name).resolve()

    def tearDown(self):
        self.test_dir.cleanup()

    def _make_file(self, name: str, content: str) -> str:
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def _diff_for(self, *filenames: str) -> str:
        parts = []
        for fn in filenames:
            parts.append(
                f"diff --git a/{fn} b/{fn}\n"
                f"--- a/{fn}\n"
                f"+++ b/{fn}\n"
                f"@@ -1 +1 @@\n"
                f"-old\n"
                f"+new\n"
            )
        return "".join(parts)

    def test_valid_python_returns_pass(self):
        """AC: syntactically valid Python files → all_passed=True, no errors."""
        self._make_file("good.py", "x = 1\n")
        diff = self._diff_for("good.py")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertTrue(passed)
        self.assertEqual(errors, [])
        self.assertEqual(checked, 1)

    def test_syntax_error_returns_fail(self):
        """AC: file with IndentationError → all_passed=False, error reported."""
        self._make_file("bad.py", "def foo():\nx = 1\n")
        diff = self._diff_for("bad.py")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertFalse(passed)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad.py", errors[0])
        self.assertEqual(checked, 1)

    def test_non_python_files_skipped(self):
        """AC: .json/.md files in diff → files_checked=0, no errors."""
        diff = self._diff_for("config.json", "readme.md")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertTrue(passed)
        self.assertEqual(errors, [])
        self.assertEqual(checked, 0)

    def test_deleted_file_skipped(self):
        """AC: file not in workspace (deleted) → silently skipped."""
        diff = self._diff_for("deleted.py")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertTrue(passed)
        self.assertEqual(errors, [])
        self.assertEqual(checked, 0)

    def test_mixed_valid_and_invalid(self):
        """AC: one valid + one invalid → all_passed=False, checked=2."""
        self._make_file("good.py", "x = 1\n")
        self._make_file("bad.py", "def foo():\nx = 1\n")
        diff = self._diff_for("good.py", "bad.py")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertFalse(passed)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad.py", errors[0])
        self.assertEqual(checked, 2)

    def test_nested_path(self):
        """AC: file in subdirectory → resolved and checked."""
        self._make_file("orchestrator/nested.py", "def bar():\n    pass\n")
        diff = self._diff_for("orchestrator/nested.py")
        passed, errors, checked = review.verify_python_syntax(str(self.workspace), diff)
        self.assertTrue(passed)
        self.assertEqual(checked, 1)


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


class TestCoveragePromptTests(unittest.TestCase):
    """AC tests for the refocused semantic Test Coverage prompt (issue #149).

    The judge evaluates semantics only - Assertion Strength, Edge Cases,
    Implementation Leakage - from the diff alone. Changed-line coverage
    arithmetic moved to the deterministic Diff Coverage Gate (ADR-0052),
    which superseded the ADR-0030 CI-output transport; no augmentation
    path exists anymore.
    """

    def test_prompt_contains_three_semantic_criteria(self):
        """AC: exactly the three new semantic criteria are defined."""
        text = review.SYSTEM_PROMPT_TEST_COVERAGE
        self.assertIn("ASSERTION STRENGTH", text)
        self.assertIn("EDGE CASES", text)
        self.assertIn("IMPLEMENTATION LEAKAGE", text)

    def test_prompt_has_no_legacy_q_labels_or_criterion_names(self):
        """AC: no legacy Test-Coverage Q1-Q4 labels or criterion names remain."""
        text = review.SYSTEM_PROMPT_TEST_COVERAGE
        self.assertNotIn("Q1", text)
        self.assertNotIn("Q2", text)
        self.assertNotIn("Q3", text)
        self.assertNotIn("Q4", text)
        self.assertNotIn("Test Presence", text)
        self.assertNotIn("Test Quality", text)
        self.assertNotIn("Test Execution", text)
        self.assertNotIn("Coverage of Changed Code", text)

    def test_retired_ci_coverage_machinery_removed(self):
        """AC: no CI-output loading/augmentation path exists at all."""
        for name in (
            "load_ci_coverage_output",
            "build_test_coverage_ci_augmentation",
            "_get_ci_coverage_output_budget",
            "_tail_clip",
            "CI_COVERAGE_OUTPUT_MAX_CHARS",
        ):
            self.assertFalse(hasattr(review, name), f"{name} should be removed")

    def test_verdict_and_output_contract_unchanged(self):
        """AC: verdict vocabulary and reasoning/findings blocks are intact."""
        text = review.SYSTEM_PROMPT_TEST_COVERAGE
        self.assertIn("<reasoning>", text)
        self.assertIn("</reasoning>", text)
        self.assertIn("<findings>", text)
        self.assertIn("</findings>", text)
        # Scoring rule still maps findings to FAIL and empty findings to PASS.
        self.assertIn("PASS:", text)
        self.assertIn("FAIL:", text)


class AugmentJudgePromptTests(unittest.TestCase):
    """Tests for the pure per-judge prompt-augmentation dispatch extracted from
    main() (issue #45)."""

    def _syntax_result(self, passed, errors=None, checked=1):
        return (passed, errors or [], checked)

    def test_security_judge_unchanged(self):
        """AC: a judge with no augmentation (security) -> prompt unchanged."""
        self.assertEqual(
            review.augment_judge_prompt(
                "security",
                "base",
                self._syntax_result(True),
                "ARCH",
            ),
            "base",
        )

    def test_syntax_lint_passed_appends_pass_message(self):
        """AC: syntax_lint with passing py_compile -> PASS verification block."""
        result = review.augment_judge_prompt(
            "syntax_lint",
            "base",
            self._syntax_result(True, checked=2),
            "",
        )
        self.assertIn("=== DETERMINISTIC SYNTAX VERIFICATION ===", result)
        self.assertIn("Q1 (Syntax Validation) is PASS", result)
        self.assertIn("base", result)

    def test_syntax_lint_failed_appends_fail_message_with_errors(self):
        """AC: syntax_lint with failing py_compile -> FAIL block listing errors."""
        result = review.augment_judge_prompt(
            "syntax_lint",
            "base",
            self._syntax_result(
                False, errors=["foo.py: bad", "bar.py: worse"], checked=2
            ),
            "",
        )
        self.assertIn("Q1 (Syntax Validation) is FAIL", result)
        self.assertIn("- foo.py: bad", result)
        self.assertIn("- bar.py: worse", result)

    def test_syntax_lint_zero_checked_no_augmentation(self):
        """AC: syntax_lint with checked==0 (no .py files) -> no augmentation."""
        result = review.augment_judge_prompt(
            "syntax_lint",
            "base",
            self._syntax_result(True, checked=0),
            "",
        )
        self.assertEqual(result, "base")

    def test_syntax_lint_none_result_no_augmentation(self):
        """AC: syntax_lint with syntax_result=None -> no augmentation."""
        result = review.augment_judge_prompt("syntax_lint", "base", None, "")
        self.assertEqual(result, "base")

    def test_architecture_with_context_appends_context(self):
        """AC: architecture judge with non-empty arch_context -> context appended."""
        result = review.augment_judge_prompt(
            "architecture", "base", None, "ADR-0014: ..."
        )
        self.assertIn("=== REPOSITORY ARCHITECTURE CONTEXT ===", result)
        self.assertIn("ADR-0014: ...", result)

    def test_architecture_without_context_appends_fallback(self):
        """AC: architecture judge with empty arch_context -> fallback message."""
        result = review.augment_judge_prompt("architecture", "base", None, "")
        self.assertIn("Falling back to default rules", result)

    def test_test_coverage_no_augmentation_path(self):
        """AC: test_coverage receives its base prompt unchanged - the CI-output
        augmentation path was retired with ADR-0030 (issue #149); semantics
        are evaluated from the diff alone."""
        result = review.augment_judge_prompt("test_coverage", "base", None, "")
        self.assertEqual(result, "base")


class MainTests(unittest.TestCase):
    """Focused tests for review.main() covering the wiring (augment_judge_prompt
    dispatch, the judge loop, submit, exit paths).

    External I/O (telemetry, stdin, env, LLM call, gh CLI) is mocked, mirroring
    the test_entrypoint.py pattern. Covers the main()-level lines touched by
    issue #45 so the changed wiring is exercised by passing tests.
    """

    def _run_main(self, judge_statuses, diff="some diff"):
        """Invoke review.main() with all externals mocked.

        ``judge_statuses`` is a dict mapping judge_key -> status string
        returned by the mocked run_judge. Returns the captured call args so
        assertions can inspect the review action and body.
        """
        from telemetry import DummyTracer

        submit_calls = []

        def fake_submit(pr_number, action, body):
            submit_calls.append((pr_number, action, body))

        def make_run_judge():
            def fake_run_judge(judge_key, prompt, diff_arg, api_key):
                status = judge_statuses.get(judge_key, "PASS")
                return (status, "reasoning", [], None, False, "model-x")

            return fake_run_judge

        env = {
            "PR_NUMBER": "42",
            "GH_PAT": "tok",
            "OPENROUTER_API_KEY": "or-key",
            "GITHUB_WORKSPACE": "/tmp/nonexistent_workspace_xyz",
        }
        # Ensure GH_TOKEN not set so GH_PAT is used.
        clean_env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN",)}
        clean_env.update(env)

        stdin_mock = MagicMock()
        stdin_mock.read.return_value = diff

        with (
            patch("review.init_telemetry"),
            patch("review.get_tracer", return_value=DummyTracer()),
            patch("review.verify_python_syntax", return_value=(True, [], 0)),
            patch("review.load_architecture_context", return_value="ARCH_CTX"),
            patch("review.run_judge", side_effect=make_run_judge()),
            patch("review.submit_github_review", side_effect=fake_submit),
            patch("sys.exit") as mock_exit,
            patch("sys.stdin", stdin_mock),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            review.main()

        return mock_exit, submit_calls

    def test_main_all_pass_approves_and_exits_zero(self):
        """AC: all judges PASS -> review action 'approve', submit called, exit(0)."""
        mock_exit, submit_calls = self._run_main({k: "PASS" for k in review.JUDGE_KEYS})
        mock_exit.assert_called_once_with(0)
        self.assertEqual(len(submit_calls), 1)
        pr_num, action, body = submit_calls[0]
        self.assertEqual(pr_num, "42")
        self.assertEqual(action, "approve")
        # Hidden verdict block lists all four judges as PASS.
        for key in review.JUDGE_KEYS:
            self.assertIn(f"{key}: PASS", body)

    def test_main_any_failed_requests_changes_and_exits_one(self):
        """AC: any judge FAIL -> review action 'request-changes', exit(1)."""
        statuses = {k: "PASS" for k in review.JUDGE_KEYS}
        statuses["test_coverage"] = "FAIL"
        mock_exit, submit_calls = self._run_main(statuses)
        mock_exit.assert_called_once_with(1)
        self.assertEqual(len(submit_calls), 1)
        _, action, _ = submit_calls[0]
        self.assertEqual(action, "request-changes")


class LayeredRetryPolicyTests(unittest.TestCase):
    """Tests for ``_run_layered_retry`` directly.

    The policy was extracted out of ``call_llm_for_review`` (issue #51) so the
    empty-content retry + fallback progression is testable in isolation,
    without resolving a Model Config or standing up a telemetry tracer. These
    tests mock only the transport (``_call_with_api_retry``).
    """

    def _build_response(self, content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    def _messages(self):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "diff"},
        ]

    @patch("review._call_with_api_retry")
    def test_non_empty_first_attempt_no_retry(self, mock_retry):
        """AC: non-empty on first attempt -> 1 call, no fallback."""
        good = self._build_response("<reasoning>r</reasoning><findings></findings>")
        mock_retry.return_value = good

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "syntax_lint",
            "primary",
            self._messages(),
            "fallback",
            "key",
            ["Together"],
            0.0,
            None,
        )
        self.assertEqual(body, good)
        self.assertFalse(used_fb)
        self.assertEqual(final_m, "primary")
        self.assertEqual(attempts, 1)
        self.assertEqual(mock_retry.call_count, 1)

    @patch("review._call_with_api_retry")
    def test_empty_then_nudge_succeeds(self, mock_retry):
        """AC: empty first, non-empty on nudge -> 2 calls, no fallback."""
        empty = self._build_response("")
        good = self._build_response("<reasoning>r</reasoning><findings></findings>")
        mock_retry.side_effect = [empty, good]

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "syntax_lint",
            "primary",
            self._messages(),
            "fallback",
            "key",
            ["Together"],
            0.0,
            None,
        )
        self.assertEqual(body, good)
        self.assertFalse(used_fb)
        self.assertEqual(final_m, "primary")
        self.assertEqual(attempts, 2)
        # The nudge must append EMPTY_CONTENT_INSTRUCTION to the last turn only.
        second_call_messages = mock_retry.call_args_list[1].args[1]
        self.assertEqual(second_call_messages[0]["content"], "sys")
        self.assertEqual(
            second_call_messages[1]["content"],
            "diff" + review.EMPTY_CONTENT_INSTRUCTION,
        )

    @patch("review._call_with_api_retry")
    def test_empty_twice_then_fallback_succeeds(self, mock_retry):
        """AC: two empties then fallback -> 3 calls, used_fallback, fallback config."""
        empty = self._build_response("")
        good = self._build_response("<reasoning>r</reasoning><findings></findings>")
        mock_retry.side_effect = [empty, empty, good]

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "security",
            "primary",
            self._messages(),
            "fallback",
            "key",
            ["Together"],
            0.5,
            {"thinking": "max"},
        )
        self.assertEqual(body, good)
        self.assertTrue(used_fb)
        self.assertEqual(final_m, "fallback")
        self.assertEqual(attempts, 3)
        # ADR-0021: fallback call uses routing=None, options=None, temperature=0.0.
        third = mock_retry.call_args_list[2]
        self.assertEqual(third.args[0], "fallback")  # model
        self.assertIsNone(third.args[3])  # routing
        self.assertEqual(third.args[4], 0.0)  # temperature
        self.assertIsNone(third.args[5])  # options
        # Fallback reuses the ORIGINAL messages, not the nudged ones.
        self.assertEqual(third.args[1][1]["content"], "diff")

    @patch("review._call_with_api_retry")
    def test_no_fallback_model_exhausts_at_two(self, mock_retry):
        """AC: no fallback_model -> 2 attempts, returns empty, no fallback."""
        empty = self._build_response("")
        mock_retry.side_effect = [empty, empty]

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "syntax_lint",
            "primary",
            self._messages(),
            None,
            "key",
            ["Together"],
            0.0,
            None,
        )
        self.assertTrue(review._is_empty_content(body))
        self.assertFalse(used_fb)
        self.assertEqual(final_m, "primary")
        self.assertEqual(attempts, 2)
        self.assertEqual(mock_retry.call_count, 2)

    @patch("review._call_with_api_retry")
    def test_primary_api_error_triggers_fallback(self, mock_retry):
        """AC: attempt 1 exhausts API retries -> fallback, 2 attempts
        (ADR-0021 amendment 2026-08-31: 429/5xx/timeouts reach the fallback)."""
        good = self._build_response("<reasoning>r</reasoning><findings></findings>")
        mock_retry.side_effect = [
            Exception("LLM review failed after retries. Last error: HTTP 429"),
            good,
        ]

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "syntax_lint",
            "primary",
            self._messages(),
            "fallback",
            "key",
            ["Together"],
            0.0,
            None,
        )
        self.assertEqual(body, good)
        self.assertTrue(used_fb)
        self.assertEqual(final_m, "fallback")
        self.assertEqual(attempts, 2)
        self.assertEqual(mock_retry.call_count, 2)
        # ADR-0021: fallback call uses routing=None, options=None, temp=0.0.
        fallback_call = mock_retry.call_args_list[1]
        self.assertEqual(fallback_call.args[0], "fallback")
        self.assertIsNone(fallback_call.args[3])
        self.assertEqual(fallback_call.args[4], 0.0)
        self.assertIsNone(fallback_call.args[5])
        self.assertEqual(fallback_call.args[1][1]["content"], "diff")

    @patch("review._call_with_api_retry")
    def test_primary_api_error_no_fallback_reraises(self, mock_retry):
        """AC: API-error exhaustion with no fallback_model -> propagates."""
        mock_retry.side_effect = Exception("LLM review failed after retries")

        with self.assertRaises(Exception):
            review._run_layered_retry(
                "syntax_lint",
                "primary",
                self._messages(),
                None,
                "key",
                ["Together"],
                0.0,
                None,
            )
        self.assertEqual(mock_retry.call_count, 1)

    @patch("review._call_with_api_retry")
    def test_nudge_api_error_triggers_fallback(self, mock_retry):
        """AC: empty first, nudge exhausts API retries -> fallback, 3 attempts."""
        empty = self._build_response("")
        good = self._build_response("<reasoning>r</reasoning><findings></findings>")
        mock_retry.side_effect = [
            empty,
            Exception("LLM review failed after retries. Last error: HTTP 429"),
            good,
        ]

        body, used_fb, final_m, attempts = review._run_layered_retry(
            "architecture",
            "primary",
            self._messages(),
            "fallback",
            "key",
            ["Together"],
            0.0,
            None,
        )
        self.assertEqual(body, good)
        self.assertTrue(used_fb)
        self.assertEqual(final_m, "fallback")
        self.assertEqual(attempts, 3)
        self.assertEqual(mock_retry.call_count, 3)

    @patch("review._call_with_api_retry")
    def test_fallback_api_error_propagates(self, mock_retry):
        """AC: fallback also exhausts API retries -> propagates to run_judge
        (which maps the error to NEEDS REVIEW)."""
        mock_retry.side_effect = [
            Exception("primary 429"),
            Exception("fallback 429"),
        ]

        with self.assertRaises(Exception):
            review._run_layered_retry(
                "syntax_lint",
                "primary",
                self._messages(),
                "fallback",
                "key",
                ["Together"],
                0.0,
                None,
            )
        self.assertEqual(mock_retry.call_count, 2)


class EnrichChunkIntegrationTests(unittest.TestCase):
    """Integration tests verifying enrichment is wired into run_judge."""

    def _build_llm_response(self, content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    def test_enrich_chunk_appends_function_context(self):
        """AC: _enrich_chunk enriches a diff with enclosing function context."""
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "foo.py").write_text(
                "def bar():\n    return 1\n", encoding="utf-8"
            )
            diff = (
                "diff --git a/foo.py b/foo.py\n"
                "--- a/foo.py\n"
                "+++ b/foo.py\n"
                "@@ -1,1 +1,1 @@\n"
            )
            enriched = review._enrich_chunk(diff, str(workspace_path))
            self.assertIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)
            self.assertIn("--- foo.py :: bar ---", enriched)
            self.assertIn("def bar():", enriched)

    def test_enrich_chunk_no_workspace_returns_unchanged(self):
        """AC: _enrich_chunk with empty/nonexistent workspace returns diff."""
        diff = "diff --git a/missing.py b/missing.py\n@@ -1 +1 @@\n"
        enriched = review._enrich_chunk(diff, "/nonexistent/path")
        self.assertEqual(enriched, diff)

    def test_run_judge_fast_path_enriches_before_llm(self):
        """AC: run_judge enriches the diff before passing to llm_caller."""
        captured_diffs: list[str] = []

        def capturing_caller(judge_key, prompt, diff, api_key):
            captured_diffs.append(diff)
            return self._build_llm_response(
                "<reasoning>r</reasoning><findings></findings>"
            ), {"used_fallback": False, "final_model": "m", "attempt_count": 1}

        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "app.py").write_text(
                "def greet():\n    return 'hi'\n", encoding="utf-8"
            )
            with patch.dict(os.environ, {"GITHUB_WORKSPACE": str(workspace_path)}):
                diff = (
                    "diff --git a/app.py b/app.py\n"
                    "--- a/app.py\n"
                    "+++ b/app.py\n"
                    "@@ -1,1 +1,1 @@\n"
                )
                review.run_judge(
                    "syntax_lint",
                    review.SYSTEM_PROMPT_SYNTAX_LINT,
                    diff,
                    "key",
                    llm_caller=capturing_caller,
                )

        self.assertEqual(len(captured_diffs), 1)
        self.assertIn("=== ENCLOSING FUNCTION CONTEXT ===", captured_diffs[0])
        self.assertIn("--- app.py :: greet ---", captured_diffs[0])

    def test_run_judge_multi_batch_path_enriches_before_llm(self):
        """AC: multi-batch path enriches EACH batch before passing to llm_caller.

        Closes the residual coverage gap from issue #55: the fast path's
        enrichment is asserted by ``test_run_judge_fast_path_enriches_before_llm``,
        but the multi-batch branch (split -> pack -> per-batch enrichment per
        ADR-0032) was only asserted for verdict aggregation, never for its
        enrichment behaviour. If the ``_enrich_chunk(batch)`` call were removed,
        no other test would fail.
        """
        captured: list[str] = []

        def capturing_caller(judge_key, prompt, diff, api_key):
            captured.append(diff)
            return self._build_llm_response(
                "<reasoning>r</reasoning><findings></findings>"
            ), {"used_fallback": False, "final_model": "m", "attempt_count": 1}

        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = Path(workspace)
            (workspace_path / "a.py").write_text(
                "def alpha():\n    return 1\n", encoding="utf-8"
            )
            (workspace_path / "b.py").write_text(
                "def beta():\n    return 2\n", encoding="utf-8"
            )
            # Budget chosen so each file-section fits (no clip) but two sections
            # cannot share a batch -> guarantees the multi-batch path (2 calls).
            env = {
                "GITHUB_WORKSPACE": str(workspace_path),
                "REVIEW_BATCH_BUDGET_CHARS": "100",
            }
            with patch.dict(os.environ, env):
                diff = (
                    "diff --git a/a.py b/a.py\n"
                    "--- a/a.py\n"
                    "+++ b/a.py\n"
                    "@@ -1,1 +1,1 @@\n"
                    "-old\n"
                    "+new\n"
                    "diff --git a/b.py b/b.py\n"
                    "--- b/b.py\n"
                    "+++ b/b.py\n"
                    "@@ -1,1 +1,1 @@\n"
                    "-x\n"
                    "+y\n"
                )
                review.run_judge(
                    "architecture",
                    review.SYSTEM_PROMPT_ARCH,
                    diff,
                    "key",
                    llm_caller=capturing_caller,
                )

        # Multi-batch path produced one call per batch.
        self.assertEqual(len(captured), 2)
        # Each batch received its own enclosing-function context block.
        for chunk in captured:
            self.assertIn("=== ENCLOSING FUNCTION CONTEXT ===", chunk)
        self.assertIn("--- a.py :: alpha ---", captured[0])
        self.assertIn("--- b.py :: beta ---", captured[1])


if __name__ == "__main__":
    unittest.main()
