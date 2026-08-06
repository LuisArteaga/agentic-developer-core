# ADR 0036: Closed Post-PR Judge-Feedback Loop (bounded merge→execute retry)

## Status
Accepted (closes the asymmetry noted by [ADR-0014](./0014-pr-verification-and-llm-judge-review-integration.md) / [ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md))

## Context
The loop already has a *bounded pre-PR retry*: when `verify_node` (or the BinEval soft gate, [ADR-0027](./0027-pre-pr-bineval-soft-semantic-gate.md)) fails, the graph routes `verify → execute` up to 3× ([ADR-0013](./0013-incremental-refinement-workspace-state-on-retry.md) / [ADR-0034](./0034-hybrid-retry-hard-rollback-on-final-attempt.md)), injecting truncated `Verification Feedback` so the Worker self-corrects. Once the PR opens, the post-PR hard-gate judges run in CI and post the hidden verdict block ([ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md)); `merge_node` polls it and parses the four verdicts. But on any `FAIL`/`NEEDS REVIEW` ([ADR-0014](./0014-pr-verification-and-llm-judge-review-integration.md)) the loop only *detected* the actionable feedback — it set `status="failed"` and `route_after_merge` had no `merge → execute` edge, so it abandoned the branch and silently re-queued the issue from scratch in `recovery_node`. The hard-gate investment ([ADR-0021](./0021-layered-retry-and-model-fallback-for-empty-llm-judge-responses.md)/[0022](./0022-enclosing-function-context-enrichment-for-pr-review-judges.md)/[0023](./0023-per-file-map-reduce-diff-evaluation-for-pr-review-judges.md)/[0030](./0030-pipe-ci-coverage-output-into-test-coverage-pr-judge.md)) produced verdicts the loop then threw away on the first failure.

## Decision
Close the loop as a **bounded, deterministic graph edge** — reusing existing machinery, not introducing a new free agent. This preserves the project's "controlled if/else, tightly-scoped LLM" philosophy.

1. **New `merge → execute` edge.** `route_after_merge` returns `"execute"` when `merge_node` parsed actionable verdicts and the merge-fix budget is not exhausted; `add_conditional_edges("merge", …)` gains the `"execute"` destination.
2. **Bounded counter.** Reuse the existing `state["attempts"]` dict with a new `attempts["merge"]` key (no new top-level state field, no `state.py` schema change), capped by `AGENT_PR_FIX_MAX` (default `3`). The cap uses a **check-before-increment** rule: the edge fires while the *pre-increment* counter is below the cap, yielding exactly `AGENT_PR_FIX_MAX` fix cycles before escalation. `attempts["merge"]` is reset to 0 on a clean merge and on a fresh claim (the claim reset also closes a latent `attempts["verify"]` leak across cycles).
3. **Findings reuse the existing feedback channel.** `merge_node` extracts the actionable `[SEVERITY]`-tagged finding lines (porting the `pr-feedback-loop` skill's `extract_findings` parser into `nodes.py` as `_extract_review_findings`, keeping the headless loop self-contained — it does not import skill scripts) and writes a structured message to `state["feedback"]`, consumed by `execute_node`'s existing injection path (`nodes.py:752–763`). No new feedback plumbing.
4. **`fix:` commit on re-entry.** `pr_node` is already idempotent for PR creation; on re-entry it commits with `fix: address PR review feedback (attempt N)` instead of `feat: resolve issue #{n}`, gated on `attempts["merge"] > 0` (the counter survives `execute → verify → pr`; `phase` does not, since each node overwrites it). The push triggers a fresh CI run + fresh judge review; the loop re-enters `merge_node`, anchored on the updated `pushed_at` so stale pre-fix reviews are ignored.
5. **Hybrid Retry interaction.** At merge-fix initiation, `attempts["verify"]` is reset to 0, granting a fresh per-cycle 3-attempt verify budget so the `AGENT_RETRY_HARD_RESET_ATTEMPT` threshold ([ADR-0034](./0034-hybrid-retry-hard-rollback-on-final-attempt.md)) counts Execute attempts within a single verify cycle, not across merge-fix cycles.
6. **Escalation, not silent re-queue.** At budget exhaustion, `merge_node` posts a summary PR comment (unresolved judges + findings) via the issues/comments endpoint *before* transitioning to `failed`/recovery — the human-readable handover the `pr-feedback-loop` skill defines for the human-steered path, without adding a human-in-the-loop gate.
7. **Routing signal.** `merge_node` signals a retry via the existing validated `status="executing"` + `phase="merge_fix"` (no new `StatusType` enum / `state.py` whitelist change); `route_after_claim` already maps `status="executing"` → `execute` on resume, so a crash during a merge-fix cycle re-enters the correct phase with `attempts["merge"]` preserved ([ADR-0016](./0016-in-process-telemetry-and-stateful-resume-tracing.md)).

## Considered Options
- **Free ReAct agent driving the post-PR path** — rejected: violates the controlled-if/else philosophy; the bounded edge reuses the existing Worker and BinEval machinery.
- **New `StatusType` value `"merge_fix"`** — rejected: overloads the enum for a signal already expressible as `executing` + `phase`, requiring a `state.py` whitelist change for no functional gain.
- **Import the `pr-feedback-loop` skill script** — rejected: skills are human-facing tools; the headless loop must stay self-contained. Porting the ~5-line parser is cheaper than the import coupling.
- **Cap = 15 (the skill's cap)** — rejected: 15 is correct for a human-steered CLI where iterations are cheap and a human can intervene. A headless loop pays a full CI round-trip per iteration; 3 matches the existing circuit-breaker posture of ADR-0013/0034 and the ODSC finding that raising the iteration count does not solve a retry loop.
- **Reset `attempts["verify"]` inside `execute_node`** — rejected: the reset is a merge-fix-cycle concern and belongs with the counter that initiates the cycle; keeping it in `merge_node` leaves `execute_node` focused on execution.

## Consequences
- **Pro**: the hard-gate verdicts are acted on, not discarded; the common single-`FAIL`-then-fix case is handled autonomously.
- **Pro**: bounded by `AGENT_PR_FIX_MAX` (default 3); each iteration is a full CI round-trip, so the cost is bounded and explicit.
- **Pro**: a crash during a merge-fix cycle resumes correctly (`attempts["merge"]` is in `state.json`; status/phase route back to `execute`).
- **Con**: up to `AGENT_PR_FIX_MAX` × (Execute + Verify + CI) per failed PR — an explicit cost trade against autonomy, capped to escape cleanly (circuit-breaker posture).
- **Con**: existing `merge_node` "blocked" tests changed from asserting `status="failed"` to asserting `status="executing"` (merge-fix retry) — a deliberate behavior change, not a regression.

## Inspiration & References
- **CodeRabbit Autofix (Apr 2026)** — scans unresolved review comments, applies fixes to the branch, verifies them: a closed review→fix→re-review loop. The bounded edge + `fix:` commit + re-poll mirrors this. https://www.coderabbit.ai/blog/coderabbit-skills-code-review
- **Bugbot Autofix (Feb 2026)** — spawns agents to fix identified problems; 35%+ of autofix changes merge into the base PR. Industry trend validates the closed-loop autofix posture. https://gitautoreview.com/blog/ai-pr-review-guide
- **Claude Code (March 2026)** — "Code Review, Auto Mode, and Auto-Fix form a closed loop" from code → review → fix → re-review. https://alirezarezvani.medium.com/claude-code-just-made-pull-requests-fully-autonomous-here-is-what-three-march-announcements-add-736434f5f8ee
- **Aider SWE-bench harness** — re-invokes aider until "plausibly correct", bounded by attempts: the bounded-retry posture ADR-0034 already mirrors. https://github.com/Aider-AI/aider-swe-bench
- **Circuit-breaker + retry best practice** — retry for transient faults; trip open after N failures to stop hammering. Validates cap=3 with a clean exit (post comment + recovery) over a higher cap. https://scalewithchintan.com/blog/circuit-breaker-bulkhead-retry-patterns-demystified
- **ODSC — "The 3 loops that break AI agents in production"** (already cited in ADR-0034) — raising the iteration count does not solve a retry loop; a clean, low exit does. Motivates cap=3 over cap=15.
- Issue #92 — the originating proposal.
