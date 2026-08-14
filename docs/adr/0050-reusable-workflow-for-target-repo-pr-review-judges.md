# ADR 0050: Reusable GitHub Actions Workflow for Target-Repository PR Review Judges

* **Status**: Accepted
* **Date**: 2026-07-14
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The Merge-Node polls the **target** repository's pull requests for the hidden
`llm-pr-review-verdicts` block posted by the PR Review Judges
([ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md)). It
resolves the target repo from `GITHUB_REPOSITORY` via
`_get_github_repository()` — and even warns on self-targeting (issue #107's
`_warn_if_self_target`). Yet the judges only run in the **orchestrator**
repository's own CI (`pr-checks.yml`'s "Run LLM review" step). The consequence,
observed 2026-08-14 on `ot-telemetry-engine` PR #17, is that target-repo PRs
never receive a verdict and the Merge-Node polls until timeout.

Issue #132 asks to close this gap by triggering the judge on
`pull_request` events in the target repo via "a reusable GitHub Actions
workflow that runs `scripts/review.py` against the PR diff and posts the
combined review via the GitHub API under the trusted judge identity."

This decision is hard to reverse because the chosen delivery mechanism becomes
an installation contract that target repositories pin to a ref; changing it
later breaks every installed caller. It is surprising without context because
the judge runs from the orchestrator repository's workflow file inside a target
repository's CI (two cross-repo checkouts). And it is the result of a real
trade-off between three credible delivery options (below).

## Decision Drivers

* **Writer/Parser Lockstep (ADR-0019)**: the verdict-block writer
  (`scripts/review.py`) and parser (`merge_node`) are coupled. Target repos
  must invoke the *same* canonical `review.py` the orchestrator ships, not a
  copy that can drift.
* **Repo-Agnosticism of `review.py`**: the judge already reads the diff from
  stdin, posts via `gh` using an env-based token, and derives enrichment inputs
  from `GITHUB_WORKSPACE` rather than assuming its own repository. No code
  change to `review.py` should be required (verified during design).
* **Spoofing Guard (ADR-0014)**: reviews are trusted only when authored by
  `AGENT_TRUSTED_JUDGE_USER` and submitted after `pushed_at`. The delivery
  mechanism must post under a token whose owner the orchestrator can match.
* **Per-Repo Autonomy**: each target repository must own its trigger conditions
  and secrets; the orchestrator must not assume write access to target-repo
  workflow files.
* **Graceful Degradation (ADR-0030)**: target repos without a CI coverage
  report must still get a valid (diff-only) verdict; the mechanism must not
  hard-require coverage.

## Considered Options

* **Option 1: Reusable workflow (`workflow_call`) hosted in the orchestrator repo**
  A workflow file under the orchestrator repo's `.github/workflows/` triggered
  by `workflow_call`. Target repos call it via
  `uses: owner/repo/.github/workflows/llm-pr-review.yml@ref`. Inside the called
  workflow the `github` context is the caller's (target repo's PR), so one
  checkout fetches the target repo and a second fetches the canonical
  `review.py`.
* **Option 2: Copied workflow + `review.py` into each target repo**
  Each target repo duplicates the workflow and `review.py` (or vendors a
  snapshot). No cross-repo checkout, but the copy drifts from the canonical
  writer — directly violating ADR-0019 lockstep — and onboarding requires
  manual file sync on every judge update.
* **Option 3: Published GitHub Action (composite or Docker)**
  Package the judge as an Action (e.g. `uses: owner/judge-action@v1`) that
  runs `review.py` in its own runtime. Strongest versioning story, but
  requires a release/distribution pipeline (tagging, marketplace or
  release-based `@v1` major-tag tracking) the project does not currently
  maintain, and a Docker action would re-build the heavy Python+tree-sitter
  image on every run unless published to a registry.

## Decision

We chose **Option 1**.

The orchestrator repository ships `.github/workflows/llm-pr-review.yml`, a
`workflow_call` workflow that:

1. Checks out the target repository at the PR head (`fetch-depth: 0`) — this is
   the caller's `github` context, supplying the diff and the `GITHUB_WORKSPACE`
   used for enclosing-function-context enrichment
   ([ADR-0022](./0022-enclosing-function-context-enrichment-for-pr-review-judges.md)).
2. Checks out the orchestrator repository at a caller-pinned ref
   (`orchestrator-ref`, default `main`) to obtain the canonical
   `scripts/review.py` and its dependencies — preserving ADR-0019 lockstep by
   construction: every caller invokes the identical writer.
3. Installs the orchestrator runtime dependencies + OpenTelemetry, prefetches
   tree-sitter parsers (best-effort, `continue-on-error`), and pipes
   `git diff -w origin/<base>...HEAD` into `python3 .../review.py`.
4. Posts the combined review via `gh pr review` using the caller-supplied
   `judge-token`, whose owner must equal `AGENT_TRUSTED_JUDGE_USER`.

Inputs let callers exclude pathspecs from the diff and optionally pipe a CI
coverage artifact (`coverage-artifact-name` / `coverage-artifact-path`) into
`CI_COVERAGE_OUTPUT_PATH` per ADR-0030; absent coverage degrades to diff-only
evaluation.

The reusable workflow is `workflow_call`-only — it deliberately defines **no**
`pull_request` trigger. Each target repository owns its caller workflow and
chooses when to invoke the judge (opened/synchronize). This keeps the trigger
decision per-repo and avoids the orchestrator imposing a policy on target
repos. Running on all PRs (rather than only orchestrator-authored ones) is
acceptable: the trusted-author guard prevents spoofing, and judging human PRs
is a feature whose cost is the target owner's choice.

`review.py` is unchanged: it is already repo-agnostic (diff from stdin,
`GITHUB_WORKSPACE`-relative enrichment, `__file__`-relative imports). This was
verified during design and is a deliberate non-change to minimize risk to the
writer/parser contract.

### Consequences

* **Pros**:
  * Single canonical writer — target repos cannot drift from the parser's
    expected block format.
  * Minimal onboarding: one small caller workflow + two secrets per target repo.
  * No new distribution pipeline (vs. Option 3); no per-repo file sync (vs.
    Option 2).
  * `review.py` untouched — zero risk to the existing orchestrator-repo judge
    path in `pr-checks.yml`.
* **Negatives**:
  * **Caller must pin `orchestrator-ref`**: a caller on `@main` silently
    adopts judge changes. Mitigated by documenting tag pinning and by the
    ADR-0019 hidden-block format being a tested, stable contract.
  * **Two checkouts per run** add a few seconds and a second network fetch;
    negligible versus the four-judge LLM cost.
  * **`pr-checks.yml` is not refactored to call this reusable workflow** — the
    orchestrator repo keeps its inline judge step. Duplicating the install
    recipe is accepted to avoid changing the orchestrator's own CI behaviour
    out of scope. Refactoring `pr-checks.yml` to call the reusable workflow is
    a follow-up that would further guarantee identical judge paths.

## Inspiration & References

* **GitHub Docs — Reuse workflows** ([docs](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows))
  — Source Quality Tier **T1** (official docs). Access date 2026-07-14.
  Verification: confirms `on: workflow_call`, `uses: owner/repo/.github/workflows/file.yml@ref`,
  inputs/secrets passing, and that a reusable workflow lives in `.github/workflows`.
* **actions/toolkit Issue #1264 — "make reference accessible in reusable workflow"**
  ([issue](https://github.com/actions/toolkit/issues/1264))
  — Source Quality Tier **T2** (official toolkit repo). Access date 2026-07-14.
  Verification: documents that "when a reusable workflow is triggered by a
  caller workflow, the `github` context is always associated with the caller
  workflow" — the property that lets the called workflow check out the target
  repo by default and resolve `github.event.pull_request.number` /
  `github.base_ref` to the caller's PR. Also shows the two-checkout pattern
  (`repository:` + `ref:` for the library repo).
* **ADR-0019: Combined PR Review Body with Hidden Verdict Block**
  — the writer/parser coupling this decision preserves by sourcing the
  canonical `review.py`.
* **ADR-0014: PR Verification and LLM Judge Review Integration**
  — the `submitted_at >= pushed_at` freshness rule and trusted-author spoofing
  guard the reusable workflow must satisfy.
* **ADR-0030: Pipe CI Coverage Output into Test Coverage PR Judge**
  — the graceful-degradation contract the optional coverage-artifact input
  honours.
