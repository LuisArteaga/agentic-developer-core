"""Structural contract tests for the PR checks workflow.

The ``pr-checks`` workflow (``.github/workflows/pr-checks.yml``) is the CI
enforcement layer of the tiered quality-gate model (ADR-0020). Since issue
#148 it is also the authoritative enforcement point of the Diff Coverage
Gate (ADR-0052): the pytest step must emit ``coverage.json`` from a single
test run, and a gate step must fail the job deterministically when changed
production lines are uncovered — before any LLM judge tokens are spent.
Since issue #149 the ADR-0030 CI-coverage-output transport is retired: no
``CI_COVERAGE_OUTPUT`` plumbing may reappear (the Test Coverage judge
evaluates semantics from the diff alone). Drift in any of these invariants
either silently disables the hard gate or reintroduces retired machinery,
so they are pinned at the ``make verify`` surface.

See ADR-0052 for the gate design and ADR-0020 for the tiered-gate model.
"""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pr-checks.yml"
)

PYTEST_STEP = "Run Pytest Coverage Check"
GATE_STEP = "Run Diff Coverage Gate"
REVIEW_STEP = "Run LLM review"


def _load_workflow() -> dict[str, Any]:
    assert WORKFLOW_PATH.exists(), f"missing workflow: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open() as f:
        return yaml.safe_load(f)


def _triggers(wf: dict[str, Any]) -> dict[str, Any]:
    # PyYAML parses the bare YAML key `on:` as the boolean True (YAML 1.1 spec),
    # so look it up under both the string and the boolean key.
    raw: dict[Any, Any] = wf
    on = raw.get("on")
    if on is None:
        on = raw.get(True)
    assert isinstance(on, dict), "workflow must define a trigger map under `on:`"
    return on


def _steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    steps = wf["jobs"]["pr-checks"]["steps"]
    assert isinstance(steps, list) and steps, "job must declare steps"
    return steps


def _step_by_name(wf: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in _steps(wf) if s.get("name") == name]
    assert matches, f"missing step: {name}"
    return matches[0]


def test_triggers_on_pull_request() -> None:
    on = _triggers(_load_workflow())
    assert "pull_request" in on, "workflow must trigger on pull_request events"


def test_checkout_fetches_full_history() -> None:
    # The gate resolves `git merge-base <base.sha> HEAD`, which requires the
    # base commit object to exist locally.
    wf = _load_workflow()
    checkout = _step_by_name(wf, "Check out repository")
    assert checkout.get("uses", "").startswith("actions/checkout")
    assert checkout["with"]["fetch-depth"] == 0


def test_step_ordering_is_stable() -> None:
    # The acceptance contract for #148: existing steps keep their relative
    # order; the gate slot sits directly after the pytest step that produces
    # its input. Pinning the full ordered list makes reordering a visible,
    # deliberate act rather than silent drift.
    names = [s.get("name") for s in _steps(_load_workflow())]
    assert names == [
        "Check out repository",
        "Validate OpenRouter API key",
        "Set up Python",
        "Install dependencies",
        "Cache tree-sitter parsers",
        "Prefetch tree-sitter parsers",
        "Run secret scan",
        "Run Ruff Lint & Format Checks",
        "Run Mypy Typecheck",
        PYTEST_STEP,
        GATE_STEP,
        "Run Semgrep Security Scan",
        "Run Pip-Audit Dependency Check",
        "Run LLM review",
    ]


def test_pytest_step_emits_json_report_and_readable_output() -> None:
    # The JSON report feeds the Diff Coverage Gate from a single test run;
    # term-missing keeps the per-file missing-line table readable in CI logs.
    run = str(_step_by_name(_load_workflow(), PYTEST_STEP)["run"])
    assert "--cov-report=term-missing" in run, "readable coverage table lost"
    assert "--cov-report=json:coverage.json" in run, "gate report not emitted"
    assert "set -o pipefail" in run, "pipefail semantics must be preserved"
    assert "| tee" not in run, "ADR-0030 output capture must stay retired"
    assert "--cov-fail-under=89" in run, "global percentage floor must be preserved"


def test_no_ci_coverage_transport_remains() -> None:
    # ADR-0030 retirement (issue #149): the env var, the staged file, and any
    # judge-consumed coverage transport must not reappear anywhere in the
    # workflow — changed-line coverage is the Diff Coverage Gate's job.
    raw = WORKFLOW_PATH.read_text()
    assert "CI_COVERAGE_OUTPUT" not in raw, "retired ADR-0030 transport reintroduced"
    assert "ci_coverage_output.txt" not in raw, "retired staging file reintroduced"


def test_gate_step_invokes_script_against_event_payload_base() -> None:
    run = str(_step_by_name(_load_workflow(), GATE_STEP)["run"])
    assert "scripts/diff_coverage_gate.py" in run, "gate script not invoked"
    assert "--coverage-json coverage.json" in run, "gate report path not passed"
    # Base SHA must come from the event payload so non-default base branches
    # and forked-head PRs resolve correctly; a hardcoded branch name breaks
    # both. Single-line asserts are stable across ruff formatter versions.
    uses_event_payload_base = "${{ github.event.pull_request.base.sha }}" in run
    assert uses_event_payload_base, "base SHA must come from the event payload"
    assert "--base main" not in run, "base branch must never be hardcoded"


def test_gate_step_runs_unconditionally_after_pytest() -> None:
    wf = _load_workflow()
    steps = _steps(wf)
    names = [s.get("name") for s in steps]
    gate = _step_by_name(wf, GATE_STEP)
    gate_directly_after_pytest = names.index(GATE_STEP) == names.index(PYTEST_STEP) + 1
    assert gate_directly_after_pytest, "gate must run directly after pytest"
    no_if_condition = "if" not in gate
    assert no_if_condition, "no `if:` — after pytest failure the job is red"
    assert not gate.get("continue-on-error"), "the gate is a hard check"


def test_llm_review_skipped_when_deterministic_checks_fail() -> None:
    # Cost control: the LLM judges run only when every deterministic gate
    # above them is green. A failing lint/mypy/pytest/diff-coverage step
    # already blocks the merge deterministically, so a judge pass over that
    # diff adds token cost without authority. (The former `if: always()`
    # was a pre-ADR-0025/ADR-0052 visibility fallback; observed 2026-08 on
    # PR #153: a diff-coverage failure still triggered a full judge review.)
    review = _step_by_name(_load_workflow(), REVIEW_STEP)
    # Boolean variables + short messages: stable across ruff formatter
    # versions (see PR #151).
    uses_default_gating = "if" not in review
    assert uses_default_gating, "LLM review must run only on green gates"
    assert not review.get("continue-on-error"), "a failed judge run fails the job"
