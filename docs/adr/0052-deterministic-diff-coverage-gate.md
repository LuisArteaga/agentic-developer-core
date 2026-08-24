# ADR 0052: Deterministic Diff Coverage Gate for Changed Production Lines

* **Status**: Accepted
* **Date**: 2026-08-24
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The Test Coverage PR Review Judge (`scripts/review.py`, ADR-0014) evaluates pull-request diffs partly through its Q4 criterion ("Coverage of Changed Code"): an LLM reads the diff plus the CI coverage output (ADR-0030) and decides whether changed production lines are exercised by tests.

Across merged PRs #122–#146, **every** Test Coverage judge FAIL was a mechanical Q4 finding — a line-arithmetic intersection that needs no semantic judgment. Example (PR #146): `orchestrator/nodes.py` lines 2485, 2539, 2543, 2590–2592, 2599, 2631 were added by the diff and absent from the coverage report. Each such finding costs an extra commit plus a full CI + judge round-trip via the Merge-Fix Loop (CONTEXT.md), purely to let a human-identical conclusion be re-derived by machinery.

This is the same structural weakness ADR-0025 eliminated for syntax validation: **an LLM is an unreliable and costly arbiter of a deterministic computation**. Line intersection is exactly computable from two artifacts the loop already produces: the branch diff and `coverage.json`.

The decision is hard to reverse because the gate becomes part of the developer workflow and a future follow-up will wire it into CI as a merge-blocking check; there were genuine alternatives (LLM-only status quo, third-party tooling, stdlib script).

## Decision Drivers

* **Deterministic-first principle** (ADR-0025 precedent): what a script can compute exactly, no LLM should estimate.
* **Metric mismatch of the global gate**: `--cov-fail-under=89` measures *total* coverage percentage; a few uncovered new lines move it imperceptibly. Only a diff-scoped check can see them.
* **Zero new dependencies**: the Architecture judge enforces Radical Simplicity / stdlib preference; ADR-0009 culture keeps tooling lean.
* **Cost asymmetry**: catching an uncovered line locally at commit time is free; catching it post-push costs a Merge-Fix Loop iteration.

## Considered Options

* **Option 1: LLM-only status quo (rejected)** — keep relying on the Test Coverage Judge's Q4.
  * *Pros*: no new code.
  * *Cons*: every finding is a paid LLM inference arriving only after push; subject to the same line-mapping errors ADR-0025 documented for visual diff reading; feedback arrives one full CI round-trip late.

* **Option 2: Adopt `diff-cover` (Bachmann1234/diff_cover) (rejected — Framework-First Research, ADR-0042)** — the canonical open-source tool solving precisely "intersect git diff with coverage report".
  * *Pros*: battle-tested; plugin ecosystem.
  * *Cons*: adds a runtime/dev dependency against this repo's stdlib-only constraint; consumes Cobertura **XML**, not the coverage.py JSON contract our pipeline already produces (ADR-0030's `coverage.json`); its default semantics are percentage-threshold reporting rather than our enumerate-every-violation contract; the logic itself is ~150 lines of stdlib.

* **Option 3: Stdlib-only gate script (chosen)** — `scripts/diff_coverage_gate.py` resolves `git merge-base <base> HEAD`, parses `git diff --no-color -U0 <merge-base>`, intersects changed production lines with `files[...].missing_lines` from `coverage.json` (schema verified empirically against coverage 7.15.3), and exits 1 listing every violation.

## Decision

We chose **Option 3**.

Contract:

1. `make diff-coverage` runs `pytest --cov=orchestrator --cov=scripts --cov-report=term-missing --cov-report=json:coverage.json`, then invokes the gate.
2. `python3 scripts/diff_coverage_gate.py --coverage-json coverage.json --base main` resolves the merge base, extracts added/modified line ranges per file, and:
   * exit **0** — every changed production line covered;
   * exit **1** — violations listed on stdout with collapsed ranges (`orchestrator/nodes.py: 2485, 2590–2592`) or file-level findings for changed production files absent from the report entirely;
   * exit **2** — usage/environment error (invalid `--base`, missing/unreadable/unparseable coverage JSON).
3. The global `--cov-fail-under=89` and all existing pr-checks.yml steps remain untouched.

### Threshold semantics

**100% of changed production lines, stateless.** No baseline bookkeeping: the gate judges only this branch's changes, so history never accumulates debt to track. This mirrors SonarSource's *Clean as You Code* methodology (quality enforced on New Code). We deliberately do **not** adopt SonarQube's small-change leniency (ignoring gates on very few new lines): as opt-in developer tooling the gate must be exact — leniency belongs to policy layers, not arithmetic.

### Scope classification

A changed file counts as *production* when it ends in `.py` and is not a conventional test artifact. Test detection mirrors `orchestrator/nodes.py::_is_test_path` (issue #141) — test directories, `test_*.py`, `*_test.*`, `.test.`/`.spec.` markers, with prefix/suffix matching inherently guarding false positives like `contest.py` or `latest_utils.py` — extended locally with root-level `conftest.py`. Duplication with `_is_test_path` is accepted; extracting a shared module is deliberately deferred to a separate future issue.

### v1 scope limits

* Exact `missing_lines` only. Branch-partial coverage (`partial_branches`) is **intentionally out of scope** — v1 asks "is the line executed at all?", not "are all branches exercised?".
* Untracked files are invisible (`git diff` cannot see them); the gate judges tracked changes.
* Orchestrator-Repository tooling only: it does NOT run inside the language-agnostic Verification path, so **Language Agnosticism** (CONTEXT.md) is unaffected; target repositories keep their own language-appropriate gates.

## Edge cases

Each covered by `scripts/test_diff_coverage_gate.py`: empty diff → 0; diverged base → merge-base scoping judges only this branch's changes; newly added production file zero-covered → all statement lines listed; changed production file absent from report → file-level FAIL; test-location-only changes → 0; deletions and pure renames ignored; non-Python files skipped; collapsed-range output; pragma-excluded lines pass naturally (they never appear in `missing_lines`); missing/unparseable JSON → 2; invalid base ref → 2.

## Consequences

* **Pros**: instant, free, exact changed-line feedback at commit time; eliminates the dominant mechanical judge-FAIL class and its Merge-Fix Loop round-trips; the judge's Q4 remains as defense-in-depth until the planned follow-up retires it once the gate is merge-blocking.
* **Negatives**: a second implementation of test-path detection lives in `scripts/` until a shared module extraction; `-U0` diff parsing is hand-rolled (mitigated by quoted-path handling and tests); developers must remember to run `make diff-coverage` (opt-in by design, mirroring the Strict Pre-Commit Gate tiering).

## Inspiration & References

* **diff-cover (Bachmann1234/diff_cover)** — [github.com/Bachmann1234/diff_cover](https://github.com/Bachmann1234/diff_cover) — Source Quality Tier T2 (official project docs/repository), accessed 2026-08-24. Verified live via web search: confirms the tool compares an XML coverage report with `git diff` output and requires Cobertura XML — the basis for Option 2's rejection (dependency cost + format mismatch). Its motto ("if you touch a line of code, that line should be covered") independently validates the threshold decision.
* **Coverage.py JSON reporting** — [coverage.readthedocs.io/en/latest/commands/cmd_json.html](https://coverage.readthedocs.io/en/latest/commands/cmd_json.html) — Tier T1 (official docs), accessed 2026-08-24. Command surface verified live; the per-file schema (`executed_lines`, `missing_lines`, `excluded_lines`) was additionally verified **empirically** by generating a report with the repo's own coverage 7.15.3 and inspecting the keys — the ground truth the parser relies on.
* **SonarSource: Clean as You Code / Coverage on New Code** — [community.sonarsource.com/t/best-practices-for-increasing-code-coverage/21423](https://community.sonarsource.com/t/best-practices-for-increasing-code-coverage/21423) — Tier T2 (vendor engineering community, official SonarSource staff responses), accessed 2026-08-24. Verified live: SonarSource's position that focusing coverage enforcement on New Code is the sustainable path; also surfaced their small-change leniency, which we consciously reject (see Threshold semantics).
* **ADR-0025: Deterministic Syntax Pre-Check for LLM Syntax/Lint Judge** — the direct precedent for moving a mechanically-checkable judge criterion out of the LLM into deterministic verification.
