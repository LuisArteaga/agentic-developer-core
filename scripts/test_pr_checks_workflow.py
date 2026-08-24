"""Structural contract tests for the PR checks workflow.

The ``pr-checks`` workflow (``.github/workflows/pr-checks.yml``) is the CI
enforcement layer of the tiered quality-gate model (ADR-0020). Since issue
#148 it is also the authoritative enforcement point of the Diff Coverage
Gate (ADR-0052): the pytest step must emit ``coverage.json`` alongside the
term-missing output the Test Coverage judge consumes (ADR-0030), and a gate
step must fail the job deterministically when changed production lines are
uncovered — before any LLM judge tokens are spent. Drift in any of these
invariants either silently disables the hard gate or breaks the judge's
coverage transport, so they are pinned at the ``make verify`` surface.

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


def test_pytest_step_emits_json_report_alongside_term_missing() -> None:
    # ADR-0030 transport (term-missing piped through tee) must survive; the
    # JSON report feeds the Diff Coverage Gate from the same single test run.
    run = str(_step_by_name(_load_workflow(), PYTEST_STEP)["run"])
    assert "--cov-report=term-missing" in run, "judge coverage output lost"
    assert "--cov-report=json:coverage.json" in run, "gate report not emitted"
    assert "set -o pipefail" in run, "pipefail semantics must be preserved"
    assert "| tee" in run, "judge output capture must be preserved"
    assert "--cov-fail-under=89" in run, "global percentage floor must be preserved"


def test_gate_step_invokes_script_against_event_payload_base() -> None:
    run = str(_step_by_name(_load_workflow(), GATE_STEP)["run"])
    assert "scripts/diff_coverage_gate.py" in run, "gate script not invoked"
    assert "--coverage-json coverage.json" in run, "gate report path not passed"
    assert (
        "${{ github.event.pull_request.base.sha }}" in run
    ), "base SHA must come from the event payload (non-default bases, forks)"
    assert "--base main" not in run, "base branch must never be hardcoded"


def test_gate_step_runs_unconditionally_after_pytest() -> None:
    wf = _load_workflow()
    steps = _steps(wf)
    names = [s.get("name") for s in steps]
    gate = _step_by_name(wf, GATE_STEP)
    assert (
        names.index(GATE_STEP) == names.index(PYTEST_STEP) + 1
    ), "gate must run directly after the pytest step that writes its report"
    assert (
        "if" not in gate
    ), "no `if:` condition — after a pytest failure the job is already red"
    assert not gate.get("continue-on-error"), "the gate is a hard check"
