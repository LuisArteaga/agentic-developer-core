# ADR 0058: Judge Usage Accounting and KPI Reporting

* **Status**: Proposed
* **Date**: 2026-08-31
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The LLM PR review (ADR-0014/ADR-0019) posts judge verdicts to every reviewed
pull request, but the review body says nothing about *how* the verdict was
produced. Which model actually served the request, through which provider,
with how many input/output tokens, and at what cost is invisible: the
`final_model` attribute reports only the *configured* model id, and token or
cost accounting exists nowhere. With four judges over chunked diffs
(ADR-0023) and a layered retry/fallback policy (ADR-0021), the actual spend
per review — and the endpoint that served each chunk — is unknowable from
the review, the step summary, or the telemetry spans.

OpenRouter returns the data on every response: the canonical `model` slug,
the serving `provider`, and a `usage` block with token counts; the
`usage.cost` field (and the cached/reasoning token breakdowns) require
opting in with a `usage: {include: true}` request field (usage accounting).

## Decision Drivers

* **Cost transparency**: judge runs are paid API calls on every PR in every
  target repository (ADR-0050); spend must be attributable per judge.
* **Routing observability**: pinned provider routing (`allow_fallbacks:
  false`, ADR-0021 fourth amendment) makes the *actually-serving* endpoint
  part of the quality story — verdicts from different endpoints are not
  interchangeable evidence.
* **Zero impact on verdict semantics**: KPI reporting must not touch the
  hidden verdict block (ADR-0019 writer/parser lockstep) or the merge gate.
* **Never break the review**: a malformed usage payload must degrade to
  missing KPIs, never fail a judge run.

## Considered Options

* **Option 1: Post-run reconciliation via the `/generation` endpoint**
  (rejected) — query OpenRouter per response id after the fact. Extra API
  calls, async delay, and another failure surface for data the response
  body already carries.
* **Option 2: Log-only accounting** (rejected) — cheapest, but logs scroll
  away; the review body is the durable, human-facing surface.
* **Option 3: Extract from the response body, aggregate, and render in the
  review body + step summary + spans** (chosen).

## Decision

We chose **Option 3**. Contract:

1. **Opt-in cost accounting on every call.** `build_payload` always adds
   `"usage": {"include": true}`; judge `options` from `factory.json` may
   still override it (config remains authoritative for payload keys).
2. **Extraction is pure and defensive.** `extract_usage(body)` reads
   top-level `model`/`provider` plus the `usage` block (token counts,
   cached/reasoning breakdowns, cost). Any structural surprise yields
   `None` fields instead of raising.
3. **Aggregation is a pure merge.** `merge_usages` sums token/cost fields,
   counts LLM calls, and joins distinct `model`/`provider` values in
   first-seen order — multi-batch runs (ADR-0023) and fallback attempts
   (ADR-0021) may legitimately hit different endpoints.
4. **Three render surfaces, one source.**
   - The posted review body gains a "Judge Usage & KPIs" table (per judge +
     Total row: model, provider, input/output/reasoning tokens, cost in
     USD, LLM calls, wall-clock duration) below the verdict summary. The
     hidden verdict block is untouched.
   - `$GITHUB_STEP_SUMMARY` receives the same table via
     `append_kpi_summary` (stateless header detection, same pattern as the
     retry-events section).
   - The `openrouter_chat_completion` span gains `llm.provider`,
     `llm.usage.prompt_tokens`, `llm.usage.completion_tokens`, and
     `llm.usage.cost_usd` attributes; the `[OPENROUTER] ok` log line
     reports provider/tokens/cost per attempt (covering discarded
     empty-content attempts whose usage never reaches the review body).
5. **Collection is side-band.** `run_judge`/`_run_single_chunk` accept an
   optional `usage_records` collector (default `None`); `main` passes a
   per-judge list, merges it, and adds `duration_seconds`. Callers that
   know nothing about the collector are unaffected.

## Consequences

* Every judge review now self-reports model, provider, tokens, and cost —
  per judge and in total — in both the PR review and the CI summary page.
* The review body grows by one table; consumers parsing only the hidden
  verdict block (merge_node, `parse_pr_verdicts.py`) are unaffected.
* Usage data reflects the *verdict-bearing* response of each chunk; a
  discarded empty-content attempt's tokens are visible only in the per-
  attempt log line — accepted to keep the retry path free of plumbing.
* `usage.cost` requires the OpenRouter usage-accounting opt-in; if a
  provider/model ever omits it, cells render `n/a` instead of failing.
