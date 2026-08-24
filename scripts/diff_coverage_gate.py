#!/usr/bin/env python3
"""Deterministic Diff Coverage Gate (ADR-0052).

Fails when any production line added or modified by the current branch's
diff is not exercised by the test suite. This mechanizes the arithmetic the
Test Coverage PR Review Judge performs under its coverage-of-changed-code
criterion: intersect diff-changed production lines with the coverage
report's missing lines.

Usage:
    python3 scripts/diff_coverage_gate.py \
        --coverage-json coverage.json --base main

Exit codes:
    0  pass — every changed production line is covered (or nothing changed)
    1  violations — human-readable line list on stdout
    2  usage/environment error (bad ref, missing/unparseable report, args)

Scope notes:
    - Stdlib only; must not import ``orchestrator.*`` (standalone-script rule).
    - Orchestrator-Repository tooling for its own Python codebase; the
      language-agnostic Verification path for target repositories is
      unaffected.
    - Threshold is 100% changed-line coverage, stateless — no baseline
      bookkeeping. The global ``--cov-fail-under=89`` gate is untouched.
    - Branch-partial coverage (``partial_branches``) is intentionally out of
      scope in v1; only exact ``missing_lines`` are judged.
    - Untracked files are invisible to ``git diff`` and therefore unjudged.
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping

HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_TEST_DIR_SEGMENTS = frozenset({"tests", "__tests__"})


class GateEnvironmentError(RuntimeError):
    """Raised when the gate cannot run: exit code 2, distinct from findings."""


def _is_test_path(path: str) -> bool:
    """Conventional test-artifact heuristic, mirrored from nodes.py.

    Mirrors ``orchestrator/nodes.py::_is_test_path`` (issue #141) so both
    layers agree on what counts as a test file, plus one deliberate local
    addition: ``conftest.py``. Duplication is accepted per issue #147;
    extracting a shared module is a separate future concern.
    """
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    parts = normalized.split("/")
    if any(segment in _TEST_DIR_SEGMENTS for segment in parts[:-1]):
        return True
    name = parts[-1]
    return (
        name == "conftest.py"
        or name.startswith("test_")
        or fnmatch.fnmatch(name, "*_test.*")
        or ".test." in name
        or ".spec." in name
    )


def _unquote_git_path(path: str) -> str:
    """Undo git's C-style quoting of unusual paths in diff output.

    Git quotes post-image paths containing spaces, quotes, or non-ASCII as
    ``"a/weird path.py"`` with backslash escapes and octal byte escapes.
    Without decoding, the quoted string would never match a coverage-report
    key and the file would be falsely reported as absent from the report.
    """
    if not (path.startswith('"') and path.endswith('"') and len(path) >= 2):
        return path
    body = path[1:-1]
    # Tokens alternate between literal text (str) and raw bytes from octal
    # escapes; consecutive byte tokens form one UTF-8 sequence (e.g. git's
    # \\303\\251 encoding of 'é').
    tokens: list[str | bytes] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            if tokens and isinstance(tokens[-1], str):
                tokens[-1] += ch
            else:
                tokens.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        simple = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}
        if nxt in simple:
            tokens.append(simple[nxt])
            i += 2
            continue
        octal = re.match(r"[0-7]{1,3}", body[i + 1 :])
        if octal:
            tokens.append(bytes([int(octal.group(0), 8)]))
            i += 1 + len(octal.group(0))
            continue
        tokens.append(ch)
        i += 1
    out: list[str] = []
    byte_run = bytearray()
    for token in tokens:
        if isinstance(token, bytes):
            byte_run.extend(token)
            continue
        if byte_run:
            out.append(byte_run.decode("utf-8", errors="replace"))
            byte_run.clear()
        out.append(token)
    if byte_run:
        out.append(byte_run.decode("utf-8", errors="replace"))
    return "".join(out)


def _normalize(path: str) -> str:
    """Normalize a repo-relative path for comparison across git/coverage."""
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return os.path.normpath(normalized).replace("\\", "/")


def resolve_merge_base(base: str) -> str:
    """Resolve the merge-base of ``base`` and HEAD so only this branch's
    changes are judged even when the base has diverged."""
    result = subprocess.run(
        ["git", "merge-base", base, "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"git exit {result.returncode}"
        raise GateEnvironmentError(
            f"Cannot resolve merge-base of '{base}' and HEAD: {detail}. "
            "Pass an existing base ref via --base."
        )
    return result.stdout.strip()


def get_branch_diff(merge_base: str) -> str:
    """Unified diff (-U0) of the branch against its merge-base.

    Zero context means every '+' body line is a genuinely added line and the
    hunk header carries the exact post-image line number. --no-ext-diff
    guards against configured external diff tools breaking the parse.
    """
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "-U0",
            merge_base,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"git exit {result.returncode}"
        raise GateEnvironmentError(f"git diff failed: {detail}")
    return result.stdout


def extract_changed_lines(diff_text: str) -> dict[str, set[int]]:
    """Map post-image path -> set of added/modified line numbers.

    Deleted files (post-image /dev/null) yield no entry; pure renames carry
    no hunks and therefore no lines; rename-with-edits is attributed to the
    new path. Path state resets at every 'diff --git' header so hunks can
    never leak into the previous file's bucket.
    """
    changed: dict[str, set[int]] = {}
    current_path: str | None = None
    next_line = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            continue
        if line.startswith("rename ") or line.startswith("similarity "):
            continue
        if line.startswith("+++ "):
            target = line[4:]
            if target == "/dev/null":
                current_path = None
            else:
                # Unquote BEFORE stripping the b/ prefix: git quotes the
                # whole post-image spec, so the prefix sits inside quotes.
                body = _unquote_git_path(target.strip())
                prefix = "b/"
                if body.startswith(prefix):
                    body = body[len(prefix) :]
                current_path = _normalize(body)
                changed.setdefault(current_path, set())
            continue
        hunk = HUNK_HEADER_RE.match(line)
        if hunk:
            next_line = int(hunk.group(1))
            continue
        if current_path is None or not line:
            continue
        marker = line[0]
        if marker == "+":
            changed[current_path].add(next_line)
            next_line += 1
        elif marker in {"-", " "}:
            if marker == " ":
                next_line += 1
            continue
        # '\ No newline...' and any other noise: ignore.
    return changed


def collapse_line_numbers(lines: Iterable[int]) -> str:
    """Collapse sorted unique integers into '2485, 2590–2592' style ranges."""
    ordered = sorted(set(lines))
    if not ordered:
        return ""
    runs: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        runs.append((start, prev))
        start = prev = value
    runs.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}\u2013{b}" for a, b in runs)


def load_missing_lines(coverage_json_path: str) -> dict[str, set[int]]:
    """Load path -> missing statement lines from a coverage.py JSON report."""
    try:
        with open(coverage_json_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise GateEnvironmentError(
            f"Coverage report '{coverage_json_path}' not found. "
            "Generate it first, e.g.: make diff-coverage"
        ) from None
    except OSError as exc:
        raise GateEnvironmentError(
            f"Coverage report '{coverage_json_path}' unreadable: {exc}"
        ) from None
    except json.JSONDecodeError as exc:
        raise GateEnvironmentError(
            f"Coverage report '{coverage_json_path}' is not valid JSON: {exc}"
        ) from None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise GateEnvironmentError(
            f"Coverage report '{coverage_json_path}' has no 'files' mapping; "
            "is it really a coverage.py JSON report?"
        )
    missing: dict[str, set[int]] = {}
    for raw_path, info in files.items():
        if not isinstance(info, dict):
            continue
        raw_missing = info.get("missing_lines", [])
        if not isinstance(raw_missing, list):
            continue
        key = _normalize(str(raw_path))
        missing[key] = {int(v) for v in raw_missing if isinstance(v, int)}
    return missing


def find_violations(
    changed: Mapping[str, Iterable[int]],
    missing: Mapping[str, Iterable[int]],
) -> list[tuple[str, list[int] | None]]:
    """Intersect changed production lines with missing lines.

    Returns (path, uncovered_lines) tuples sorted by path; uncovered_lines
    is None when the changed production file is absent from the report
    entirely (never imported by any test).
    """
    violations: list[tuple[str, list[int] | None]] = []
    for path in sorted(changed):
        if not path.endswith(".py"):
            continue
        if _is_test_path(path):
            continue
        if path not in missing:
            violations.append((path, None))
            continue
        uncovered = sorted(set(changed[path]) & set(missing[path]))
        if uncovered:
            violations.append((path, uncovered))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when production lines added by this branch's diff are not "
            "covered by tests (ADR-0052)."
        )
    )
    parser.add_argument(
        "--coverage-json",
        default="coverage.json",
        help="Path to the coverage.py JSON report (default: coverage.json).",
    )
    parser.add_argument(
        "--base",
        default="main",
        help="Base ref to diff against (default: main).",
    )
    args = parser.parse_args(argv)

    try:
        merge_base = resolve_merge_base(args.base)
        diff_text = get_branch_diff(merge_base)
        changed = extract_changed_lines(diff_text)
        missing = load_missing_lines(args.coverage_json)
    except GateEnvironmentError as exc:
        print(f"[diff-coverage] {exc}", file=sys.stderr)
        return 2

    violations = find_violations(changed, missing)
    judged_files = sum(
        1 for path in changed if path.endswith(".py") and not _is_test_path(path)
    )
    if not violations:
        print(
            f"Diff Coverage Gate: PASS — {judged_files} changed production "
            "file(s), all changed lines covered."
        )
        return 0

    print("Diff Coverage Gate: FAIL — uncovered changed production lines:")
    for path, uncovered in violations:
        if uncovered is None:
            print(f"  {path}: never imported by any test (absent from report)")
        else:
            print(f"  {path}: {collapse_line_numbers(uncovered)}")
    total = sum(len(u) for _, u in violations if u is not None)
    print(
        f"{total} uncovered line(s) across {len(violations)} file(s). "
        "Add or extend tests before committing."
    )
    return 1


if __name__ == "__main__":  # pragma: no cover — process entrypoint
    sys.exit(main())
