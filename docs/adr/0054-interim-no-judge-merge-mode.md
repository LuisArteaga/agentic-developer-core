# ADR 0054: Interim No-Judge Merge Mode with Resumable Pause

* **Status**: Accepted (interim — to be superseded by the judge-integration outcome of #132)
* **Date**: 2026-08-24
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

ADR-0014 made PR merge conditional on parsed PR Review Judge verdicts, and ADR-0050 shipped the Reusable Judge Workflow so target repositories can run those judges. Until a target repository actually installs that workflow (#132), every orchestrator run reaching the Merge phase polls for verdict blocks that will never arrive. After `AGENT_MERGE_POLL_TIMEOUT` (default 300 s) it transitions to `failed`/recovery with "Polling timed out" — resetting the issue label and discarding the workspace branch for a condition that is not a failure at all but an undeployed judge. Observed in production on 2026-08-14: issue #13 of the `ot-telemetry-engine` target repo sat through verdict polling on PR #17 until the timeout, and the run had to be aborted manually.

The industry norm reinforces that this is a misclassification: across credible vendors, **AI reviews; humans or policy merge** (GitHub's Copilot reviewer posts Comment-only reviews by design; Claude Code Review completes neutral check runs precisely so it never blocks merging). A deployment without automated judges is simply the L0/L1 end of that ladder — a human gate — which mainstream CI treats as a *waiting* state, never as a failure.

The decision is interim by construction (#132 removes the need), but the semantics chosen here shape operator workflows today and the state machine must remain coherent under resume — hence an ADR rather than a README note.

## Decision Drivers

* **Operational honesty**: "no judge deployed" is a legitimate posture, not an error; the loop must not emit failure telemetry or reset labels for it.
* **Default preservation**: ADR-0014 blocking semantics are the contract; any new mode must be strictly opt-in and byte-identical when off.
* **Latency scales differ**: judge verdicts arrive in minutes (machine scale); human reviews take hours to days (human scale). A single timeout knob cannot serve both without ambiguity.
* **Stateful Resume** must stay intact: whatever the mode does at window expiry has to be reachable again from persisted state.
* **Minimal dependencies** (ADR-0017-era convention): env-var knobs only, no new libraries.

## Considered Options

* **Option 1: Status quo / shrink `AGENT_MERGE_POLL_TIMEOUT` (rejected)** — keep polling verdicts with a longer timeout.
  * *Cons*: still fetches and parses reviews pointlessly (judge-keys log spam); expiry is still a misleading `failed`; one knob now encodes two incompatible latencies.

* **Option 2: Overload `AGENT_MERGE_POLL_TIMEOUT` with sentinel values (rejected)** — e.g. `0` = wait indefinitely when judges are disabled.
  * *Cons*: ambiguous unset-vs-zero semantics; the precedence question ("which value wins?") becomes unanswerable cleanly because the same knob means different things per mode; existing behavior relies on zero meaning "skip the loop" in judge mode (used deliberately in test fixtures), so redefining it per-mode invites subtle regressions.

* **Option 3: Dedicated flag + dedicated window + resumable pause (chosen)** — see Decision.

## Decision

We chose **Option 3**: an explicit opt-in mode with its own knob vocabulary.

### Contract

1. **Flag**: `AGENT_JUDGE_ENABLED`. Unset or truthy (`1/true/yes/on`, case-insensitive) → current ADR-0014 semantics exactly. Falsy (`0/false/no/off`) → No-Judge Merge Mode. A malformed value degrades to the safe default (judges enabled) with a warning — the same warn-and-degrade posture as the factory-config loader; a typo must never silently disable merge blocking.
2. **No-judge poll body**: the trusted-user lookup, `/reviews` fetch, and verdict parsing are skipped entirely (the parsing logic itself is untouched, merely gated). The loop checks only `merged: true` → `status="done"` and closed-without-merge → failure, preserving the merge-detection contract verbatim.
3. **Timeout precedence (issue edge case)**: while the flag is falsy, `AGENT_NO_JUDGE_MERGE_TIMEOUT` fully replaces `AGENT_MERGE_POLL_TIMEOUT` — the legacy knob is not consulted at all. Default `86400` (24 h); a non-positive value disables the window (wait indefinitely; documented operational expectation: a human merges or aborts the run).
4. **Window expiry = resumable pause, not failure**: status and phase remain `"merging"` with no feedback/error. The graph run ends via the existing `route_after_merge` fallthrough; Recovery is deliberately bypassed, so the issue keeps `agent-in-progress` and the workspace keeps its branch. Stateful Resume routes the next invocation back into `merge_node`.
5. **Mid-run toggle determinism**: mode and window are resolved once per Merge-Node entry, before any API call; the persisted state carries no mode marker. Whatever the environment says at (re-)entry wins — flipping the flag between invocations (e.g., judges ship mid-cycle) is well-defined and deterministic.
6. Judge mode is untouched: identical log lines, freshness anchoring, merge-fix loop, and timeout→recovery path.

## Consequences

* Target repositories without #132 get honest, quiet runs that end in `done` after a human merges.
* An indefinite window makes the orchestrator process long-lived by design; operators who prefer bounded runs keep the default 24 h window, after which the run pauses (not fails) and resumes on the next invocation — e.g., the process supervisor's next scheduled iteration.
* The mode is scaffolding: when #132 ships, `AGENT_JUDGE_ENABLED`, `AGENT_NO_JUDGE_MERGE_TIMEOUT`, this ADR, and the gated branches are deleted together; CONTEXT.md marks the term interim.
* No state-schema change: the pause rides existing statuses, so older persisted states resume correctly.

## Inspiration & References

* **GitHub Actions — deployment approval timeouts** ([community discussion #5673](https://github.com/orgs/community/discussions/5673)): environment approvals hold a workflow run in a *waiting-for-approval* state — a resumable posture, not a failure — with a fixed 30-day auto-cancel; Azure DevOps by contrast exposes configurable approval timeouts. Directly informed decision 3 (dedicated, configurable human-scale window) and decision 4 (pause ≠ fail).
* **GitLab CI — [deployment approvals](https://docs.gitlab.com/ci/environments/deployment_approvals/) & manual jobs**: a blocking manual job keeps the pipeline blocked indefinitely ("the `timeout` keyword does not expire an unstarted manual job"), validating the indefinite-wait option; GitLab's own approval examples use `timeout: 24h`, the source of our default window.
* **"AI Pull Request Auto-Merge: Enterprise Guide"** ([firstaimovers.com](https://radar.firstaimovers.com/ai-pull-request-auto-merge-enterprise-guide-2026)) and the Copilot/Claude Code review postures cited therein: codifies the L0–L4 autonomy ladder where un-judged, human-merged operation is a supported level, not a defect — the framing that makes an explicit interim mode legitimate rather than a workaround.
* **Mergify — ["Merge Queues Were Built for Humans. AI Agents Need More."](https://mergify.com/blog/merge-queues-and-ai-coding-agents)**: documents the operational reality that agent-authored PRs bottleneck on human review latency — the exact latency scale `AGENT_NO_JUDGE_MERGE_TIMEOUT` is sized for.
