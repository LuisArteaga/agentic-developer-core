# ADR 0021: Layered Retry and Model Fallback for Empty LLM Judge Responses

* **Status**: Accepted
* **Date**: 2026-07-12
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The PR Review Judges (`scripts/review.py`) occasionally receive empty `content` responses from OpenRouter — structurally valid API responses (`{"choices": [{"message": {"content": ""}}]}`) that carry no evaluative signal. The existing retry loop in `call_llm_for_review` (line 259) validates **response structure** (HTTP errors, missing `choices` block) but not **response quality**. An empty `content` passes validation, returns successfully, and is only caught post-hoc by `evaluate_response` (line 411), which maps it to `NEEDS REVIEW`. Per ADR-0014, `NEEDS REVIEW` unconditionally blocks the merge and triggers Failure Recovery — so a transient model hiccup structurally blocks the autonomous loop.

This is hard to reverse because the change alters `call_llm_for_review`'s signature (str → tuple), adds a `fallback_model` field to `config/factory.json`, and establishes a retry/fallback contract that `run_judge` and `build_review_body` depend on. A future reader will wonder why the retry loop inspects content quality, why `options` are dropped on fallback, and why the fallback indicator is visible-only in the review body.

## Decision Drivers

* **Loop Resilience**: A transient empty response should not block the autonomous loop when a retry or fallback could recover.
* **Verdict Fidelity**: When a fallback model is used, the human reviewer must be able to see it — a verdict from a substitute model carries different confidence than one from the primary.
* **ADR-0014 Preservation**: The hard merge gate (`FAIL`/`NEEDS REVIEW` → block) is unchanged. Fallback improves the *chance* of a valid verdict; it does not weaken the gate.
* **ADR-0019 Preservation**: The hidden verdict block remains a pure verdict contract (`judge_key: VERDICT`). Fallback metadata does not enter the parser contract.

## Decision

### Layered retry progression (3 attempts max)

1. **Attempt 1** — primary model, original prompt. Existing 2-attempt API-error retry wraps this call.
2. **Attempt 2** (only if attempt 1 returned empty `content`) — same primary model, with an explicit instruction appended: *"Your previous response was empty. Provide a verdict with `<reasoning>` and `<findings>` tags."*
3. **Attempt 3** (only if attempt 2 returned empty `content`) — fallback model, original prompt, `routing=None`, `options=None`, `temperature=0.0`.
4. **Fail** — return `NEEDS REVIEW` (current behavior preserved as last resort).

This collapses the issue's proposed 4-layer approach (retry, retry-with-instruction, fallback, fail) into 3 attempts. The explicit instruction is applied on the second empty-content retry, not as a separate layer.

### Empty-content check location

The empty-content check moves from `evaluate_response` (post-hoc) into `call_llm_for_review`'s retry loop (pre-emptive). The retry loop already owns "is this response usable?" via structural validation; an empty `content` is another flavor of unusable. Keeping all retry/fallback state in one function makes telemetry cleaner — one span, one return path.

### Fallback model source

A per-judge `fallback_model` field in `config/factory.json` (optional). Each judge specifies a capability-appropriate fallback. If absent, the retry loop skips the fallback attempt and goes straight to `NEEDS REVIEW` after exhausting same-model retries.

### Fallback call configuration

When falling back: `routing=None` (the fallback model may not be registered in the primary's routing list, per the Model Config Override glossary term), `options=None` (the primary's options — e.g., `{"thinking": "max"}` for DeepSeek — may not be supported by the fallback model), `temperature=0.0` (deterministic grading regardless of model).

### Return value change

`call_llm_for_review` returns `(response_body: str, metadata: dict)` where metadata is `{"used_fallback": bool, "final_model": str, "attempt_count": int}`. The caller (`run_judge`) unpacks both, passes the body to `evaluate_response`, and stores `used_fallback` / `final_model` into the judge result for `build_review_body` to render. Matches the existing tuple-return style (`run_judge` already returns a 4-tuple); no dataclasses introduced.

### Fallback Indicator (visible review section only)

When `used_fallback` is true, `build_review_body` renders a highlighted notice in the per-judge section — `⚠️ Fallback Model Used: This verdict was produced by {fallback_model} after the primary model returned empty responses.` — placed directly under the judge's status line. The hidden verdict block (ADR-0019) is unchanged: `merge_node` only consumes verdicts, not fallback metadata.

### Telemetry (minimal)

Two new span attributes on the `openrouter_chat_completion` span: `used_fallback` (bool) and `final_model` (str). A single `[INFO]` log line when fallback is triggered: `"[INFO] Judge {judge_key} fell back to model {fallback_model}"`. The existing per-attempt `[WARN]` logs cover API-error retries. No per-attempt success/failure log lines — aggregate span data surfaces trends without per-attempt noise.

## Considered Options

* **4-layer retry (issue's original proposal)**: separate retry, retry-with-instruction, and fallback as distinct layers. Rejected: the explicit-instruction nudge is better applied on the second empty-content retry than as a standalone layer, collapsing 4 → 3 attempts.
* **Environment variable for fallback model (`REVIEW_FALLBACK_MODEL`)**: one fallback model for all judges. Rejected: judges have different capability profiles (`kimi-k2.7-code` for syntax vs `deepseek-v4-pro` for security); a single fallback can't be capability-appropriate for all.
* **Cross-borrow another judge's model as fallback**: implicit coupling between judges. Rejected: changing one judge's model would silently change another's fallback.
* **Dataclass return value**: `ReviewLLMResult(body, used_fallback, final_model, attempt_count)`. Rejected: `review.py` uses plain functions and dicts throughout; a dataclass would be inconsistent with the file's style.
* **Fallback metadata in hidden verdict block**: extend the ADR-0019 contract. Rejected: `merge_node` only needs verdicts; fallback metadata would broaden the parser contract and risk breaking the regex on format drift.
* **Full per-attempt logging**: log every attempt with its outcome. Rejected: transient condition that should rarely fire; aggregate span data is sufficient for trend analysis.

## Consequences

* **Pros**:
  * Transient empty responses no longer structurally block the loop.
  * Human reviewers can see when a verdict came from a fallback model.
  * Per-judge fallback config respects capability differences between judges.
  * ADR-0014 and ADR-0019 contracts preserved — the merge gate and verdict parser are untouched.
* **Negatives**:
  * `call_llm_for_review`'s signature change (str → tuple) requires updating all callers and tests.
  * `call_llm_for_review` grows in responsibility (content-quality check, fallback logic). Mitigated by issue #51, which evaluates separating concerns in the LLM call path.
  * Fallback model options are dropped (`options=None`), which may produce lower-quality reasoning for judges that rely on model-specific features (e.g., `thinking: max`). Accepted: a verdict from a fallback model without special options is better than a structural `NEEDS REVIEW` block.

## Inspiration & References

* [Bifrost/Maxim AI: Retries, Fallbacks, and Circuit Breakers](https://www.getmaxim.ai/articles/retries-fallbacks-and-circuit-breakers-in-llm-apps-a-production-guide) — layered retry → fallback → fail pattern
* [DeepLearning.AI community: When to fallback vs retry](https://community.deeplearning.ai/t/when-should-an-llm-app-fallback-to-another-model-instead-of-retrying/893299) — fallback trade-offs, "not free" but better than failure
* [Cognigy: LLM Fallback](https://docs.cognigy.com/ai/agents/develop/gen-ai-and-llms/fallback) — fallback as temporary replacement, retry-first strategy
* [ADR-0014: PR Verification and LLM Judge Review Integration](./0014-pr-verification-and-llm-judge-review-integration.md) — the `NEEDS REVIEW` → block behavior preserved
* [ADR-0019: Combined PR Review Body with Hidden Verdict Block](./0019-combined-pr-review-body-with-hidden-verdict-block.md) — the verdict contract preserved
* [ADR-0018: Flat Factory.json Schema](./0018-flat-factory-json-schema-over-grouped-node-taxonomy.md) — the per-node model routing that makes per-judge `fallback_model` a flat-key lookup
