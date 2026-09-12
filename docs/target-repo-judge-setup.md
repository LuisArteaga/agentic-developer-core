# Onboarding a Target Repository to the PR Review Judges

The orchestrator's **Merge-Node** polls the *target* repository's pull requests
for the hidden `llm-pr-review-verdicts` block posted by the PR Review Judges
([ADR-0019](./adr/0019-combined-pr-review-body-with-hidden-verdict-block.md)).
By default the judges only run in the orchestrator repository's own CI, so
target-repo PRs never receive a verdict and the Merge-Node times out. This
guide wires the judges into a target repository via the **reusable workflow**
defined at `.github/workflows/llm-pr-review.yml` ([ADR-0050](./adr/0050-reusable-workflow-for-target-repo-pr-review-judges.md))).

> **Note (2026-09-12, ADR-0059):** the canonical judge and deterministic-gate
> provider is now the public
> [quality-gates-toolkit](https://github.com/LuisArteaga/quality-gates-toolkit)
> — new target repositories should call
> `LuisArteaga/quality-gates-toolkit/.github/workflows/pr-checks.yml@<tag>`
> with `enable-llm-review: true` instead of the orchestrator's retired local
> workflow. The verdict-block protocol (ADR-0019) and trusted-identity
> semantics (ADR-0014) are unchanged. The instructions below remain as the
> historical orchestrator-workflow setup.

## How it works

1. The target repository adds a small *caller* workflow that triggers on
   `pull_request` and calls the orchestrator's reusable workflow.
2. The reusable workflow checks out the target repo (for the PR diff) **and**
   the orchestrator repo (for the canonical `scripts/review.py`), then runs the
   four judges and posts a single combined review.
3. The review is authored by the trusted judge identity; the orchestrator's
   `AGENT_TRUSTED_JUDGE_USER` must match that author or the Merge-Node ignores
   it (spoofing guard, [ADR-0014](./adr/0014-pr-verification-and-llm-judge-review-integration.md)).

## Step 0 — Grant cross-repo access to the reusable workflow

Because this repository is private, it must explicitly allow other
repositories to invoke its reusable workflows:

1. In **this** (source) repository: **Settings → Actions → General →**
   scroll to the **Access** section (below "Actions permissions") →
   select *Accessible from repositories owned by `<owner>`*.
2. Do **not** confuse this with the "Actions permissions" radios at the top of
   the same page. That policy governs which actions/workflows may run *inside*
   a repository; the Access section governs who may call *into* it. Setting
   `local_only` here does not block callers — but it breaks this repository's
   own CI, because GitHub-owned actions (`actions/checkout`) no longer count
   as local.

Without this grant the target repo's caller run fails immediately as
`startup_failure` with zero jobs.

## Step 1 — Create the required secrets in the target repository

| Secret | Purpose |
| --- | --- |
| `JUDGE_GH_TOKEN` | A GitHub PAT (or App token) with `pull-requests: write` on the target repo **and read access to this orchestrator repo** (the reusable workflow checks out `scripts/review.py` with it). **Its owner must equal the orchestrator's `AGENT_TRUSTED_JUDGE_USER`.** |
| `OPENROUTER_API_KEY` | OpenRouter API key for the LLM judges. |

No secrets are committed to any repository file.

## Step 2 — Add the caller workflow

Create `.github/workflows/llm-pr-review.yml` **in the target repository**:

```yaml
name: LLM PR Review

on:
  pull_request:
    types: [opened, synchronize]

jobs:
  judge:
    uses: LuisArteaga/agentic-developer-core/.github/workflows/llm-pr-review.yml@main
    permissions:
      contents: read
      pull-requests: write
    secrets:
      judge-token: ${{ secrets.JUDGE_GH_TOKEN }}
      openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

The `permissions:` block is **required** unless the target repo's default
workflow permissions already include `pull-requests: write`: a called reusable
workflow may narrow but never widen the caller-granted `GITHUB_TOKEN`, so the
callee's `pull-requests: write` request is otherwise rejected as an escalation
— another `startup_failure` with zero jobs.

Pin `@main` to a release tag (e.g. `@v1`) once one is cut, so the verdict-block
writer stays in lockstep with the orchestrator's parser ([ADR-0019](./adr/0019-combined-pr-review-body-with-hidden-verdict-block.md)).

## Step 3 — Configure the orchestrator (judge trust)

The Merge-Node trusts a PR review **only** when its author's GitHub login equals `AGENT_TRUSTED_JUDGE_USER` (spoofing guard, ADR-0014). This is an **orchestrator-side** environment variable — set it wherever the orchestrator runs against the target repo, **not** in the target repository.

```
AGENT_TRUSTED_JUDGE_USER=<login that owns JUDGE_GH_TOKEN>
```

Where to set it:

- **Local dev / `uv run`** — add the line to the orchestrator's `.env` (copy from `.env.example`).
- **Docker** — pass it as a `-e` flag alongside the other orchestrator vars:
  ```bash
  docker run --rm \
    -e OPENROUTER_API_KEY=... -e GH_PAT=... \
    -e GITHUB_REPOSITORY=owner/target-repo \
    -e AGENT_TRUSTED_JUDGE_USER=<judge login> \
    agentic-developer-core
  ```

If `AGENT_TRUSTED_JUDGE_USER` is **unset**, the Merge-Node falls back to the orchestrator's own GitHub identity (`GET /user`, then `GITHUB_ACTOR`). If that identity differs from the account that owns `JUDGE_GH_TOKEN`, the judge's reviews are posted under one login while the Merge-Node trusts another — the verdicts are ignored and the poll times out. This is exactly the gap the integration closes, so always set `AGENT_TRUSTED_JUDGE_USER` to the `JUDGE_GH_TOKEN` owner.

The Merge-Node also only considers reviews whose `submitted_at` is on or after the PR's `pushed_at` (freshness, ADR-0014), and polls every `AGENT_MERGE_POLL_INTERVAL` seconds (default `10`) up to `AGENT_MERGE_POLL_TIMEOUT` (default `300`) before escalating to recovery.

### Creating `JUDGE_GH_TOKEN`

`JUDGE_GH_TOKEN` is a GitHub token created under the account that should appear as the reviewer — that login becomes `AGENT_TRUSTED_JUDGE_USER`. Two options:

**Fine-grained PAT (recommended, least privilege):**

1. Sign in as the judge account → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
2. **Resource owner**: the judge account. **Repository access**: *Only select
   repositories* → the target repo **and** this orchestrator repo — the
   reusable workflow checks out `scripts/review.py` from the latter, and a PAT
   without read access there fails the checkout with HTTP 403.
3. **Permissions**: *Pull requests* → Read and write; *Contents* → Read-only.
4. Generate, copy the `github_pat_…` value, add it as target-repo secret `JUDGE_GH_TOKEN`.

**Classic PAT (simpler, broader):**

1. Same account → **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**.
2. Scope: `repo`.
3. Add the `ghp_…` value as secret `JUDGE_GH_TOKEN`.

To confirm the value for `AGENT_TRUSTED_JUDGE_USER`, run `gh api user --jq .login` while authenticated as the `JUDGE_GH_TOKEN` account — that login is what you set.

## Optional inputs

The caller may pass inputs to the reusable workflow:

| Input | Default | Purpose |
| --- | --- | --- |
| `orchestrator-repo` | `LuisArteaga/agentic-developer-core` | Source of `scripts/review.py`. Change only for a fork. |
| `orchestrator-ref` | `main` | Orchestrator git ref. Pin to a tag for lockstep. |
| `diff-exclude` | `""` | Space-separated pathspecs to exclude (e.g. `uv.lock`). |

> **Breaking change (issue #149):** the `coverage-artifact-name` and
> `coverage-artifact-path` inputs were removed when the ADR-0030
> CI-coverage-output transport was retired. The `test_coverage` judge now
> evaluates semantic test quality (Assertion Strength, Edge Cases,
> Implementation Leakage) from the diff alone; changed-line coverage is
> enforced by the deterministic Diff Coverage Gate ([ADR-0052](./adr/0052-deterministic-diff-coverage-gate.md)).
> Callers still passing the removed inputs get a GitHub Actions
> unexpected-input validation error — delete those inputs from your caller
> workflow when upgrading.

## Deterministic static checks

The judges are the *semantic* tier of the quality model (ADR-0020); the deterministic tier (ruff, mypy, coverage floor, Diff Coverage Gate, dependency and secret scans) is reusable too: see [ADR-0057](../docs/adr/0057-reusable-pr-checks-workflow-for-target-repos.md) and call `.github/workflows/pr-checks.yml` from a caller job, passing your repository's paths and thresholds as inputs. Running the deterministic gates in the same PR event as the judges preserves the cost ordering — judges only spend tokens once every mechanical gate is green.

## Branch protection & review event type

The judge posts the review as `approve` (all PASS) or `request-changes` (any
FAIL/NEEDS REVIEW); when the token owner equals the PR author, `gh` downgrades
to a `comment`. The Merge-Node parses only the hidden verdict block, so the
review **event type is irrelevant to parsing** — but if branch protection
requires the judge check to pass before merging, name the `judge` job
consistently so the required-status-check name is stable.

## Failure modes

- **Caller run shows `startup_failure` with zero jobs** → either the source
  repo's Actions **Access** grant is missing (Step 0), or the caller job lacks
  the `permissions:` block so the callee's `pull-requests: write` request is
  rejected as an escalation (Step 2). GitHub surfaces both with a generic
  "workflow file issue" hint; check the settings, not the YAML.
- **Orchestrator-repo checkout fails with `repository not found` or HTTP 403**
  → the `JUDGE_GH_TOKEN` PAT cannot read this private repository (`repository
  not found`: no usable credential sent; `403`: credential lacks scope — see
  Step 1 / "Creating `JUDGE_GH_TOKEN`").
- **Judge workflow fails or never runs** → no review is posted; the Merge-Node
  terminates via its existing poll-timeout path (`AGENT_MERGE_POLL_TIMEOUT`) and
  escalates to recovery. No infinite hang.
- **Multiple pushes to the same PR** → each push triggers a fresh review; the
  Merge-Node selects the newest qualifying review (`submitted_at >= pushed_at`).
- **Target repo without the caller workflow installed** → behaviour identical to
  today (no verdicts, poll timeout). An interim "no-judge mode" is tracked
  separately and is out of scope here.
