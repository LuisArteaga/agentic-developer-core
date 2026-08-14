# Onboarding a Target Repository to the PR Review Judges

The orchestrator's **Merge-Node** polls the *target* repository's pull requests
for the hidden `llm-pr-review-verdicts` block posted by the PR Review Judges
([ADR-0019](./adr/0019-combined-pr-review-body-with-hidden-verdict-block.md)).
By default the judges only run in the orchestrator repository's own CI, so
target-repo PRs never receive a verdict and the Merge-Node times out. This
guide wires the judges into a target repository via the **reusable workflow**
defined at `.github/workflows/llm-pr-review.yml` ([ADR-0050](./adr/0050-reusable-workflow-for-target-repo-pr-review-judges.md))).

## How it works

1. The target repository adds a small *caller* workflow that triggers on
   `pull_request` and calls the orchestrator's reusable workflow.
2. The reusable workflow checks out the target repo (for the PR diff) **and**
   the orchestrator repo (for the canonical `scripts/review.py`), then runs the
   four judges and posts a single combined review.
3. The review is authored by the trusted judge identity; the orchestrator's
   `AGENT_TRUSTED_JUDGE_USER` must match that author or the Merge-Node ignores
   it (spoofing guard, [ADR-0014](./adr/0014-pr-verification-and-llm-judge-review-integration.md)).

## Step 1 — Create the required secrets in the target repository

| Secret | Purpose |
| --- | --- |
| `JUDGE_GH_TOKEN` | A GitHub PAT (or App token) with `pull-requests: write` on the target repo. **Its owner must equal the orchestrator's `AGENT_TRUSTED_JUDGE_USER`.** |
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
    secrets:
      judge-token: ${{ secrets.JUDGE_GH_TOKEN }}
      openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

Pin `@main` to a release tag (e.g. `@v1`) once one is cut, so the verdict-block
writer stays in lockstep with the orchestrator's parser ([ADR-0019](./adr/0019-combined-pr-review-body-with-hidden-verdict-block.md)).

## Step 3 — Configure the orchestrator

Set the orchestrator environment so the Merge-Node trusts the review author:

```
AGENT_TRUSTED_JUDGE_USER=<login that owns JUDGE_GH_TOKEN>
```

The Merge-Node ignores reviews from any other author, and only considers reviews
whose `submitted_at` is on or after the PR's `pushed_at` ([ADR-0014](./adr/0014-pr-verification-and-llm-judge-review-integration.md)).

## Optional inputs

The caller may pass inputs to the reusable workflow:

| Input | Default | Purpose |
| --- | --- | --- |
| `orchestrator-repo` | `LuisArteaga/agentic-developer-core` | Source of `scripts/review.py`. Change only for a fork. |
| `orchestrator-ref` | `main` | Orchestrator git ref. Pin to a tag for lockstep. |
| `diff-exclude` | `""` | Space-separated pathspecs to exclude (e.g. `uv.lock`). |
| `coverage-artifact-name` | `""` | Name of a coverage-report artifact ([ADR-0030](./adr/0030-pipe-ci-coverage-output-into-test-coverage-pr-judge.md)). Empty = diff-only. |
| `coverage-artifact-path` | `ci_coverage_output.txt` | Path to the report inside that artifact. |

### Piping CI coverage (ADR-0030)

When the target repository runs tests in a preceding job, upload the coverage
report as an artifact and reference it so the `test_coverage` judge consumes
real coverage instead of diff-only heuristics:

```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # ... run tests, e.g.:
      - run: pytest --cov-report=term-missing | tee ci_coverage_output.txt
      - uses: actions/upload-artifact@v4
        with:
          name: ci-coverage
          path: ci_coverage_output.txt

  judge:
    needs: test
    uses: LuisArteaga/agentic-developer-core/.github/workflows/llm-pr-review.yml@main
    with:
      coverage-artifact-name: ci-coverage
      coverage-artifact-path: ci_coverage_output.txt
    secrets:
      judge-token: ${{ secrets.JUDGE_GH_TOKEN }}
      openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
```

If the artifact is absent or the file is missing, the judge degrades gracefully
to diff-only evaluation — it never fails the hard gate on missing coverage.

## Branch protection & review event type

The judge posts the review as `approve` (all PASS) or `request-changes` (any
FAIL/NEEDS REVIEW); when the token owner equals the PR author, `gh` downgrades
to a `comment`. The Merge-Node parses only the hidden verdict block, so the
review **event type is irrelevant to parsing** — but if branch protection
requires the judge check to pass before merging, name the `judge` job
consistently so the required-status-check name is stable.

## Failure modes

- **Judge workflow fails or never runs** → no review is posted; the Merge-Node
  terminates via its existing poll-timeout path (`AGENT_MERGE_POLL_TIMEOUT`) and
  escalates to recovery. No infinite hang.
- **Multiple pushes to the same PR** → each push triggers a fresh review; the
  Merge-Node selects the newest qualifying review (`submitted_at >= pushed_at`).
- **Target repo without the caller workflow installed** → behaviour identical to
  today (no verdicts, poll timeout). An interim "no-judge mode" is tracked
  separately and is out of scope here.
