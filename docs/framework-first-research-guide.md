# Framework-First Research Guide

This is the operational playbook for [ADR-0042](./adr/0042-framework-first-research-protocol-before-custom-implementation.md). The ADR records the decision; this guide records how to execute it.

---

## When This Applies

The Framework-First Research Protocol triggers when you are about to implement **custom code that would duplicate a plausible framework responsibility**. "Framework responsibility" means a capability the adopted framework (LangGraph, LangChain, OpenRouter, tree-sitter, etc.) is designed to provide — state persistence, graph execution, checkpoint resume, tool dispatch, streaming, schema validation, and so on.

### Triggers (do the check)

- Custom code that wraps or substitutes for a framework's core behavior (e.g., a manual "skip first turn" logic that duplicates checkpoint resume).
- An ADR that documents a workaround, abstraction, or shim over framework behavior.
- A decision to build a feature the framework might already expose (persistence, human-in-the-loop, streaming, retries).

### Non-triggers (skip the check)

- Trivial utilities with no framework analogue (string formatting, local config parsing, path joining).
- Code in a domain no adopted framework covers.
- Bug fixes to existing custom code that has already passed a framework-capability check.

**When in doubt, do the check.** The cost of a redundant search is far lower than the cost of redundant custom code.

---

## The Framework Capability Check

Before writing custom code in a framework's responsibility domain, answer these three questions with **live web search** (per ADR-0041's Fail-Closed Research — do not rely on parametric model knowledge alone):

### 1. Does the framework solve this natively?

Search the framework's **official documentation** (T1/T2 source) for the capability.

```
[framework name] [capability description] site:[official-doc-domain]
```

Example: `LangGraph resume conversation from checkpoint site:docs.langchain.com`

- If a native API/pattern exists → proceed to question 2.
- If search returns nothing authoritative → document the search performed and proceed to custom implementation (the absence of a native solution is itself the justification).

### 2. What is the documented pattern?

Identify the canonical API or configuration the framework provides.

- Fetch the documentation page (use `fetch_url` for in-codebase research; manually for meta-layer research).
- Confirm the described behavior matches the problem you are solving.
- Note the exact API signature and version.

### 3. Are there community examples?

Confirm the pattern is in active, current use — not deprecated or experimental.

- Check the framework's official examples repository or docs.
- Look for recent (within ~1 year) community usage in discussions, issues, or blogs (T3 corroboration).
- If the pattern is marked deprecated or experimental → that is a valid reason to choose custom code, but it must be documented as the rejection reason.

---

## Justification Template

When a custom solution is chosen over a framework-native one, the ADR's **Considered Options** section must include a rejected-alternative entry in this shape:

```markdown
### [N]. [Framework-native solution name]

[Named API/pattern] from [framework] (version [X.Y]).

**Rejected because**: [concrete reason — missing capability, behavioral mismatch,
performance constraint, deprecation, etc. Not "unknown" or "not researched".]

**What the custom solution provides instead**: [the specific capability or
behavior the framework solution lacks.]
```

### Worked Example (the motivating case)

The "Skip First Turn on load" implementation (an external retrospective, referenced in Issue #65) duplicated LangGraph's native resume. Under this protocol, an ADR documenting that workaround would have been required to include:

```markdown
### LangGraph native resume via `graph.invoke(None, config)` with `thread_id`

LangGraph's checkpointer persists graph state per `thread_id`. Calling
`graph.invoke(None, config)` loads the last checkpoint and continues execution
without injecting new input — the documented native resume mechanism.

**Rejected because**: [would need a concrete reason — none existed; the
framework solves the problem completely.]

**What the custom solution provides instead**: [nothing the native solution
lacks — which is precisely why the custom workaround should not have been built.]
```

In this case, the protocol would have **prevented** the custom implementation: the framework-native solution could not be rejected on substantive grounds, so the custom code would not have been written.

---

## Periodic Framework Audit

Existing custom workarounds should be re-evaluated as the framework evolves.

### When to audit

- After a framework major/minor version upgrade.
- When a framework release notes mention a capability in the workaround's domain.
- During periodic ADR review (e.g., quarterly, or when touching the relevant code).

### How to audit

1. Identify ADRs documenting custom workarounds in a framework's responsibility domain.
2. Re-run the Framework Capability Check against current framework documentation.
3. If a native solution is now available and the original rejection reason no longer holds:
   - Move the ADR to `docs/adr/superseded/` with a `Status: superseded by [native solution]` note.
   - Adopt the native solution in code.
   - Open an issue tracking the migration.

---

## Relationship to ADR-0041 (Search Integrity)

These two protocols operate at the same meta-layer (AI-assisted research methodology) but govern different concerns:

| | ADR-0041 (Search Integrity) | ADR-0042 (Framework-First) |
|---|---|---|
| **Governs** | Veracity of cited evidence | Completeness of research scope |
| **Question** | Are search results real and authoritative? | Did you check the framework before building custom? |
| **Triggers** | Any external evidence cited in an ADR | Custom code in a framework's responsibility domain |
| **Failure mode** | Hallucinated/fabricated citations | Redundant custom code duplicating native capability |

Both apply simultaneously when framework-capability research cites external sources: ADR-0041 ensures the citations are real; ADR-0042 ensures the framework was checked at all.

---

## Red Flags — Framework-Knowledge-Gap Indicators

Stop and run the Framework Capability Check if you observe:

- **"I want to [verb] sessions/state/conversations"** → the framework likely has a persistence/resume API. Check before building.
- **A workaround named after the problem it avoids** ("skip first turn", "manual resume", "custom checkpoint") → strong signal of a duplicated native capability.
- **The human pre-specifies a solution** → treat as a hypothesis, not a directive. Verify the framework doesn't already solve it.
- **The implementation is simpler than expected for the problem** → the framework may be doing the hard part already; confirm you aren't fighting it.
- **No framework documentation was consulted before writing the ADR** → the decision is "first available," not "chosen."
