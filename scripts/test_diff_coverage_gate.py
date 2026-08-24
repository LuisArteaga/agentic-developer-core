#!/usr/bin/env python3
"""Unit tests for scripts/diff_coverage_gate.py (issue #147, ADR-0052).

Covers every edge case from the issue contract: synthetic ``coverage.json``
fixtures plus temporary git repositories, mirroring ``test_review.py``
patterns.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make diff_coverage_gate.py importable from the same directory.
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))

import diff_coverage_gate as gate  # noqa: E402

SCRIPT_PATH = scripts_dir / "diff_coverage_gate.py"


def _run_gate(cwd: str | Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Run the gate as a subprocess against ``cwd``, returning the result."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *extra_args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _write_coverage_fixture(
    repo: Path,
    files: dict[str, list[int]],
    name: str = "coverage.json",
) -> Path:
    """Write a minimal synthetic coverage.py JSON report into ``repo``."""
    payload = {
        "meta": {"format": 3, "version": "0.0.0", "timestamp": "1970-01-01"},
        "files": {
            path: {"missing_lines": missing, "executed_lines": []}
            for path, missing in files.items()
        },
        "totals": {},
    }
    target = repo / name
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


class TempGitRepoTestCase(unittest.TestCase):
    """Base helper building an isolated two-commit git repository."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self._git("init", "-b", "main")
        self._write("README.md", "# temp repo\n")
        self._commit("initial")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _write(self, rel_path: str, content: str) -> Path:
        target = self.repo / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-m",
            message,
        )

    def _checkout_branch(self, name: str, create: bool = False) -> None:
        args = ["checkout"]
        if create:
            args.append("-b")
        args.append(name)
        self._git(*args)


class ExtractChangedLinesTests(unittest.TestCase):
    def test_multi_hunk_single_file_accumulates_exact_lines(self):
        """AC: added lines are keyed by post-image number across hunks."""
        diff = (
            "diff --git a/app.py b/app.py\n"
            "index 111..222 100644\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -2,0 +3,2 @@\n"
            "+alpha\n"
            "+beta\n"
            "@@ -10,0 +12,1 @@\n"
            "+gamma\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {"app.py": {3, 4, 12}})

    def test_context_line_tolerated_and_advances_counter(self):
        """AC: a -U3-style diff (context ' ' lines) still yields correct numbers."""
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,3 +1,4 @@\n"
            " keep\n"
            "-old\n"
            "+new\n"
            " keep too\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {"app.py": {2}})

    def test_no_newline_marker_ignored(self):
        """AC: '\\ No newline at end of file' noise never becomes a finding."""
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1 +1 @@\n"
            "-x\n"
            "\\ No newline at end of file\n"
            "+y\n"
            "\\ No newline at end of file\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {"app.py": {1}})

    def test_deleted_file_yields_no_entry(self):
        """AC: +++ /dev/null sections (deleted files) are ignored."""
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-dead\n"
            "-code\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {})

    def test_pure_rename_without_hunks_yields_nothing(self):
        """AC: renames without a post-image carry no changed lines."""
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 100%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {})

    def test_rename_with_edits_attributed_to_new_path(self):
        """AC: rename-with-edits hunks land under the post-image path."""
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 80%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
            "--- a/old_name.py\n"
            "+++ b/new_name.py\n"
            "@@ -1 +1,2 @@\n"
            "+fresh\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {"new_name.py": {1}})

    def test_quoted_post_image_path_is_unquoted(self):
        """AC: git-quoted paths (spaces/specials) decode to plain paths."""
        diff = (
            'diff --git "a/weird path.py" "b/weird path.py"\n'
            '--- "a/weird path.py"\n'
            '+++ "b/weird path.py"\n'
            "@@ -0,0 +1 @@\n"
            "+x\n"
        )
        self.assertEqual(gate.extract_changed_lines(diff), {"weird path.py": {1}})


class UnquoteGitPathTests(unittest.TestCase):
    def test_plain_path_passthrough(self):
        self.assertEqual(gate._unquote_git_path("a/b.py"), "a/b.py")

    def test_quoted_space_path_decodes(self):
        self.assertEqual(gate._unquote_git_path('"my file.py"'), "my file.py")

    def test_octal_escape_decodes_utf8_bytes(self):
        self.assertEqual(gate._unquote_git_path('"caf\\303\\251.py"'), "café.py")

    def test_escaped_quote_decodes(self):
        self.assertEqual(gate._unquote_git_path('"q\\"uote.py"'), 'q"uote.py')


class CollapseLineNumbersTests(unittest.TestCase):
    def test_empty_input_gives_empty_string(self):
        self.assertEqual(gate.collapse_line_numbers([]), "")

    def test_consecutive_run_collapses_to_en_dash_range(self):
        self.assertEqual(
            gate.collapse_line_numbers([2590, 2591, 2592]), "2590\u20132592"
        )

    def test_mixed_singles_and_ranges_match_issue_format(self):
        collapsed = gate.collapse_line_numbers([2485, 2590, 2591, 2592, 2599])
        self.assertEqual(collapsed, "2485, 2590\u20132592, 2599")

    def test_duplicates_and_order_are_normalized(self):
        self.assertEqual(gate.collapse_line_numbers([3, 1, 3, 2]), "1\u20133")


class IsTestPathMirrorTests(unittest.TestCase):
    def test_conventional_signals_recognized(self):
        for positive in [
            "tests/test_app.py",
            "__tests__/inner/app.js",
            "test_worker.py",
            "worker_test.go",
            "app.test.tsx",
            "app.spec.ts",
            "conftest.py",
        ]:
            with self.subTest(path=positive):
                self.assertTrue(gate._is_test_path(positive))

    def test_false_positive_guards_rejected(self):
        for negative in [
            "orchestrator/nodes.py",
            "contest.py",
            "latest_utils.py",
            "attestation.py",
            "specification.py",
        ]:
            with self.subTest(path=negative):
                self.assertFalse(gate._is_test_path(negative))


class FindViolationsTests(unittest.TestCase):
    def test_intersection_reports_only_changed_missing_lines(self):
        changed = {"app.py": {1, 2, 3}}
        missing = {"app.py": {2, 3, 99}}
        violations = gate.find_violations(changed, missing)
        self.assertEqual(violations, [("app.py", [2, 3])])

    def test_non_python_files_skipped(self):
        changed = {"README.md": {1}, "ci.yml": {2}}
        self.assertEqual(gate.find_violations(changed, {}), [])

    def test_test_paths_skipped_even_when_absent_from_report(self):
        changed = {"tests/test_app.py": {1}, "conftest.py": {2}}
        self.assertEqual(gate.find_violations(changed, {}), [])

    def test_absent_production_file_flags_none_marker(self):
        violations = gate.find_violations({"ghost.py": {1}}, {})
        self.assertEqual(violations, [("ghost.py", None)])

    def test_findings_sorted_by_path(self):
        violations = gate.find_violations({"zeta.py": {1}, "alpha.py": {1}}, {})
        self.assertEqual([path for path, _ in violations], ["alpha.py", "zeta.py"])

    def test_pragma_excluded_lines_absent_from_missing_lines_pass(self):
        """AC: '# pragma: no cover' lines never appear in missing_lines, so
        changed-but-excluded lines cannot be flagged (coverage.py contract,
        verified against coverage 7.x JSON reports)."""
        changed = {"app.py": {5}}
        missing = {"app.py": [1, 2]}  # line 5 excluded -> not reported
        self.assertEqual(gate.find_violations(changed, missing), [])


class LoadMissingLinesTests(unittest.TestCase):
    def test_missing_file_raises_environment_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gate.GateEnvironmentError) as ctx:
                gate.load_missing_lines(os.path.join(tmp, "nope.json"))
        self.assertIn("make diff-coverage", str(ctx.exception))

    def test_unparseable_json_raises_environment_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(gate.GateEnvironmentError):
                gate.load_missing_lines(str(bad))

    def test_structure_without_files_mapping_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('{"totals": {}}', encoding="utf-8")
            with self.assertRaises(gate.GateEnvironmentError):
                gate.load_missing_lines(str(bad))

    def test_paths_normalized_and_ints_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "coverage.json"
            report.write_text(
                json.dumps(
                    {"files": {"./pkg//mod.py": {"missing_lines": [3, "x", 4]}}}
                ),
                encoding="utf-8",
            )
            loaded = gate.load_missing_lines(str(report))
        self.assertEqual(loaded, {"pkg/mod.py": {3, 4}})


class GateEndToEndTests(TempGitRepoTestCase):
    def test_empty_diff_passes(self):
        """AC: no changed files -> exit 0."""
        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_diverged_base_judges_only_this_branchs_changes(self):
        """AC: merge-base resolution isolates branch changes from a moving
        base; changes landed on main after branching are never judged."""
        self._write("prod_base.py", "a = 1\n")
        self._commit("prod on main")
        self._checkout_branch("feature", create=True)
        self._write("feature_prod.py", "x = 1\ny = 2\n")
        self._commit("feature work")
        self._checkout_branch("main")
        self._write("main_only.py", "m = 1\n")  # diverges AFTER branching
        self._commit("independent main work")
        self._checkout_branch("feature")

        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(self.repo, "--coverage-json", str(report), "--base", "main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("feature_prod.py", result.stdout)
        self.assertNotIn("main_only.py", result.stdout)

    def test_new_production_file_zero_covered_lists_all_statement_lines(self):
        """AC: newly added uncovered production file fails listing its lines."""
        self._checkout_branch("feature", create=True)
        self._write("brand_new.py", "q = 1\nw = 2\ne = 3\n")
        self._commit("add module")
        # Imported by tests but zero coverage: every statement is missing.
        report = _write_coverage_fixture(self.repo, {"brand_new.py": [1, 2, 3]})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("brand_new.py: 1\u20133", result.stdout)

    def test_changed_production_file_absent_from_report_fails_at_file_level(self):
        """AC: tracked production file missing from coverage.json -> FAIL."""
        self._write("legacy.py", "value = 1\n")
        self._commit("add legacy")
        self._checkout_branch("feature", create=True)
        self._write("legacy.py", "value = 2\n")
        self._commit("touch legacy")
        report = _write_coverage_fixture(self.repo, {})  # no legacy.py entry
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("never imported by any test", result.stdout)
        self.assertIn("legacy.py", result.stdout)

    def test_covered_changed_lines_pass(self):
        """AC: changed lines exercised by tests -> exit 0."""
        self._write("covered.py", "keep = 1\n")
        self._commit("add covered")
        self._checkout_branch("feature", create=True)
        self._write("covered.py", "keep = 1\nadded = 2\n")
        self._commit("extend covered")
        report = _write_coverage_fixture(self.repo, {"covered.py": []})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_partial_coverage_reports_only_the_missing_changed_lines(self):
        """AC: intersection semantics — pre-existing gaps outside the diff
        are invisible; exactly the changed-and-missing lines are listed."""
        self._write("partial.py", "one = 1\n")
        self._commit("base")
        self._checkout_branch("feature", create=True)
        content = "one = 1\n" + "".join(f"v{i} = {i}\n" for i in range(2, 10))
        self._write("partial.py", content)  # adds post-image lines 2..9
        self._commit("grow partial")
        report = _write_coverage_fixture(self.repo, {"partial.py": [5, 8, 9]})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("partial.py: 5, 8\u20139", result.stdout)

    def test_changes_confined_to_test_locations_pass(self):
        """AC: test dirs, test_*.py, *_test.py, conftest.py -> exit 0."""
        self._checkout_branch("feature", create=True)
        self._write("tests/test_feature.py", "def test_x():\n    assert True\n")
        self._write("tests/conftest.py", "FIXTURE = 1\n")
        self._write("helper_test.py", "CASES = []\n")
        self._commit("tests only")
        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_deleted_and_pure_rename_changes_ignored(self):
        """AC: deletions and rename-only moves produce no findings."""
        self._write("doomed.py", "d = 1\n")
        self._commit("add doomed on main")
        self._checkout_branch("feature", create=True)
        self._git("mv", "doomed.py", "renamed.py")  # pure rename, no edits
        self._commit("rename without edits")
        self._write("scratch.py", "s = 1\n")
        self._commit("add scratch")
        self._git("rm", "scratch.py")  # deletion
        self._commit("delete scratch")
        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_non_python_changes_skipped(self):
        """AC: md/yml/toml changes never reach the arithmetic."""
        self._checkout_branch("feature", create=True)
        self._write("docs.md", "# hello\n")
        self._write("pipeline.yml", "on: push\n")
        self._write("config.toml", "[tool]\nkey = 1\n")
        self._commit("docs and config")
        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_missing_coverage_report_exits_2(self):
        """AC: absent coverage.json -> exit 2 with instructive stderr."""
        self._checkout_branch("feature", create=True)
        self._write("app_mod.py", "a = 1\n")
        self._commit("change without report")
        result = _run_gate(self.repo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("coverage", result.stderr)

    def test_unparseable_coverage_report_exits_2(self):
        """AC: corrupt coverage.json -> distinct exit-2 usage error."""
        (self.repo / "coverage.json").write_text("{{{", encoding="utf-8")
        result = _run_gate(self.repo)
        self.assertEqual(result.returncode, 2)
        self.assertNotEqual(result.returncode, 1)

    def test_invalid_base_ref_exits_2(self):
        """AC: unknown --base ref -> exit 2, instructive stderr."""
        report = _write_coverage_fixture(self.repo, {})
        result = _run_gate(
            self.repo, "--coverage-json", str(report), "--base", "no-such-ref"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--base", result.stderr)

    def test_findings_printed_as_collapsed_ranges_on_stdout(self):
        """AC: precise human-readable line list with en-dash ranges."""
        self._checkout_branch("feature", create=True)
        self._write(
            "ranges.py",
            "".join(f"v{i} = {i}\n" for i in range(1, 7)),  # lines 1..6
        )
        self._commit("add ranges module")
        report = _write_coverage_fixture(self.repo, {"ranges.py": [2, 3, 6]})
        result = _run_gate(self.repo, "--coverage-json", str(report))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ranges.py: 2\u20133, 6", result.stdout)


class InProcessCoverageCompletionTests(TempGitRepoTestCase):
    """Exercise branches the subprocess e2e tests cannot measure.

    The e2e suite runs the gate in a child process, so coverage.py never
    traces ``resolve_merge_base``/``get_branch_diff``/``main``. These tests
    drive those paths in-process (patched subprocess, direct main calls)
    so every new code path has measurable test coverage.
    """

    def _chdir_repo(self) -> None:
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)

    def test_dot_slash_prefix_normalized_in_test_path_heuristic(self):
        """AC: './'-prefixed paths classify identically to bare paths."""
        self.assertTrue(gate._is_test_path("./tests/app_test.py"))
        self.assertFalse(gate._is_test_path("./app.py"))

    def test_unknown_escape_sequence_preserved_verbatim(self):
        """AC: unquote keeps unrecognized escapes as literal characters."""
        self.assertEqual(gate._unquote_git_path('"we\\qird.py"'), "we\\qird.py")

    def test_trailing_octal_bytes_flushed_at_end_of_path(self):
        """AC: octal bytes at path end decode without a following literal."""
        # Lone leading byte 0xC3 is not valid UTF-8 -> replacement char.
        self.assertEqual(gate._unquote_git_path('"\\303"'), "\ufffd")

    def test_resolve_merge_base_returns_trimmed_sha(self):
        """AC: merge-base resolution returns git's stdout stripped."""
        completed = subprocess.CompletedProcess([], 0, stdout="abc123d\n", stderr="")
        with patch.object(gate.subprocess, "run", return_value=completed):
            self.assertEqual(gate.resolve_merge_base("main"), "abc123d")

    def test_resolve_merge_base_invalid_ref_raises_environment_error(self):
        """AC: failing merge-base surfaces stderr detail, exit-2 class."""
        completed = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: Not a valid object name 'nope'\n"
        )
        with patch.object(gate.subprocess, "run", return_value=completed):
            with self.assertRaises(gate.GateEnvironmentError) as ctx:
                gate.resolve_merge_base("nope")
        self.assertIn("nope", str(ctx.exception))
        self.assertIn("--base", str(ctx.exception))

    def test_get_branch_diff_uses_u0_and_merge_base(self):
        """AC: branch diff runs git diff -U0 --no-color against merge-base."""
        completed = subprocess.CompletedProcess(
            [], 0, stdout="diff --git ...", stderr=""
        )
        with patch.object(gate.subprocess, "run", return_value=completed) as mock_run:
            out = gate.get_branch_diff("deadbee")
        self.assertEqual(out, "diff --git ...")
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], "git")
        self.assertIn("-U0", args)
        self.assertIn("--no-ext-diff", args)
        self.assertIn("deadbee", args)

    def test_get_branch_diff_git_failure_raises_environment_error(self):
        """AC: failing git diff surfaces stderr detail, exit-2 class."""
        completed = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: bad object deadbee"
        )
        with patch.object(gate.subprocess, "run", return_value=completed):
            with self.assertRaises(gate.GateEnvironmentError) as ctx:
                gate.get_branch_diff("deadbee")
        self.assertIn("git diff", str(ctx.exception))

    def test_load_missing_lines_directory_report_raises_environment_error(self):
        """AC: unreadable (OSError) report -> exit-2 class, not a crash."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gate.GateEnvironmentError) as ctx:
                gate.load_missing_lines(tmp)
        self.assertIn("unreadable", str(ctx.exception))

    def test_non_dict_file_entry_is_skipped(self):
        """AC: malformed per-file entries are tolerated, not fatal."""
        report = Path(self.repo) / "coverage.json"
        report.write_text(
            json.dumps({"files": {"a.py": "not-a-dict"}}), encoding="utf-8"
        )
        self.assertEqual(gate.load_missing_lines(str(report)), {})

    def test_non_list_missing_lines_entry_is_skipped(self):
        """AC: non-list missing_lines values are tolerated, not fatal."""
        report = Path(self.repo) / "coverage.json"
        report.write_text(
            json.dumps({"files": {"a.py": {"missing_lines": "1-2"}}}),
            encoding="utf-8",
        )
        self.assertEqual(gate.load_missing_lines(str(report)), {})

    def test_main_passes_on_clean_tree(self):
        """AC: main() returns 0 and prints PASS on an empty diff."""
        self._chdir_repo()
        report = _write_coverage_fixture(self.repo, {})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = gate.main(["--base", "main", "--coverage-json", str(report)])
        self.assertEqual(code, 0)
        self.assertIn("PASS", buffer.getvalue())

    def test_main_fails_on_uncovered_change(self):
        """AC: main() returns 1 and prints the finding on uncovered lines."""
        self._chdir_repo()
        self._checkout_branch("feature", create=True)
        self._write("uncovered.py", "u = 1\n")
        self._commit("add uncovered")
        report = _write_coverage_fixture(self.repo, {"uncovered.py": [1]})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = gate.main(["--base", "main", "--coverage-json", str(report)])
        self.assertEqual(code, 1)
        self.assertIn("uncovered.py: 1", buffer.getvalue())

    def test_main_environment_error_returns_2(self):
        """AC: main() maps GateEnvironmentError to exit code 2 on stderr."""
        self._chdir_repo()
        _write_coverage_fixture(self.repo, {})
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = gate.main(["--base", "no-such-ref"])
        self.assertEqual(code, 2)
        self.assertIn("[diff-coverage]", buffer.getvalue())

    def test_main_reports_file_absent_from_coverage_report(self):
        """AC: changed production file missing from the report fails at
        file level ('never imported by any test'), not line level."""
        self._chdir_repo()
        self._checkout_branch("feature", create=True)
        self._write("orphan.py", "o = 1\n")
        self._commit("add orphan")
        report = _write_coverage_fixture(self.repo, {})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = gate.main(["--base", "main", "--coverage-json", str(report)])
        self.assertEqual(code, 1)
        self.assertIn(
            "orphan.py: never imported by any test (absent from report)",
            buffer.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
