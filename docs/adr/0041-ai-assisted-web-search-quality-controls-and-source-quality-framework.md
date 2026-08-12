# ADR 0041: AI-Assisted Web Search Quality Controls and Source Quality Framework

## Status

Accepted

## Context

A critical incident exposed a systemic gap in how AI-assisted architecture research is conducted. During the Lessons Learned analysis of ADR-0006, it was discovered that:

1. A search-provider API key was missing after a tool reinstallation.
2. The agent was asked to perform web research on a technical stability question.
3. Without search active, the agent **hallucinated web search results** — claiming "web research shows X is better" — producing fabricated citations that looked plausible.
4. The hallucination was only detected because zero search credits were consumed despite claimed research.
5. An ADR was written based on this hallucinated evidence and accepted as "research-backed."

This is not an isolated risk. Academic research documents the same pattern at scale: Zhao et al. (2026) audited 111 million references across 2.5 million papers and estimated **146,932 hallucinated citations in 2025 alone**, concentrated after widespread LLM adoption. GhostCite (2026) found a 1.07% hallucinated-citation rate at top-tier AI venues — an 80.9% jump in 2025. The CiteCheck framework (arXiv:2605.27700) demonstrates that even strong LLMs cannot reliably distinguish real from fabricated citations from parametric knowledge alone; external retrieval is required.

The orchestrator's in-codebase `web_search` Worker Tool already has strong controls — annotation-based citation capture (ADR-0039, eliminating parse fragility), honest error signals (distinguishing "search broken" from "genuinely empty"), SSRF-protected URL fetch (ADR-0028), and Worker Trace logging. **The gap is not in the Worker Tool; it is at the meta-layer** — the methodology governing how an AI agent conducts research when writing ADRs and making architecture decisions. There are no quality controls on:

- Whether claimed search results are real (not hallucinated)
- Whether sources are authoritative enough for the decision type
- Whether the agent distinguishes training-data knowledge from live search results
- Whether a missing search tool fails loudly or silently degrades to hallucination
- Whether source provenance is tracked in ADR references

## Decision

We establish a four-pillar **Search Integrity Framework** governing all AI-assisted research that informs ADRs and architecture decisions in this repository.

### Pillar 1: Source Quality Tier Framework

A 4-level hierarchy classifying research sources by authority and accountability, informed by the Oregon State "Four Tiers of Sources" model and the Elsevier levels-of-evidence hierarchy:

| Tier | Label | Examples | Accountability |
|------|-------|----------|----------------|
| **T1** | Authoritative Primary | Peer-reviewed academic publications, official standards (RFC, W3C, ISO), CVE/NVD advisories, official language/library documentation (e.g., Python docs, OpenRouter docs), source code of the referenced library itself | Peer review or formal editorial/standards process |
| **T2** | Authoritative Secondary | Official project documentation, established vendor engineering blogs (AWS, Google, Microsoft), reputable industry publications with editorial oversight | Editorial oversight, institutional reputation |
| **T3** | Community | Stack Overflow, developer forums, community blogs, tutorials, conference talks | Community voting, no formal review |
| **T4** | Unverified | Social media posts, unverified community posts, AI-generated summaries without citations | None |

**Rule**: Security-critical and correctness-critical ADR decisions require **at least one T1 source**. Non-critical decisions may rely on T2 as the highest tier but must justify the absence of T1. T4 sources may supplement but never serve as the sole basis for any decision.

### Pillar 2: Fail-Closed Research

When web search is unavailable (missing API key, tool timeout, network failure), the agent must:

1. **Explicitly state**: "Web search unavailable — the following analysis is based on training data only and has not been verified against live sources."
2. **Never claim** "web research shows," "studies indicate," or "external sources confirm" without actual, citation-backed search results.
3. **Pre-research check**: Before beginning research, verify that the search tool is operational (a probe query that must return real results, not an empty or error response).

This mirrors the graceful-degradation principle documented for agent systems: when a non-essential capability (search) is unavailable, the agent enters a **degraded mode** with explicitly reduced confidence, rather than silently substituting parametric knowledge and presenting it as verified.

### Pillar 3: Citation Verification Protocol

Every claimed research result must pass a 3-step verification, informed by the CiteCheck retrieval-grounded detection model and Perplexity's cross-referencing approach:

1. **Search**: Execute a live web search. Results must come from the search tool's structured output (URL citations, annotations), not from the model's parametric memory.
2. **Fetch/Validate**: At least one cited URL per claim must be fetched and its content verified to support the claim. The orchestrator's `fetch_url` tool (SSRF-protected, ADR-0028) is the canonical fetch mechanism for in-codebase research; for meta-layer research, manual URL validation is acceptable.
3. **Cross-reference**: Claims require **at least 2 independent sources** (different domains/publishers). A single source, even T1, is insufficient for claims presented as established fact. Disagreement between sources must be surfaced, not suppressed.

### Pillar 4: Search Provenance in ADR References

The ADR "Inspiration & References" section (already established by prior ADRs) is enhanced to track source provenance. Each reference must include:

- **URL** (permanent link preferred: DOI, archived URL, or canonical doc URL)
- **Source Tier** (T1–T4)
- **Access date** (when the source was retrieved/verified)
- **Verification note** (one sentence: how the source was verified — e.g., "fetched and content confirmed," "cross-referenced with [second source]")

ADRs predating this framework are grandfathered; their references are not retroactively annotated. New ADRs and ADRs that cite external evidence must comply.

### Relationship to Existing Controls

This framework operates at the **meta-layer** (AI-assisted ADR research methodology) and is complementary to, not overlapping with, the orchestrator's in-codebase controls:

- **ADR-0039** (annotation capture) ensures the Worker Tool's `web_search` returns genuine citation data, not model-synthesized JSON. This framework governs how a *researcher agent* (e.g., dcode) uses search results when writing ADRs.
- **ADR-0028** (SSRF validation) protects the URL fetch mechanism. This framework governs *whether* and *how many* URLs are fetched for verification.
- **Worker Trace** (ADR-0016) logs the Worker's full ReAct conversation. This framework governs the research audit trail for ADR authoring, which is a separate concern.

## Considered Options

### 1. Automated hallucination detection via a verification LLM
Rejected as the primary mechanism. CiteCheck demonstrates that LLM-based citation verification improves with external retrieval (macro-F1 from 62.7 → 73.1 for GPT-class models with web search enabled), but even the best automated detectors remain below human-level accuracy. A verification LLM adds cost and latency to every research cycle, and its own hallucination risk is unbounded. The human-in-the-loop ADR review process is the authoritative verification gate; this framework ensures the *input* to that review is well-formed and honestly labeled.

### 2. Mandatory T1 sources for all ADRs
Rejected. Over-constrains routine decisions (e.g., "we use stdlib `urllib` over `requests`") that are adequately supported by T2 sources (official docs, established engineering blogs). The T1 requirement is scoped to security-critical and correctness-critical decisions, where the cost of a wrong decision is highest and the accountability bar must be highest.

### 3. Code-level enforcement (pre-commit hook scanning ADR references for tiers)
Rejected for now. A static check cannot verify that a cited URL actually supports the claimed finding, that the tier assignment is correct, or that the search was real rather than hallucinated. The verification is inherently semantic, not syntactic. A lightweight linter that checks for the *presence* of tier/access-date annotations in new ADR references is a possible future enhancement, but it would be a formatting check, not a quality gate.

### 4. Separate audit log file for all research queries
Considered but deferred. Logging every search query and result to a dedicated audit file (e.g., `.agent_logs/research_audit.jsonl`) would provide full traceability, but the research is conducted by external agents (dcode) whose tool calls are not captured by the orchestrator's logging infrastructure. The ADR reference section with provenance annotations is the pragmatic audit trail: it captures the *outcome* of research (which sources were used and how they were verified) without requiring a new logging pipeline. A dedicated audit log is viable if the research tool gains native query logging.

## Consequences

- **Research overhead**: The 3-step verification protocol (search → fetch → cross-reference) adds time and token cost to ADR research. This is the deliberate trade-off: the cost of a hallucinated ADR (wrong architectural decision, accepted as "research-backed") is far higher than the cost of verification.
- **Honest degradation**: When search is unavailable, ADRs are explicitly labeled as "unverified — training data only" rather than silently presenting parametric knowledge as research-backed. This prevents the exact failure mode that motivated this ADR.
- **Provenance trail**: Future readers can assess the evidentiary basis of any ADR by inspecting its references — not just *what* was cited, but *how authoritative* the source is and *how it was verified*.
- **Grandfathering**: Existing ADRs are not retroactively annotated, creating a transitional period where some ADRs have full provenance and others do not. This is acceptable: the framework governs new research, not historical archaeology.
- **Tier assignment is human judgment**: The T1–T4 classification of a given source is a judgment call (e.g., is a vendor engineering blog T2 or T3?). The framework provides criteria, not a mechanical classifier. The ADR author assigns the tier and the reviewer validates it — the same division of labor as the rest of the ADR review process.

## Inspiration & References

- **CiteCheck: Retrieval-Grounded Detection of LLM Citation Hallucinations in Scientific Text** (arXiv:2605.27700, Khajavi et al., 2026) — Demonstrates that citation hallucination detection requires external retrieval, not parametric knowledge alone; even strong LLMs (GPT-class, Claude, Gemini) improve substantially when web search is enabled but still cannot match a retrieval-grounded framework. Directly informs Pillar 3's verification protocol. https://arxiv.org/html/2605.27700v1
- **arXiv tightens policy on hallucinated references** (SMU Library, 2026) — Documents the scale of the problem: Zhao et al. audited 111 million references, estimated 146,932 hallucinated citations in 2025; GhostCite found a 1.07% rate at top-tier AI venues, an 80.9% jump. Establishes that hallucinated citations are a documented, growing systemic risk, not a one-off. https://library.smu.edu.sg/topics-insights/arxiv-tightens-policy-hallucinated-references
- **Four Tiers of Sources** (Oregon State University, open.oregonstate.education) — The academic source-tier model adapted for Pillar 1: T1 peer-reviewed → T2 credible secondary → T3 community → T4 unverified. The principle that "if a Tier 3 source describes a study, find the original Tier 1 source" directly informs the T1 requirement for critical decisions. https://open.oregonstate.education/goodargument/chapter/four-tiers-of-sources
- **Levels of evidence in research** (Elsevier Author Services) — The evidence hierarchy pyramid from systematic reviews down to expert opinion. Confirms the academic precedent for tiered source credibility frameworks. https://scientific-publishing.webshop.elsevier.com/research-process/levels-of-evidence-in-research
- **How Perplexity Chooses Which Sources To Cite: 5 Key Signals** (Addlly, 2026) — Perplexity's cross-referencing approach: verifies information across multiple independent sources, looks for agreement patterns, favors sources with domain authority and editorial quality. Directly informs Pillar 3's "at least 2 independent sources" rule. https://addlly.ai/blog/how-perplexity-chooses-which-sources-to-cite
- **Detect hallucinations for RAG-based systems** (AWS Machine Learning Blog) — RAG hallucination detection via embedding similarity, consistency checks across multiple generations, and citation verification. Confirms that "check how well the model's statements match what was actually found in the source documentation" is the canonical verification pattern. https://aws.amazon.com/blogs/machine-learning/detect-hallucinations-for-rag-based-systems
- **Graceful Degradation Patterns in AI Agent Systems** (Zylos Research, 2026) — Establishes the principle that when capabilities are unavailable, agents should "enter a degraded mode with reduced but still useful capabilities" and explicitly categorize capabilities as essential vs. non-essential. Directly informs Pillar 2's fail-closed protocol. https://zylos.ai/research/2026-02-20-graceful-degradation-ai-agent-systems
- **Reduce hallucinations when using search-grounded LLM responses** (Firecrawl) — "Require citations for every claim to create a verifiable chain from search result to output. Validate outputs against sources programmatically." Concise statement of the grounding principle underlying Pillar 3. https://www.firecrawl.dev/glossary/web-search-apis/reduce-hallucinations-search-grounded-llm-responses
- **ADR-0039** (OpenRouter URL Citation Annotation Capture) — The in-codebase control that ensures the Worker Tool's `web_search` returns genuine citation data. This framework operates at the complementary meta-layer. `docs/adr/0039-openrouter-url-citation-annotation-capture-via-chatopenai-subclass.md`
- **ADR-0026** (Web Search Worker Tool via OpenRouter Server-Side Search) — Established the server-side search approach and the honest error signal pattern (distinguishing "search broken" from "genuinely empty"). Pillar 2 extends this principle to the meta-layer. `docs/adr/0026-web-search-worker-tool-via-openrouter-server-side-search.md`
