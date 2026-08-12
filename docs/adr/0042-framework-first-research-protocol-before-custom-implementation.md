# ADR 0042: Framework-First Research Protocol Before Custom Implementation

## Status

Accepted

## Context

A retrospective on a prior "Skip First Turn on load" implementation exposed a systemic failure mode distinct from — but sharing the root cause of — the hallucinated-research incident that motivated ADR-0041:

1. The human pre-specified a solution ("I want to resume sessions") from prior tool experience.
2. The AI (a weaker model) built the first available implementation ("skip the first turn") without checking whether the adopted framework already solved the problem natively.
3. No web research was performed on framework capabilities.
4. LangGraph already provides a native resume mechanism: `graph.invoke(None, config)` with the same `thread_id` loads the last checkpoint and continues without injecting new input (confirmed against official LangChain documentation, T1). The "Skip First Turn" workaround duplicates this framework functionality with custom code.

ADR-0041 governs the **veracity** of cited evidence (are search results real, are sources authoritative). It does not govern the **completeness** of research scope — specifically, whether the agent checked the framework for a native solution before writing custom code that duplicates it. This is a distinct gap: a perfectly cited, non-hallucinated ADR can still document a custom workaround that the framework already makes unnecessary.

Custom workarounds that duplicate framework functionality:
- Increase maintenance burden (custom code must track framework updates).
- Hide framework knowledge gaps (the workaround looks intentional, not accidental).
- Prevent adoption of framework improvements (native features are never discovered).
- Create false ADRs (decisions documented as "chosen" when they were "first available").

## Decision

We establish the **Framework-First Research Protocol** — a meta-layer research discipline, sibling to ADR-0041's Search Integrity Framework, governing when and how framework capabilities must be checked before custom implementation.

### Requirement 1: Framework Capability Check

Before implementing custom code that would duplicate a plausible framework responsibility, the agent must research whether the adopted framework solves the problem natively. The check asks three questions:

1. **Does the framework solve this natively?** Search the framework's official documentation and reference implementation.
2. **What is the documented pattern?** Identify the canonical API or configuration the framework provides for this use case.
3. **Are there community examples?** Confirm the pattern is in active use, not deprecated or experimental.

This check is scoped: it triggers when the custom code interacts with, wraps, or substitutes for a framework's responsibility domain (e.g., state persistence, graph execution, tool dispatch, checkpoint resume in LangGraph). It does **not** trigger for trivial utilities (string helpers, local formatting) that fall outside any framework's scope.

### Requirement 2: Justification Requirement

When a custom solution is chosen over a framework-native one, the ADR's "Considered Options" section must document:

- **Which framework solution was evaluated** (named API/pattern, with a citation per ADR-0041 provenance rules).
- **Why it was rejected** — a concrete reason (missing capability, performance, behavioral mismatch), not "it was unknown."
- **What the custom solution provides that the framework does not.**

An ADR that documents a framework-overriding decision without naming and rejecting the native alternative is incomplete. This mirrors ADR-FORMAT.md's "Rejected alternatives when the rejection is non-obvious" guidance, elevated to a **mandatory** requirement for framework-adjacent decisions.

### Requirement 3: Periodic Framework Audit

Existing custom workarounds should be re-evaluated against current framework documentation as the framework evolves. When a framework-native solution becomes available for a previously custom-solved problem, the corresponding ADR is transitioned to `superseded` (using the existing `docs/adr/superseded/` convention) and the native solution adopted. This is an ongoing practice, not a one-time activity.

### Relationship to Model Selection

The issue proposes using a stronger model for framework research. This is subsumed by ADR-0041's Fail-Closed Research pillar: framework capability checks must use live web search (verified against the framework's official documentation), not rely on model parametric knowledge alone — regardless of model strength. A strong model's parametric knowledge of a framework API is still training-data, not verified research; the Search Integrity Framework already governs this.

## Considered Options

### 1. Fold into ADR-0041 as "Pillar 5: Framework-Capability Check"
Rejected. ADR-0041's scope is tightly bounded to the **veracity** of cited evidence (are sources real, authoritative, and verified). Adding "did you check the framework?" dilutes that scope and muddies the "Search Integrity" name. The two protocols have different triggers: ADR-0041 fires whenever *any* external evidence is cited; this protocol fires specifically when *custom code is about to duplicate a framework capability*. A standalone ADR keeps each meta-layer concern individually citable, consistent with ADR-0041's own "Relationship to Existing Controls" framing.

### 2. Mandatory for all custom code
Rejected. Over-broad. The protocol triggers on framework-adjacent decisions — code that interacts with or substitutes for a framework's responsibility domain. Forcing a framework-capability check on a string-trimming helper adds overhead with no benefit. The trigger criterion (plausible framework responsibility) keeps the protocol targeted.

### 3. Code-level enforcement (pre-commit hook scanning ADRs for framework-capability citations)
Rejected, for the same reason ADR-0041 rejected its analogous option: the check is semantic, not syntactic. A static scanner cannot determine whether a custom solution plausibly duplicates a framework capability, whether the named framework alternative was genuinely evaluated, or whether the rejection reason is substantive. The human-in-the-loop ADR review process is the authoritative gate; this protocol ensures the *input* to that review is complete.

## Consequences

- **Research overhead**: Framework-adjacent decisions now carry a mandatory capability-check step (search official docs, identify native pattern, evaluate against custom approach). This is the deliberate trade-off: the cost of redundant custom code (maintenance burden, false ADRs, missed framework improvements) is far higher than the cost of a pre-implementation search.
- **Honest documentation**: ADRs for framework-overriding decisions now name the native alternative and the rejection reason, making the trade-off explicit rather than implicit. A future reader sees not just *what* was chosen, but *what was rejected and why*.
- **Supersession path**: The periodic audit creates a structured mechanism for retiring custom workarounds as frameworks mature, reducing accumulated technical debt.
- **Scope boundary**: The "plausible framework responsibility" trigger criterion is a judgment call. The human ADR reviewer is the backstop against over- or under-triggering; the protocol documents the intent, not a mechanical rule.

## Inspiration & References

- **LangGraph Interrupts — Resuming with `None` input** (LangChain official documentation) — T1
  https://docs.langchain.com/oss/python/langgraph/interrupts
  Accessed: 2026-07-15. Verified: fetched and confirmed — the official docs show `graph.invoke(None, config=config)` resumes from the last checkpoint with `thread_id` as the persistent pointer, establishing that "Skip First Turn" duplicated a native capability.

- **Resume from checkpoint via `thread_id` + `None` input** (LangChain Forum, official) — T2
  https://forum.langchain.com/t/can-we-resume-from-the-checkpoint-and-continue-running-at-the-interruption-point-instead-of-starting-from-the-first-node/1240
  Accessed: 2026-07-15. Verified: cross-referenced with the official docs above — forum response confirms `None` input lets the runtime load the last checkpoint without injecting new state, distinguishing resume from re-invocation.

- **ADR-0041: AI-Assisted Web Search Quality Controls** (this repository) — T1 (internal canonical)
  ./0041-ai-assisted-web-search-quality-controls-and-source-quality-framework.md
  Accessed: 2026-07-15. Verified: read in-repo; establishes the Search Integrity Framework whose scope (veracity) this ADR complements rather than extends.

- **"Don't reinvent the wheel" — framework design principles** (Software Engineering Stack Exchange community) — T3
  https://softwareengineering.stackexchange.com/questions/133170/are-there-general-rules-or-best-practices-for-building-a-new-framework
  Accessed: 2026-07-15. Verified: fetched; corroborates the industry-wide maxim that frameworks should depend on existing frameworks rather than re-implementing their capabilities. T3 — used as supporting context, not as sole basis for any decision.
