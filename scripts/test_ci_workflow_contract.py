#!/usr/bin/env python3
"""Caller-shape contract test for .github/workflows/ci.yml (issue #187, ADR-0059).

This repository consumes the quality-gates-toolkit composite. The contract
pins the caller shape so a drift (floating ref, dropped input, broken secret
mapping, pre-commit/ci tag divergence) fails a deterministic gate instead of
degrading silently in CI.
"""

import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRE_COMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"

TOOLKIT_TAG = "v1.6.0"
TOOLKIT_COMPOSITE_USES = (
    "LuisArteaga/quality-gates-toolkit/.github/workflows/pr-checks.yml@" + TOOLKIT_TAG
)

EXPECTED_INPUTS = {
    "python-version": "3.12",
    "lint-paths": "orchestrator scripts",
    "cov-paths": "orchestrator scripts",
    "coverage-floor": 89,
    "extra-pip-packages": (
        "opentelemetry-api opentelemetry-sdk "
        "opentelemetry-exporter-otlp openinference-semantic-conventions"
    ),
    "prefetch-tree-sitter": True,
    "enable-lint": True,
    "enable-test": True,
    "enable-security": True,
    "enable-secret-scan": True,
    "enable-semgrep": True,
    "enable-pip-audit": True,
    "enable-diff-gate": True,
    "enable-llm-review": True,
    "config-path": "config/factory.json",
    "diff-exclude": "uv.lock",
}

EXPECTED_SECRETS = {
    "openrouter-api-key": "${{ secrets.OPENROUTER_API_KEY }}",
    "judge-token": "${{ secrets.GH_PAT }}",
}


class TestCIWorkflowContract(unittest.TestCase):
    """Pin the toolkit composite caller shape (tag, inputs, secrets, perms)."""

    def setUp(self):
        with CI_WORKFLOW_PATH.open(encoding="utf-8") as fh:
            self.workflow = yaml.safe_load(fh)
        self.job = self.workflow["jobs"]["ci"]

    def test_calls_toolkit_composite_at_immutable_release_tag(self):
        # toolkit D-0007: pins are immutable release tags, never floating refs.
        uses = self.job.get("uses", "")
        self.assertEqual(uses, TOOLKIT_COMPOSITE_USES)

    def test_full_documented_input_set(self):
        with_block = self.job.get("with") or {}
        self.assertEqual(with_block, EXPECTED_INPUTS)

    def test_explicit_secrets_mapping(self):
        # `secrets: inherit` does not map GH_PAT onto judge-token by name;
        # bot-authored reviews are ignored by the merge gate (ADR-0014).
        secrets_block = self.job.get("secrets") or {}
        self.assertEqual(secrets_block, EXPECTED_SECRETS)

    def test_pull_requests_write_grant_present(self):
        permissions = self.job.get("permissions") or {}
        self.assertEqual(permissions.get("contents"), "read")
        self.assertEqual(permissions.get("pull-requests"), "write")

    def test_no_legacy_local_workflow_callers_remain(self):
        workflows_dir = REPO_ROOT / ".github" / "workflows"
        retired = {"pr-checks.yml", "llm-pr-review.yml"}
        present = {p.name for p in workflows_dir.glob("*.yml")}
        self.assertEqual(present & retired, set())

    def test_ci_triggers_only_on_pull_request(self):
        triggers = self.workflow.get(True) or self.workflow.get("on") or {}
        self.assertEqual(list(triggers.keys()), ["pull_request"])


class TestPreCommitToolkitAlignment(unittest.TestCase):
    """Pre-commit toolkit rev must equal the ci.yml tag (single source)."""

    def setUp(self):
        with PRE_COMMIT_CONFIG_PATH.open(encoding="utf-8") as fh:
            self.config = yaml.safe_load(fh)

    def test_toolkit_rev_matches_ci_tag(self):
        revs = {
            repo.get("rev")
            for repo in self.config["repos"]
            if "quality-gates-toolkit" in str(repo.get("repo", ""))
        }
        self.assertEqual(revs, {TOOLKIT_TAG})

    def test_ruff_pre_commit_pinned_to_toolkit_ci_version(self):
        ruff_revs = [
            repo.get("rev")
            for repo in self.config["repos"]
            if "ruff-pre-commit" in str(repo.get("repo", ""))
        ]
        self.assertEqual(ruff_revs, ["v0.16.6"])

    def test_only_ruff_and_toolkit_hook_sources(self):
        sources = {str(repo.get("repo", "")) for repo in self.config["repos"]}
        self.assertEqual(
            sources,
            {
                "https://github.com/astral-sh/ruff-pre-commit",
                "https://github.com/LuisArteaga/quality-gates-toolkit",
            },
        )


if __name__ == "__main__":
    unittest.main()
