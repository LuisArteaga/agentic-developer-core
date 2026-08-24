# ADR 0053: Workspace Snapshot in the Worker Prompt with Paired Recursion-Budget Raise

* **Status**: Accepted
* **Date**: 2026-08-24
* **Deciders**: Luis Arteaga & The Architect
* **Issue**: #124

## Context and Problem Statement

Worker Trace analysis of the issue #13 run (2026-08-14) showed a systematic budget-efficiency failure: **all four worker runs** (test_writer ×1, execute ×3) exhausted the Recursion Budget (50 supersteps ≈ 25 think→tool rounds) mid-work, with 40–60% of tool calls spent on pure orientation — 8–10 `list_directory` calls plus reads of every empty `__init__.py` — before the first edit. In `worker_trace_13_1.jsonl` the first `patch_file` was tool call 29 of 36.

Every retry pays this tax again: each attempt starts a fresh Worker with no memory of the previous attempt's exploration (the Worker Trace sidecar is post-hoc-only by design). The Plan-Node already computes exactly the artifacts that would eliminate this tax — the directory tree and Structural Outlines (ADR-0017) of the plan-localized target files — but they die with the plan prompt instead of reaching the Worker.

## Decision Drivers

* **The exploration tax is deterministic waste**: it is re-paid on every attempt (Hybrid Retry can mean 3+ full payments per issue cycle) and is the dominant cause of budget exhaustion on scaffolding issues.
* **ADR-0045's documented anti-pattern**: raising `recursion_limit` alone "just delays the crash if there is a genuine cycle". Any budget raise must be paired with an efficiency fix addressing the actual consumption.
* **Outlines are hints, not ground truth** (CONTEXT.md): feeding them to the Worker cannot weaken the Read-Before-Edit Constraint (ADR-0006/ADR-0033) as long as the snapshot registers no `read_files` membership.
* **Token cost is bounded and one-sided**: the snapshot costs at most its cap once per attempt; the exploration it replaces costs multiple tool round-trips (each a full LLM call plus tool result) per attempt.
* **Language Agnosticism tiering**: Verification stays untouched; the execution-side snapshot follows the Language-Aware Best-Effort Planning precedent (tree for unsupported languages).

## Considered Options

* **Option 1: Status quo (rejected)** — keep the generic "Start by exploring the codebase" instruction.
  * *Pros*: no code change.
  * *Cons*: every attempt re-pays the tax; scaffolding issues exhaust the budget on orientation instead of work; retries compound the waste.

* **Option 2: Raise `recursion_limit` only (rejected)** — the direct lever against `GraphRecursionError`.
  * *Pros*: one-line config change via the existing ADR-0045 resolution mechanism.
  * *Cons*: explicitly documented anti-pattern in ADR-0045; raises the token ceiling of failed runs without removing the cause; Tool Loop Detection (ADR-0046) would remain the only efficiency guard.

* **Option 3: Whole-workspace outline snapshot like the Plan-Node's (rejected)** — reuse `build_outlines` verbatim for the Worker.
  * *Pros*: maximal context; zero new selection logic.
  * *Cons*: unbounded on large repositories; spends budget on files the plan never localized — defeating the point of Localization; the Plan-Node already caps whole-repo outlines at `OUTLINE_CHAR_CAP` precisely because full coverage does not fit.

* **Option 4: Targeted Workspace Snapshot + paired budget raise (chosen)** — inject a budget-bounded snapshot (directory tree + Structural Outlines of the plan's `target_files`) into the Execute/Test-Writer user message, replacing the explore-first instruction; raise the factory `recursion_limit` for `execute`/`test_writer` from the default 50 to 100 supersteps through the unchanged ADR-0045 resolver.

* **Cross-attempt trajectory memory (rejected for now)** — persisting the prior attempt's explored-file map into the retry prompt.
  * *Cons*: contradicts the Worker Trace's post-hoc-only contract; risks stale guidance after Hybrid Retry's hard reset (ADR-0034); the Verification Feedback channel remains the single retry-injection path by design.

## Decision

We chose **Option 4**, as one inseparable decision: the budget raise is *justified only by* the exploration-tax fix, and the snapshot's benefit is *fully realized only with* headroom for the remaining legitimate work.

### Snapshot semantics

* Composed deterministically: header → directory tree (sub-capped at 6,000 chars, line-dropped truncation note) → per-file Structural Outlines of the plan's deduplicated `target_files` in first-appearance plan order (per file capped at `OUTLINE_PER_FILE_CAP`). Total bounded by `AGENT_SNAPSHOT_MAX_CHARS` (default 20,000 chars ≈ ~5k tokens); entries that do not fit are skipped and named in an explicit truncation note — later smaller files still fit.
* A hint frame, not authorization: no `read_files` registration, Read-Before-Edit untouched, tools reflect reality when the snapshot goes stale mid-attempt.
* Graceful degradation everywhere: malformed/legacy prose plans yield a tree-only snapshot; unsafe/missing/non-source targets are skipped defensively (shared validation with the Plan Detail Request path); a builder failure falls back to the original explore-first instruction rather than breaking the worker.
* Both Execute and Test-Writer receive it — the Test-Writer pays the same tax (its trace showed the identical pattern).
* Verification Feedback injection is untouched: the snapshot is attempt-start context, not retry feedback.

### Budget value: 100 supersteps

≈50 think→tool rounds — roughly 2× the longest observed trajectory (36 tool calls) and comfortably above the post-snapshot expected consumption (~25 calls for scaffolding work incl. verification runs). It remains far below LangGraph's own framework default (≥ v1.0.6 ships 1000), so this is still a conservative crash guard, not a license to wander. Tool Loop Detection thresholds (warn 3 / hard 5 / window 10) are deliberately unchanged: it stays the cheap first-line guard against genuine cycles, per the layered defense of ADR-0045/ADR-0046.

## Edge cases

Each covered by unit tests (`orchestrator/test_snapshot.py`, `orchestrator/test_worker.py`): empty/skeleton workspace → degenerates to the directory tree ("(empty directory)"), so the Worker still orients without tool calls; malformed or legacy non-JSON plans → tree-only snapshot; unsupported-extension and nonexistent targets → skipped, section omitted entirely when nothing resolves; duplicate targets across tasks → single outline entry; tiny budget → deterministic truncation notes, length ≤ cap + suffix; builder exception → worker message falls back to explore-first instruction; env override absent → explicit factory values (100) resolved, and the DEFAULT_RECURSION_LIMIT (50) fallback still verified against an empty factory config.

## Consequences

* **Pros**: baseline orientation costs zero tool calls on every attempt (not just the first); scaffolding-issue trajectories should now fit the budget with room for verification runs; Trajectory Length (TL) should drop measurably — validated via the `.agent_logs/metrics.jsonl` trend per Run Observability (directional evidence, not a one-off benchmark); Plan Alignment becomes a cleaner signal once exploration noise is removed (expected PA trend shift, no code change).
* **Negatives**: up to ~5k tokens added per attempt even when the Worker would have explored cheaply (bounded by the cap, env-tunable); the snapshot can go stale within an attempt (documented semantics — tools are ground truth); one more consumer of the serialized DevelopmentPlan shape (parse is defensive and degrades silently); the recursion-limit raise increases the worst-case token spend of a genuinely runaway-but-non-repeating loop before the crash guard fires (mitigated by loop detection).

## Inspiration & References

* **Aider: Building a better repository map with tree sitter** — [aider.chat/2023/10/22/repomap.html](https://aider.chat/2023/10/22/repomap.html) and [aider.chat/docs/repomap.html](https://aider.chat/docs/repomap.html) — Source Quality Tier T2 (official project docs/blog), accessed 2026-08-24. Verified live via web search: confirms the canonical pattern this decision adapts — a tree-sitter-derived signature map under an explicit token budget where "GPT can see classes, methods and function signatures from everywhere in the repo … If it needs to see more code, GPT can use the map to figure out by itself which files it needs to look at in more detail." This validates both the hint-not-ground-truth semantics and keeping exploration tools available alongside the map. We adopt a *targeted* subset of the idea (plan-localized files rather than graph-ranked whole-repo relevance), since our Worker already holds a Localization.
* **LangGraph Graph API documentation (recursion_limit)** — [docs.langchain.com/oss/python/langgraph/graph-api](https://docs.langchain.com/oss/python/langgraph/graph-api) — Tier T1 (official docs), accessed 2026-08-24. Verified live: `recursion_limit` bounds graph super-steps and raises `GraphRecursionError`; passed per-invocation via the config dict; the framework default since v1.0.6 is 1000. Confirms our superstep accounting (2 per ReAct round) and that a node-level value of 100 remains conservative relative to the framework's own default.
* **SWE-agent: Agent-Computer Interfaces enable automated software engineering (NeurIPS 2024)** — [arxiv.org/abs/2405.15793](https://arxiv.org/abs/2405.15793) — Tier T1 (peer-reviewed publication), accessed 2026-08-24. Verified via search-indexed abstract/coverage: the design of the agent's observation/action interface strongly affects resolution rates. Supports the core diagnosis that burning observations on redundant orientation is an interface defect worth fixing at the source rather than enlarging the step cap.
* **LangChain Forum: What does recursionLimit actually count?** — [forum.langchain.com/t/what-does-recursionlimit-actually-count-in-createagent-langchain-js/3460](https://forum.langchain.com/t/what-does-recursionlimit-actually-count-in-createagent-langchain-js/3460) — Tier T3 (official project forum, maintainer responses), accessed 2026-08-24. Verified live: guidance to treat `recursion_limit` as a safety net well above the business budget, with tight business budgets enforced separately (here: Tool Loop Detection middleware, thresholds unchanged). Tier T3 is acceptable per ADR-0041 for this non-security-critical supporting point.
* **ADR-0045 / ADR-0046** — internal precedents: the per-node Recursion Budget resolution consumed unchanged here, and the layered loop-defense model that makes the paired raise safe.
