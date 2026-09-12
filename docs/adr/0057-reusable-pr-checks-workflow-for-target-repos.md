# ADR 0057: Reusable PR Checks Workflow for Target Repositories

* **Status**: Superseded by
  [ADR-0059](./0059-adopt-quality-gates-toolkit-canonical-quality-gates.md)
  (amendment dated 2026-09-12): `.github/workflows/pr-checks.yml` was
  retired in favor of the quality-gates-toolkit composite.
* **Date**: 2026-08-26
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The deterministic enforcement layer of the tiered quality-gate model (ADR-0020) lives in `.github/workflows/pr-checks.yml`: secret scan, ruff, mypy, the pytest coverage floor with its JSON report, the Diff Coverage Gate (ADR-0052), Semgrep, pip-audit, and — on green gates only — the LLM judges. This layer is repository-local by design: every path, package list, and threshold in it is hardcoded to *this* repository (`orchestrator scripts`, the OpenTelemetry judge stack, `--cov-fail-under=89`).

Target repositories of the orchestrator (ADR-0050) currently receive only the *semantic* half of the quality model: they wire `llm-pr-review.yml` as a reusable workflow, so judges review their diffs, but no deterministic gate ever runs there. The asymmetry is visible in practice: a target-repo PR can pass its own thin CI with zero coverage tooling while the Test Coverage judge reviews the same diff semantically — the exact division of labor ADR-0052 was written to correct ("what a script can compute exactly, no LLM should estimate"), just never applied outside this repository.

The first concrete consumer is `ot-telemetry-engine`, whose CI runs bare `pytest` with no coverage measurement at all.

## Decision Drivers

* **Deterministic-first principle** (ADR-0025 / ADR-0052 precedent): target repos need the mechanical gates before the paid judges, not instead of them.
* **Single source of enforcement**: forking the gate logic into per-repo copies would strand ADR-0052's contract from its tests; drift between copies silently weakens some repos' gates.
* **Zero behavior change for native runs**: this workflow is the merge-blocking CI of the orchestrator itself; any default that differs from today's native run is a regression.
* **Least privilege**: called workflows may narrow but never widen token scopes; the reusable surface must declare explicit minimal permissions.

## Considered Options

* **Option 1: Per-repo copies of the gate script + steps (rejected)** — each target repo vendors its own `ci.yml`.
  * *Pros*: zero coupling; each repo evolves freely.
  * *Cons*: N copies of the ADR-0052 arithmetic to maintain; fixes land here and nowhere else; the contract tests that pin the workflow's invariants cover exactly one copy.

* **Option 2: Keep static checks repo-local; target repos stay judge-only (rejected)** — status quo.
  * *Pros*: no change.
  * *Cons*: the semantic/deterministic split of ADR-0020 exists only in one repository; every new target repo re-implements or omits coverage enforcement.

* **Option 3: Parameterize `pr-checks.yml` into a dual-trigger reusable workflow (chosen)** — add `workflow_call` beside the existing `pull_request` trigger, lifting every repository-specific literal into an input whose default reproduces native behavior byte-for-byte.

## Decision

We chose **Option 3**.

Contract:

1. **Inputs, not conditionals, carry variability.** Python version, lint/mypy paths, coverage paths, coverage floor, post-install package list, and step toggles are `workflow_call` inputs. Defaults reproduce this repository's native run exactly (floor `89`; OTel stack installed; tree-sitter prefetch on; secret-scan script on; judges on).
2. **Steps may be skipped only by an explicit input toggle or an event-type guard — never by `always()`-style fallbacks.** The Diff Coverage Gate additionally requires a `pull_request` event because the merge-base diff does not exist on push builds; the global coverage floor still guards those. This refines the former "gate has no `if:`" invariant pinned by the contract tests.
3. **Secrets move out of workflow-level `env`.** The OpenRouter key and PAT resolve via `${{ secrets.REPO_LEVEL || secrets.declared-input }}` inside the steps that consume them, so native pull_request runs read repo secrets directly while callers pass declared optional secrets. Steps that need neither secret (everything except key validation and the LLM review) run without any token beyond `contents: read`.
4. **Gitleaks becomes an opt-in step** (`enable-gitleaks`, default off) because the orchestrator already scans via its dedicated `secret-scan.yml`; callers without one enable it through the same workflow.
5. **The LLM review step stays orchestrator-native.** Callers that want judges use the dedicated `llm-pr-review.yml` reusable workflow (ADR-0019/ADR-0050) with its verdict-block protocol; embedding judge invocation here would fork the writer/parser lockstep surface.
6. **Semgrep and pip-audit install their tools in-step**, decoupling the workflow from caller dev-extras; for this repository the installs are idempotent no-ops.
7. **Job identity is stable**: the job id stays `pr-checks` so required-status-check names survive for both trigger modes.
8. The structural contract tests in `scripts/test_pr_checks_workflow.py` pin all of the above at `make verify` surface, including the defaults table.

### Decision (2) — Pure-reusable architecture (revised on PR #174)

Making the workflow dual-trigger (native `pull_request` + `workflow_call`)
failed twice in CI before the final shape emerged:

1. Bare `${{ inputs.x }}` references: the `inputs` context is EMPTY outside
   `workflow_call`, so native runs lost their lint scope and skipped
   toggled steps entirely.
2. Typed comparisons (`inputs.x != false`): GitHub casts comparison operands
   to numbers, and both `''` and `false` cast to 0 — so native runs evaluated
   `'' != false` as FALSE. Every default-on step, judges included, silently
   skipped.
3. Mode guards (`github.event_name != 'workflow_call' || ...`): inside a
   called workflow the `github` context belongs to the CALLER, so
   `event_name` is e.g. `pull_request`, never `'workflow_call'`. No built-in
   expression distinguishes called from direct execution.

The robust resolution is architectural, not expression-level: **the workflow
is pure reusable** — `workflow_call` is its only trigger, `inputs` is always
fully materialized, and bare-truthiness conditions (`if: inputs.enable-x`)
are exact. This repository's own pull requests run it through the thin
self-caller `.github/workflows/ci.yml` (`secrets: inherit`, explicit inputs
mirroring historical behavior: floor 89, tree-sitter prefetch, secret-scan
script, semgrep, pip-audit, LLM review; gitleaks stays off in favor of the
dedicated secret-scan.yml). String/number interpolations keep
`${{ inputs.x || 'default' }}` fallbacks so callers may omit optional knobs;
callers wanting no extra packages pass the sentinel token `none`.

The contract suite pins all of this structurally: single-trigger invariant,
bare-truthiness condition shapes, interpolation fallbacks, and the
self-caller's explicit inputs (`test_self_caller_invokes_reusable_workflow_…
`). Note the required-check name changes from `pr-checks` (direct) to
`ci / pr-checks` (called) — branch protection must reference the latter.

## Consequences

* Target repositories onboard the full deterministic layer with one caller job and ~10 input lines; `ot-telemetry-engine` is the reference consumer.
* The workflow file now has two audiences; every future edit must keep native-run defaults byte-compatible or justify a deliberate cross-repo behavior change in this ADR.
* Version skew between this workflow (`@main`) and caller assumptions is managed the same way as ADR-0050: callers may pin a ref, and breaking input changes require removing retired inputs per the ADR-0019 lockstep discipline.
