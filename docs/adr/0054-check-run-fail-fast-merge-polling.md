# ADR 0054: Check-Run Fail-Fast in Merge Polling with Dual-Triage Feedback Routing

* **Status**: Accepted
* **Date**: 2026-08-25
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The Merge-Node polls a PR for exactly one actionable signal: the hidden
`llm-pr-review-verdicts` block posted by the PR Review Judges
([ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md)). On
2026-08-14, target-repository PR #19 failed at its CI lint step; no judge
review was ever posted (the judges never ran), and the Merge-Node burned the
full `AGENT_MERGE_POLL_TIMEOUT` (300 s) with all verdicts `None`, then
transitioned to `failed` with "Polling timed out" — no actionable feedback,
no merge-fix retry, manual intervention required. The parser had no second
signal source: GitHub **check runs** appeared nowhere in the orchestrator
codebase.

The industry norm is fail-fast on red checks: GitHub's merge queue removes a
PR as soon as a required check fails rather than waiting out a window, and
branch protection treats a failed required check as an immediate merge
blocker. Waiting for judge verdicts that are structurally impossible (the
judge workflow gates on the same green checks) is pure latency waste — and,
worse, it *discards* the one piece of feedback (`output.title` /
`output.summary`) that tells the Worker exactly what to fix.

This decision is hard to reverse because the feedback-channel contract and
escalation semantics become load-bearing for the Worker's retry loop; it is
surprising without context because the Merge-Node now reads a second GitHub
signal alongside the ADR-0019 verdict block; and it is the result of a real
trade-off between exact-name matching, substring matching, and structured
check-run metadata for the escalation-vs-fixable discrimination.

## Decision Drivers

* **ADR-0019 lockstep**: the hidden verdict block remains the primary signal;
  check runs are an *additional* signal, never a replacement.
* **ADR-0036 machinery reuse**: fixable failures must ride the existing
  Merge-Fix Loop (`status="executing"`, `phase="merge_fix"`,
  `attempts["merge"]` bounded by `AGENT_PR_FIX_MAX`, feedback via the existing
  Verification Feedback channel, per-gate counters reset per ADR-0047).
* **ADR-0050 infrastructure asymmetry**: the Reusable Judge Workflow itself
  runs as a check. A failed judge-workflow check is infrastructure the Worker
  cannot fix — consuming merge-fix budget on it guarantees exhaustion without
  convergence.
* **Language Agnosticism**: feedback derives from GitHub check output only;
  no language-specific parsing of logs or exit codes.
* **Resilience contract**: pagination and transient API errors must never
  break the poll — observability failures degrade to "no signal", mirroring
  the no-op-test-phase fail-open posture.

## Considered Options

* **Option 1: Status quo** — keep polling verdicts only. Rejected: the
  evidence case shows structural timeout with zero actionable output.
* **Option 2: Exact check-name matching against the judge workflow's job
  name** (e.g. `== "llm-pr-review"`). Rejected: when a target repository
  calls the reusable workflow from its own caller job, GitHub registers the
  check run as `caller-job / called-job` (GitHub community discussion
  #72708). Exact matching misses every real-world invocation.
* **Option 3: Substring discrimination + dual-triage routing (chosen)** — see
  Decision.
* **Option 4: Treat any failed check as merge-fixable** (no escalation tier).
  Rejected: violates ADR-0050; a broken judge workflow cannot converge under
  automated retries, so budget must not be consumed.
* **Option 5: Fetch check annotations per failed run**
  (`/check-runs/{id}/annotations`). Rejected for v1: doubles the API surface;
  `output.title` + `output.summary` carried the signal in the evidence case.
  Annotations remain a bounded future enhancement.

## Decision

Each Merge-Poll iteration additionally fetches
`GET /repos/{owner}/{repo}/commits/{head_sha}/check-runs?per_page=100&page=N`
for the **current head SHA** (freshness by SHA keying — a merge-fix push
mid-poll automatically re-anchors evaluation to the new head, consistent with
the `pushed_at` anchor for verdicts). Conclusions `failure`, `cancelled`,
`timed_out`, and `action_required` count as concluded failures;
`neutral`, `skipped`, `success`, pending statuses, and the GitHub-managed
`stale` / `startup_failure` do not (the latter two deliberately fall through
to existing polling behavior).

Failed checks are triaged into two tiers:

1. **Judge-infrastructure failure** — the check name contains (case-
   insensitive) any fragment of `AGENT_JUDGE_CHECK_NAMES` (comma-separated,
   default `llm-pr-review`). Substring matching is deliberate: reusable-
   workflow checks are named `caller-job / called-job`. The loop escalates
   immediately: posts `_judge_infra_failure_comment` on the PR explaining the
   failure is unfixable automation infrastructure and a human should re-run
   the judge workflow, then transitions to Failure Recovery **without
   touching `attempts["merge"]`**. Escalation outranks any simultaneously
   failing fixable check, because fix cycles cannot converge while the hard
   gate is broken.
2. **Fixable product-CI failure** — any other concluded failure. The loop
   breaks out early (no timeout burn) and enters the existing Merge-Fix Loop:
   budget check → `attempts["merge"] += 1`, `verify_cmd`/`bineval` reset to 0
   (ADR-0047), structured feedback via `_check_run_feedback` into
   `state["feedback"]` (name, conclusion, title, summary truncated at 500
   chars, first five checks listed, remainder counted), `status="executing"`
   + `phase="merge_fix"`. At budget exhaustion a dedicated
   `_checks_exhaustion_comment` summarizes unresolved checks before recovery.

**Mixed signals** resolve to the newest actionable signal: check-run
`completed_at` versus qualifying-review `submitted_at`; a missing or
unparseable timestamp defers to the verdict (primary ADR-0019 signal). Both
signals feed the same feedback channel.

**Resilience**: pagination follows pages while full (bounded at 10 pages ≈
1000 runs); non-dict payloads, missing `check_runs` keys, non-dict entries,
and raised API errors each log a warning and degrade to "no signal" — the
poll continues unchanged. Absent check runs (repo without CI) reproduce
pre-ADR-0054 behavior exactly. No-Judge mode ([ADR-0053](./0053-interim-no-judge-merge-mode.md))
keeps skipping reviews/verdicts entirely but *does* gain check-run fail-fast:
a red CI build during the human-review window is exactly as fixable as in
judge mode, and waiting 24 h for a human to notice is strictly worse than a
bounded autonomous fix cycle.

### Consequences

* **Pro**: the evidence-class failure (CI red, judges unreachable) converts
  from a silent 300 s timeout + dead-end recovery into an immediate bounded
  fix cycle carrying the failing check's own diagnostics.
* **Pro**: judge-workflow outages stop burning merge-fix budget; humans get
  an actionable comment instead of a generic timeout.
* **Con**: the parser now consumes two GitHub signals; the writer↔parser
  lockstep of ADR-0019 extends to a *triage* contract — if target repos name
  their judge jobs something not covered by `AGENT_JUDGE_CHECK_NAMES`, a real
  infra failure would be misrouted into merge-fix (bounded to
  `AGENT_PR_FIX_MAX` attempts, then escalated anyway).
* **Con**: two extra API calls per poll iteration (pulls already existed);
  mitigated by the poll interval (10 s default) and pagination cap.

## Inspiration & References

* **GitHub REST API — List check runs for a Git reference**
  ([docs](https://docs.github.com/rest/checks/runs#list-check-runs-for-a-git-reference))
  — Source Quality Tier **T1** (official docs). Access date 2026-08-25.
  Verification: confirms the endpoint path, `status` values (`queued`,
  `in_progress`, `completed`) vs `conclusion` values (`success`, `failure`,
  `neutral`, `cancelled`, `skipped`, `timed_out`, `action_required`,
  `stale`, `startup_failure`), and `per_page`/`page` pagination (max 100).
* **GitHub Docs — Using the REST API to interact with checks**
  ([docs](https://docs.github.com/rest/guides/using-the-rest-api-to-interact-with-checks))
  — Source Quality Tier **T1** (official docs). Access date 2026-08-25.
  Verification: documents that only GitHub sets `stale` (incomplete >14 days)
  and that check suites aggregate the highest-priority conclusion — grounding
  the decision to exclude `stale`/`startup_failure` from the failure set.
* **GitHub community discussion #72708 — Required status checks, reusable
  workflows and skipped jobs**
  ([discussion](https://github.com/orgs/community/discussions/72708))
  — Source Quality Tier **T3** (community, corroborated by multiple users'
  screenshots). Access date 2026-08-25. Verification: demonstrates checks
  registered as `caller-job / called-job` when a reusable workflow runs —
  the direct evidence for substring over exact-name matching.
* **GitHub Docs — Managing a merge queue**
  ([docs](https://docs.github.com/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue))
  — Source Quality Tier **T1** (official docs). Access date 2026-08-25.
  Verification: the merge queue waits for required checks and removes PRs
  that fail them — the industry fail-fast posture this ADR imports.
* **Trunk.io — What is GitHub Merge Queue?**
  ([article](https://trunk.io/learn/what-is-github-merge-queue-a-quick-overview))
  — Source Quality Tier **T2** (vendor engineering publication). Access date
  2026-08-25. Verification: restates the fail-fast semantics ("If the CI
  checks fail, the pull request is automatically removed"), confirming the
  norm across implementations rather than a GitHub quirk.
* **ADR-0036** — the Merge-Fix Loop machinery (budget, feedback channel,
  escalation comment) reused verbatim for the fixable tier.
* **ADR-0050** — the infrastructure-asymmetry principle behind the
  escalation tier.
* Issue #140 — the originating proposal, including the 2026-08-14 evidence.
