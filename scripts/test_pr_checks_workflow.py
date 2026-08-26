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

Since ADR-0057 the workflow is also callable as a REUSABLE WORKFLOW by
target repositories. That added a second class of invariant: the
repository-specific bits are input-parameterized WITH DEFAULTS THAT
REPRODUCE THIS REPOSITORY'S NATIVE BEHAVIOR EXACTLY, conditional steps may
only ever be skipped by an explicit input toggle or an event-type guard —
never by ``always()``-style unconditional fallbacks that would silently
downgrade the tiered gates.

See ADR-0052 for the gate design, ADR-0020 for the tiered-gate model, and
ADR-0057 for the reusable-workflow decision.
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


def _workflow_call_inputs(wf: dict[str, Any]) -> dict[str, Any]:
    call = _triggers(wf).get("workflow_call")
    assert isinstance(call, dict), "workflow must expose a workflow_call trigger"
    inputs = call.get("inputs")
    assert isinstance(inputs, dict) and inputs, "workflow_call must declare inputs"
    return inputs


def _steps(wf: dict[str, Any]) -> list[dict[str, Any]]:
    steps = wf["jobs"]["pr-checks"]["steps"]
    assert isinstance(steps, list) and steps, "job must declare steps"
    return steps


def _step_by_name(wf: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [s for s in _steps(wf) if s.get("name") == name]
    assert matches, f"missing step: {name}"
    return matches[0]


def test_has_no_direct_triggers() -> None:
    # ADR-0057 (PR #174): the workflow is PURE reusable. Dual triggering made
    # every conditional silently mode-dependent because `github.event_name`
    # inside a called workflow mirrors the CALLER's event — there is no
    # expression that detects being called. This repository's own PRs run it
    # via the thin caller `.github/workflows/ci.yml` instead.
    on = _triggers(_load_workflow())
    # Single-line assert by design (ruff version skew, PR #151/#173).
    assert list(on.keys()) == ["workflow_call"], "single trigger only"


def test_exposes_workflow_call_trigger() -> None:
    # ADR-0057: target repositories invoke this workflow via `uses:`.
    on = _triggers(_load_workflow())
    assert "workflow_call" in on, "workflow must be callable as a reusable workflow"


def test_caller_secrets_are_optional_named_inputs() -> None:
    # Callers wire the declared names (or pass `secrets: inherit` from a
    # same-repo caller, as this repository's ci.yml does).
    call = _triggers(_load_workflow())["workflow_call"]
    secrets = call.get("secrets")
    assert isinstance(secrets, dict), "workflow_call must declare its secrets"
    has_judge_secret = "openrouter-api-key" in secrets and "gh-pat" in secrets
    assert has_judge_secret, "caller-side judge secret names must be declared"
    all_optional = all(not s.get("required", False) for s in secrets.values())
    assert all_optional, "optional secrets keep minimal callers valid"


def test_reusable_input_defaults_preserve_local_behavior() -> None:
    # The refactor must be a no-op for this repository's own CI: every new
    # input defaults to exactly what the pre-ADR-0057 workflow hardcoded.
    inputs = _workflow_call_inputs(_load_workflow())
    floor_is_native = inputs["coverage-floor"]["default"] == 89
    assert floor_is_native, "coverage floor default drifted from 89"
    assert inputs["enable-diff-gate"]["default"] is True, "gate must stay on by default"
    assert inputs["enable-llm-review"]["default"] is True, "judges must stay on"
    tree_sitter_on = inputs["prefetch-tree-sitter"]["default"] is True
    assert tree_sitter_on, "native runs need the tree-sitter prefetch"
    gitleaks_off = inputs["enable-gitleaks"]["default"] is False
    assert gitleaks_off, "dedicated secret-scan.yml already covers this repository"
    extras = str(inputs["extra-pip-packages"]["default"])
    otel_stack_present = "opentelemetry-api" in extras
    otel_stack_present = (
        otel_stack_present and "openinference-semantic-conventions" in extras
    )
    assert otel_stack_present, "native runs need the OTel judge stack by default"


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
    # deliberate act rather than silent drift. ADR-0057 inserted the opt-in
    # Gitleaks step right after checkout so it needs no Python setup.
    names = [s.get("name") for s in _steps(_load_workflow())]
    assert names == [
        "Check out repository",
        "Run Gitleaks",
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
    expands_cov_paths = "--cov=$path" in run
    assert expands_cov_paths, "cov paths must expand into repeated --cov flags"
    floor_wired_to_input = "--cov-fail-under=${{ inputs.coverage-floor || 89 }}" in run
    assert floor_wired_to_input, "percentage floor must stay driven by its input"


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


def test_gate_step_runs_directly_after_pytest() -> None:
    wf = _load_workflow()
    steps = _steps(wf)
    names = [s.get("name") for s in steps]
    gate = _step_by_name(wf, GATE_STEP)
    gate_directly_after_pytest = names.index(GATE_STEP) == names.index(PYTEST_STEP) + 1
    assert gate_directly_after_pytest, "gate must run directly after pytest"
    assert not gate.get("continue-on-error"), "the gate is a hard check"


def test_gate_step_skip_conditions_are_scoped_to_reusability() -> None:
    # ADR-0057 refinement of the former blanket "no `if:`" invariant: after a
    # pytest failure the job is red regardless, so the only legitimate skips
    # are (a) non-PR events — push builds have no PR diff base and remain
    # guarded by the --cov-fail-under floor — and (b) explicit caller
    # opt-out. Anything else (notably always()) would downgrade the gate.
    gate = _step_by_name(_load_workflow(), GATE_STEP)
    gate_if = str(gate.get("if", ""))
    scoped_to_pr_events = "github.event_name == 'pull_request'" in gate_if
    assert scoped_to_pr_events, "gate must be scoped to pull_request events"
    honors_opt_out = "inputs.enable-diff-gate" in gate_if
    # Single-line assert by design: the pinned and latest ruff formatters
    # disagree on wrapping long assert messages (PR #151 / #173 skew).
    assert honors_opt_out, "callers must be able to opt out via enable-diff-gate"


def test_llm_review_skipped_when_deterministic_checks_fail() -> None:
    # Cost control: the LLM judges run only when every deterministic gate
    # above them is green. A failing lint/mypy/pytest/diff-coverage step
    # already blocks the merge deterministically, so a judge pass over that
    # diff adds token cost without authority. (The former `if: always()`
    # was a pre-ADR-0025/ADR-0052 visibility fallback; observed 2026-08 on
    # PR #153: a diff-coverage failure still triggered a full judge review.)
    # Under ADR-0057 the step's ONLY permitted condition is the bare input
    # toggle — default success gating stays intact because the toggle never
    # references job outcome.
    review = _step_by_name(_load_workflow(), REVIEW_STEP)
    # Boolean variables + short messages: stable across ruff formatter
    # versions (see PR #151).
    bare_input_toggle_only = review.get("if") == "inputs.enable-llm-review"
    assert bare_input_toggle_only, "review condition must be the bare input toggle"
    assert not review.get("continue-on-error"), "a failed judge run fails the job"


def test_gitleaks_step_is_strictly_opt_in() -> None:
    # This repository's dedicated secret-scan.yml covers push + pull_request;
    # the in-workflow Gitleaks step exists purely for target repositories, so
    # its condition must be exactly the input toggle.
    gitleaks = _step_by_name(_load_workflow(), "Run Gitleaks")
    opt_in_comparison = gitleaks.get("if") == "inputs.enable-gitleaks"
    assert opt_in_comparison, "Gitleaks must gate on its bare enable-gitleaks input"
    # Contract (not mechanism): the scan runs under this workflow's read-only
    # token policy, so the step must not inject credentials, and a finding
    # must fail the job. Any scanner invocation satisfying both is valid.
    injects_credentials = "GITHUB_TOKEN" in str(gitleaks.get("env", {}))
    assert not injects_credentials, "scan must not require elevated scopes"
    is_hard_gate = not gitleaks.get("continue-on-error")
    assert is_hard_gate, "a leak finding must fail the job"
    scans_secrets = "gitleaks" in str(gitleaks.get("run", ""))
    assert scans_secrets, "the step must invoke the gitleaks scanner"


def test_no_step_uses_always_gating() -> None:
    # Tiered-gate backstop: `always()` was how the judges used to burn tokens
    # on red diffs (PR #153). No step in the enforcement layer may resurrect
    # unconditional execution. Parsed-conditionals scope keeps historical
    # mentions inside explanatory comments from tripping the pin.
    wf = _load_workflow()
    offenders = [
        f"{job_id}/{step.get('name')}"
        for job_id, job in wf["jobs"].items()
        for step in job.get("steps", [])
        if "always()" in str(step.get("if", ""))
    ]
    assert offenders == [], "steps with always() gating defeat the tiered gates"


def test_boolean_conditions_are_bare_truthiness() -> None:
    # ADR-0057 (PR #174): with workflow_call as the only trigger, `inputs` is
    # always materialized and bare truthiness is exact. The two earlier guard
    # styles were both broken in one execution mode or the other (PR #173):
    # bare conditions under dual triggers skipped steps natively ('' is
    # falsy), and typed comparisons like `!= false` skip called steps because
    # GitHub compares numerically (`'' == false`). Allowed condition shapes
    # are exactly the bare toggle and the bare toggle plus the gate's
    # pull_request scope.
    allowed_exact = {
        "inputs.enable-gitleaks",
        "inputs.enable-llm-review",
        "inputs.prefetch-tree-sitter",
        "inputs.enable-secret-scan-script",
        "inputs.enable-semgrep",
        "inputs.enable-pip-audit",
        "inputs.enable-diff-gate && github.event_name == 'pull_request'",
    }
    offenders = []
    for job_id, job in _load_workflow()["jobs"].items():
        for step in job.get("steps", []):
            step_if = str(step.get("if", ""))
            if "inputs." in step_if and step_if not in allowed_exact:
                offenders.append(f"{job_id}/{step.get('name')}: {step_if}")
    assert offenders == [], "conditions must be pinned bare-truthiness forms"


def test_string_interpolations_keep_fallbacks() -> None:
    # Optional string/number inputs may be omitted by callers; every
    # interpolation in run/env/with must therefore carry its `|| default`.
    wf = _load_workflow()
    offenders = []
    for job_id, job in wf["jobs"].items():
        for step in job.get("steps", []):
            for key in ("run", "env", "with"):
                block = str(step.get(key, ""))
                if "inputs." in block and " || " not in block:
                    offenders.append(f"{job_id}/{step.get('name')} ({key})")
    assert offenders == [], "string/number inputs need || fallbacks"


CALLER_PATH = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
)


def _load_caller() -> dict[str, Any]:
    assert CALLER_PATH.exists(), f"missing caller workflow: {CALLER_PATH}"
    with CALLER_PATH.open() as f:
        return yaml.safe_load(f)


def test_self_caller_invokes_reusable_workflow_with_inherited_secrets() -> None:
    # ADR-0057: this repository's own PR coverage runs through the reusable
    # workflow; the caller must forward secrets and mirror the historical
    # behavior via explicit inputs.
    caller = _load_caller()
    job = caller["jobs"]["ci"]
    calls_self = str(job.get("uses", "")).endswith("./.github/workflows/pr-checks.yml")
    assert calls_self, "caller must invoke the local reusable pr-checks workflow"
    inherits = job.get("secrets") == "inherit"
    assert inherits, "caller must inherit repo-level judge secrets"
    with_block = job.get("with", {})
    floor_matches_history = with_block.get("coverage-floor") == 89
    assert floor_matches_history, "self-caller must pin the historical 89% floor"
    judges_on = with_block.get("enable-llm-review") is True
    assert judges_on, "this repository keeps running the LLM judges on its PRs"
    gitleaks_off = with_block.get("enable-gitleaks") is False
    assert gitleaks_off, "dedicated secret-scan.yml already covers this repository"
