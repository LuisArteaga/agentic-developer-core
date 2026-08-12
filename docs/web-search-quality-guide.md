# AI-Assisted Web Search Quality Guide

This is the operational playbook for [ADR-0041](./adr/0041-ai-assisted-web-search-quality-controls-and-source-quality-framework.md). The ADR records the decision; this guide records how to execute it.

---

## When This Applies

Any time an AI agent performs web research that will inform an ADR, a architecture decision, or a claim presented as "research-backed." This includes:

- Writing a new ADR that cites external evidence
- Revising an existing ADR with new external references
- Grilling sessions (`grill-with-docs-and-websearch` skill) that validate designs against industry patterns
- Any issue resolution that claims "best practices show" or "industry standard is"

---

## Pre-Research Checklist

Before starting research, verify that the search tool is operational:

1. **Probe the search tool.** Execute a trivial query (e.g., "Python documentation site"). If the tool returns real results with URLs, proceed. If it returns an error, empty results, or no citations, **stop and declare degraded mode** (see Fail-Closed Protocol below).
2. **Confirm the search provider.** Know which search backend is active (OpenRouter server-side search, Tavily, etc.). If you are unsure whether the search tool is actually executing or the model is answering from memory, treat it as unavailable.

---

## Fail-Closed Protocol

When web search is unavailable, missing, or returns no real citations:

1. **State it explicitly** at the top of the research output:
   > ⚠️ Web search unavailable — the following analysis is based on training data only and has not been verified against live sources.
2. **Never use phrases** that imply live research was conducted:
   - ❌ "Web research shows…"
   - ❌ "Studies indicate…"
   - ❌ "External sources confirm…"
   - ❌ "According to recent documentation…" (unless you fetched the doc)
3. **Mark the ADR status** as `Proposed (unverified)` rather than `Accepted` if it relies on unverified training-data claims.
4. **Flag for follow-up**: Note in the ADR that verification is pending and the decision should be re-validated once search is available.

---

## Source Quality Tiers

Classify every cited source into one of four tiers. The tier reflects **accountability** — what consequences exist if the source is wrong.

### Tier 1 — Authoritative Primary

Sources backed by formal review, standards processes, or direct artifact inspection.

| Source Type | Examples | How to Verify |
|-------------|----------|---------------|
| Peer-reviewed publications | arXiv papers, journal articles | Fetch the paper; confirm the claim appears in the abstract or results |
| Official standards | RFCs, W3C specs, ISO standards | Fetch the spec; confirm the section cited |
| Security advisories | CVE/NVD entries, GHSA advisories | Fetch the advisory; confirm the affected versions and severity |
| Official documentation | Python docs, OpenRouter docs, library API docs | Fetch the doc page; confirm the described behavior |
| Source code | The library's own repository | Read the source; confirm the implementation matches the claim |

### Tier 2 — Authoritative Secondary

Sources with editorial oversight or institutional reputation, but not peer-reviewed.

| Source Type | Examples | How to Verify |
|-------------|----------|---------------|
| Vendor engineering blogs | AWS, Google, Microsoft engineering blogs | Fetch the post; check it cites primary sources |
| Official project docs | Framework guides, migration guides | Fetch the guide; confirm the recommendation |
| Reputable industry publications | InfoQ, ThoughtWorks Technology Radar | Fetch the article; check editorial process |

### Tier 3 — Community

Sources with community feedback mechanisms but no formal review.

| Source Type | Examples | How to Verify |
|-------------|----------|---------------|
| Q&A sites | Stack Overflow, GitHub Discussions | Check answer score and acceptance; look for primary-source citations in answers |
| Community blogs | Personal technical blogs by recognized practitioners | Check author credentials; look for code examples that can be reproduced |
| Conference talks | YouTube conference recordings | Cross-reference claims with published slides or papers |

### Tier 4 — Unverified

No accountability mechanism. Use only as leads, never as evidence.

| Source Type | Examples |
|-------------|----------|
| Social media | X/Twitter posts, Reddit comments |
| Unverified posts | Forum posts without community validation |
| AI-generated summaries | Content that itself lacks citations |

### Tier Rules

- **Security-critical decisions**: At least **1 T1 source** required.
- **Correctness-critical decisions** (data integrity, concurrency, error handling): At least **1 T1 source** required.
- **Routine decisions** (library choice, config format): T2 as highest is acceptable; justify T1 absence.
- **T4 sources**: May be used as discovery leads to find higher-tier sources. Never cited as evidence.

---

## Citation Verification Protocol

For every claim presented as research-backed, execute these 3 steps:

### Step 1: Search

Execute a live web search. **Results must come from the search tool's structured output** (URL citations, annotations), not from the model's parametric memory.

- If the search tool returns citations → proceed to Step 2.
- If the search tool returns no citations → do not fabricate results. Either rephrase the query or declare degraded mode.

### Step 2: Fetch and Validate

At least **one cited URL per claim** must be fetched and its content verified to support the claim.

- Use `fetch_url` (for in-codebase research) or manually open the URL.
- Confirm the fetched content actually contains the information being claimed.
- If the URL 404s, returns unrelated content, or does not support the claim → discard it and find another source.

### Step 3: Cross-Reference

Claims require **at least 2 independent sources** (different domains/publishers).

- Two posts from the same blog are not independent.
- A vendor blog citing its own docs is not two independent sources.
- If sources disagree → surface the disagreement in the ADR; do not pick the one that supports your preferred conclusion.

---

## ADR Reference Format

Every external reference in an ADR's "Inspiration & References" section must include:

```markdown
- **[Source Title]** (Author/Org, Year) — [T1/T2/T3/T4]
  [URL]
  Accessed: [YYYY-MM-DD]. Verified: [one sentence on how — e.g., "fetched and content confirmed," "cross-referenced with [second source]"]
```

### Example

```markdown
- **CiteCheck: Retrieval-Grounded Detection of LLM Citation Hallucinations** (Khajavi et al., 2026) — T1
  https://arxiv.org/html/2605.27700v1
  Accessed: 2026-07-14. Verified: fetched and confirmed the macro-F1 improvement figures in §4.
```

### Grandfathering

ADRs predating ADR-0041 are not retroactively annotated. New ADRs and ADRs revised with new external evidence must comply.

---

## Search Query Templates

Use these templates to structure queries for different research types:

### ADR Validation (is this the right architectural choice?)

```
[technology/pattern] vs [alternative] [comparison|trade-offs|benchmark] site:[official-doc-domain]
```

Example: `OpenRouter server-side web_search vs Tavily API comparison trade-offs`

### Best Practices (what does the industry do?)

```
[practice/pattern] best practices [year] [site:vendor-blog OR site:official-docs]
```

Example: `RAG citation verification best practices 2026`

### Scientific Papers (is there academic evidence?)

```
[topic] [arxiv OR doi OR "peer-reviewed"] [year]
```

Example: `LLM citation hallucination detection arxiv 2026`

### Error Messages / API Changes

```
[exact error message OR API name] [documentation OR changelog] [version]
```

Example: `langchain-openai _create_chat_result annotations discarded`

---

## Red Flags — Hallucination Indicators

Stop and re-verify if you observe any of these:

- **No URLs returned** but the agent claims "research shows" → likely hallucinated.
- **URLs that 404** when fetched → fabricated or stale.
- **Snippet doesn't match the claimed summary** → the agent inferred something the source doesn't say.
- **Only one source** for a claim presented as established fact → insufficient cross-referencing.
- **Zero search credits consumed** despite claimed research → search never executed (the original incident indicator).
- **Perfect agreement across all sources** with no nuance → suspicious; real research surfaces trade-offs and disagreements.
