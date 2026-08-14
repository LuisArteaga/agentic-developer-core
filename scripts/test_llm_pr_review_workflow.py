"""Structural contract tests for the reusable PR Review Judge workflow.

The reusable workflow (``.github/workflows/llm-pr-review.yml``) is the CI-side
writer of the hidden ``llm-pr-review-verdicts`` block that ``merge_node``
parses (ADR-0019). Drift in its contract silently corrupts the hard merge gate,
so these tests pin the structural invariants at the ``make verify`` surface:
trigger type, declared inputs/secrets, the orchestrator-repo checkout, the
``review.py`` invocation, env wiring, and permissions.

See ADR-0050 for the design rationale.
"""

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / ".github"
    / "workflows"
    / "llm-pr-review.yml"
)


def _load_workflow() -> dict[str, Any]:
    assert WORKFLOW_PATH.exists(), f"missing reusable workflow: {WORKFLOW_PATH}"
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


def test_workflow_is_reusable_via_workflow_call() -> None:
    on = _triggers(_load_workflow())
    assert "workflow_call" in on, "reusable workflow must trigger on workflow_call"


def test_declared_inputs() -> None:
    on = _triggers(_load_workflow())
    inputs = on["workflow_call"].get("inputs", {})
    expected = {
        "orchestrator-repo",
        "orchestrator-ref",
        "diff-exclude",
        "coverage-artifact-name",
        "coverage-artifact-path",
    }
    assert expected <= set(inputs), f"missing inputs: {expected - set(inputs)}"
    # The default repo pins the writer of the verdict block (ADR-0019 lockstep).
    assert (
        inputs["orchestrator-repo"]["default"] == "LuisArteaga/agentic-developer-core"
    )


def test_declared_secrets_required() -> None:
    on = _triggers(_load_workflow())
    secrets = on["workflow_call"].get("secrets", {})
    assert secrets["judge-token"]["required"] is True
    assert secrets["openrouter-api-key"]["required"] is True


def test_permissions_allow_review_posting() -> None:
    wf = _load_workflow()
    perms = wf.get("permissions", {})
    assert perms.get("pull-requests") == "write"
    assert perms.get("contents") == "read"


def test_checks_out_orchestrator_repo_and_target_repo() -> None:
    wf = _load_workflow()
    steps = wf["jobs"]["llm-pr-review"]["steps"]
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkouts) >= 2, "must check out target repo and orchestrator repo"
    orch_checkout = next(
        (c for c in checkouts if c.get("with", {}).get("repository")), None
    )
    assert orch_checkout is not None, "no checkout pins the orchestrator repository"
    assert orch_checkout["with"]["repository"] == "${{ inputs.orchestrator-repo }}"


def test_runs_review_py_with_pr_diff() -> None:
    wf = _load_workflow()
    steps = wf["jobs"]["llm-pr-review"]["steps"]
    run_steps = [s for s in steps if "run" in s]
    joined = "\n".join(str(s["run"]) for s in run_steps)
    assert "scripts/review.py" in joined, "must invoke scripts/review.py"
    assert "git diff" in joined, "must compute a PR diff"
    assert "github.base_ref" in joined, "diff must be anchored to the PR base ref"


def test_required_env_wired() -> None:
    wf = _load_workflow()
    env = wf["jobs"]["llm-pr-review"].get("env", {})
    assert env.get("PR_NUMBER") == "${{ github.event.pull_request.number }}"
    assert env.get("GH_TOKEN") == "${{ secrets.judge-token }}"
    assert env.get("OPENROUTER_API_KEY") == "${{ secrets.openrouter-api-key }}"
    assert env.get("AGENT_MODE") == "ci"
