# ADR 0031: Separation of Concerns in the Review LLM Call Path

* **Status**: Accepted
* **Date**: 2026-07-14
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

ADR-0021 added layered empty-content retry and model fallback to
`call_llm_for_review` in `scripts/review.py` to stop transient empty LLM
responses from structurally blocking the autonomous loop. That change was
necessary but pushed `call_llm_for_review` toward a god-function: after
ADR-0021 it owned config resolution, message construction, HTTP transport
(via `call_openrouter_api`), API-error retry (via `_call_with_api_retry`),
empty-content quality validation, the fallback-model progression, telemetry
span management, and metadata assembly - all in one body. ADR-0021 itself
flagged this growth and pointed at issue #51 as the mitigation.

Issue #51 asks to "divide and conquer" these responsibilities so future
changes (more retry layers, different fallback policies, observability tweaks)
land on a clean foundation instead of being bolted onto a monolith.

## Decision Drivers

* **Testability in isolation**: the retry/fallback policy should be exercisable
  without resolving a Model Config or standing up a telemetry tracer.
* **Radical simplicity**: this repository's own Architecture Compliance judge
  forbids unnecessary abstractions, boilerplate, redundant interfaces, and
  scaffolding for future use. The split must be the smallest one that removes
  the god-function smell - not a framework.
* **ADR-0021 preservation**: the layered progression (primary -> nudge ->
  fallback -> give up), the `(body, metadata)` return shape, the
  `routing=None/options=None/temperature=0.0` fallback call config, and the
  "one span, one return path" telemetry contract are all unchanged.
* **ADR-0014 / ADR-0019 preservation**: the hard merge gate and the hidden
  verdict block are untouched - this refactor is internal to the call path.

## Decision

Extract the **retry/fallback policy** into a dedicated pure-ish helper,
`_run_layered_retry(judge_key, model, messages, fallback_model, api_key,
routing, temperature, options) -> (body, used_fallback, final_model,
attempt_count)`, and reduce `call_llm_for_review` to three clearly separated
concerns:

1. **Config resolution** - `resolve_model_config(judge_key)` at the single
   boundary between callers and the factory. Config is runtime data, not
   interleaved with the call mechanism.
2. **Retry/fallback policy** - delegated to `_run_layered_retry`, which owns
   only "given a model, its messages, and an optional fallback, drive the
   transport until a non-empty response is obtained or the progression is
   exhausted." It is free of telemetry so it is unit-testable in isolation.
3. **Telemetry** - one `openrouter_chat_completion` span with input, output,
   `used_fallback`, and `final_model` attributes (ADR-0021's "one span, one
   return path").

The resulting layering of the call path is:

```
call_openrouter_api          # HTTP transport (raw urllib POST)
  <- _call_with_api_retry    # 2-attempt API-error retry (structure validation)
  <- _run_layered_retry      # empty-content retry + model fallback policy  [NEW boundary]
  <- call_llm_for_review    # config resolution + telemetry + delegation
  <- run_judge              # one judge end-to-end (batching, aggregation)
```

Each layer wraps a single, smaller failing unit - matching the industry
guidance to "keep the scope of retry as small as possible" (retry the call
that can fail, not the orchestration around it).

### Resolution of issue #51's open questions

* **Lift config resolution to the caller (`run_judge` / `main`)?** - **No.**
  `call_llm_for_review` is the single boundary; resolving there keeps config as
  runtime data at the call site. `run_judge` does call `resolve_model_config`
  a second time for its span label and default model, but resolution is a
  microsecond JSON read (per `orchestrator/config.py`) over an immutable file,
  and threading a resolved config through the `llm_caller` dependency-injection
  seam would change the 4-arg protocol and ~10 tests for marginal benefit. The
  duplication is a documented smell, not a bug.
* **Retry + fallback policy as a separate function/strategy object?** -
  **A plain function, not a strategy object.** A class/strategy abstraction for a
  single call-site would be exactly the "redundant interface / scaffolding for
  future use" the Architecture Compliance judge rejects. `_run_layered_retry` is
  the smallest extraction that makes the policy independently testable.
* **Separate telemetry via a decorator or context manager?** - **No.** A
  decorator/CM for a single use-site is boilerplate. Telemetry stays inline in
  `call_llm_for_review`; the win is moving it *out of* the policy so the policy
  is pure, not abstracting the span creation itself.
* **Content-quality validation: pre-emptive (retry loop) or post-hoc
  (`evaluate_response`)?** - **Pre-emptive, per ADR-0021.** `_run_layered_retry`
  owns "is this response usable?" via `_is_empty_content`.
  `evaluate_response`'s `if not content: return "Needs Review"` stays as the
  *terminal* path for the all-attempts-empty case (defensive), not as a
  validator the loop consults. The boundary is: policy = "usable?";
  `evaluate_response` = "given a usable body, what is the verdict?".
* **Boundary between `call_llm_for_review` and `run_judge`?** -
  `call_llm_for_review` = one logical LLM call (config + telemetry + policy);
  `run_judge` = one judge end-to-end (per-file batching, per-chunk calls,
  aggregation, judge-level telemetry). The existing `llm_caller=call_llm_for_review`
  dependency-injection seam already enforces this boundary and is preserved.

## Considered Options

* **Strategy object / `RetryPolicy` class wrapping the transport**: rejected -
  redundant interface for a single call-site; violates the simplicity rule.
* **Decorator or context manager for telemetry**: rejected - single-use
  boilerplate; telemetry belongs inline at the boundary function.
* **Lift config resolution to `run_judge` and pass a resolved `Model Config`
  in**: rejected - changes the `llm_caller` DI protocol and many tests for a
  microsecond-read deduplication; the single-boundary placement is preferable.
* **Dataclass return value for the policy**: rejected - ADR-0021 already
  rejected dataclasses for `review.py` ("plain functions and dicts throughout");
  the 4-tuple is consistent with the file's style and with `run_judge`'s 6-tuple.
* **Inject the transport into `_run_layered_retry` as a parameter**: rejected -
  unnecessary; tests already patch `review._call_with_api_retry` at module level,
  which the helper references directly. Adding a parameter would be DI ceremony
  with no testability gain here.

## Consequences

* **Pros**:
  * The retry/fallback progression is unit-testable without config or tracer
    mocks (a new `LayeredRetryPolicyTests` class exercises it directly).
  * `call_llm_for_review` shrinks to its three stated concerns and stops being
    a god-function; future retry/fallback changes edit `_run_layered_retry`
    without touching telemetry or config wiring.
  * No public signature changes: `call_llm_for_review`, `run_judge`, and
    `evaluate_response` keep their contracts; the `llm_caller` DI seam is
    preserved, so all pre-existing tests pass unchanged.
* **Negatives**:
  * One more private function to navigate; mitigated by a single call-site and
    a docstring stating its one concern.
  * `run_judge`'s second `resolve_model_config` call remains (documented above).

## Inspiration & References

* [LangChain `RunnableRetry` / `with_retry` / `with_fallbacks`](https://reference.langchain.com/python/langchain-core/runnables/retry/RunnableRetry) -
  retry and fallback are composable wrappers around an inner Runnable, and the
  guidance is to "keep the scope of the retry as small as possible" (retry the
  unit that can fail, not the whole chain). Our layering mirrors this: the
  policy wraps the single transport call, telemetry wraps the policy.
* [langchain-ai/langchain `runnables/retry.py`](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/runnables/retry.py) -
  reference implementation separating retry state from the underlying call.
* [Bifrost / Maxim AI: Retries, Fallbacks, and Circuit Breakers](https://www.getmaxim.ai/articles/retries-fallbacks-and-circuit-breakers-in-llm-apps-a-production-guide) -
  the layered retry -> fallback -> fail pattern that ADR-0021 adopted; this
  ADR keeps that policy intact while isolating it.
* [TrueFoundry: What Is LLM Fallback?](https://www.truefoundry.com/blog/what-is-llm-fallback) -
  route every model call through a single boundary; keep retry limits and
  fallback policy in the shared layer, not scattered across integrations.
* [ADR-0021: Layered Retry and Model Fallback for Empty LLM Judge Responses](./0021-layered-retry-and-model-fallback-for-empty-llm-judge-responses.md) -
  the policy this refactor extracts, and the ADR that named issue #51 as its
  mitigation.
* [ADR-0014: PR Verification and LLM Judge Review Integration](./0014-pr-verification-and-llm-judge-review-integration.md)
  and [ADR-0019: Combined PR Review Body with Hidden Verdict Block](./0019-combined-pr-review-body-with-hidden-verdict-block.md) -
  the merge-gate and verdict contracts preserved unchanged.
