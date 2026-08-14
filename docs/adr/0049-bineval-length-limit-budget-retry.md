# ADR 0049: BinEval Length-Limit Budget Retry for Reasoning Models

## Status

Accepted (extends [ADR-0027](./0027-pre-pr-bineval-soft-semantic-gate.md),
[ADR-0040](./0040-structured-output-max-tokens-strict-and-finish-reason-logging.md),
and [ADR-0047](./0047-split-per-gate-verify-attempt-counters.md); addresses
[issue #134](https://github.com/LuisArteaga/agentic-developer-core/issues/134))

## Context

On orchestrator run 2026-08-14 (Target Repository issue #13), the Pre-PR
BinEval soft gate ([ADR-0027](./0027-pre-pr-bineval-soft-semantic-gate.md))
silently degraded to `PASS` with `bineval_degraded=true`. The BinEval LLM call
failed with *"Could not parse response content as the length limit was
reached"* — `CompletionUsage(completion_tokens=4096, reasoning_tokens=4078)`.
The configured flash model spent the **entire** `max_tokens: 4096` budget
([ADR-0040](./0040-structured-output-max-tokens-strict-and-finish-reason-logging.md))
on hidden `reasoning_tokens` and emitted no parseable `BinEvalResult` verdict.
OpenRouter normalizes this truncation to `finish_reason=length`.

Per the soft-gate design this length-limit parse failure was treated as an
infrastructure failure → degraded `PASS`. The PR was created with **no semantic
grading at all** — the gate was effectively disabled for reasoning models, the
exact class of model configured for `bin_eval`. This is materially worse than a
genuine `FAIL`: a `FAIL` feeds structured feedback to the Worker for in-cycle
self-correction; a length-limit degradation produces neither a verdict nor
feedback, silently bypassing ADR-0027's purpose.

The failure class is specific to reasoning models: a non-reasoning model emits
no `reasoning_tokens`, so `max_tokens` is consumed only by the (compact)
verdict JSON, and `finish_reason=length` never occurs in practice.

## Decision

Adopt a **combined Option 1 + Option 3** fix (the issue's own enumerated
options). Option 2 (route BinEval to a non-reasoning model) is rejected.

### 1. Raise the BinEval `max_tokens` budget (Option 1)

Raise `config/factory.json` `bin_eval.max_tokens` from `4096` to `8192`. This
is justified by the **measured** reasoning length (4078 tokens observed in
production), not a blanket maximum: 8192 leaves ~4114 tokens for the verdict
JSON (10 checks, ~1–1.5k tokens) plus headroom, covering the observed p95
reasoning case. It mirrors the `plan` node, which already uses `max_tokens:
8192` for the same reason (large structured JSON). The cost control constraint
is honored: worst-case spend per BinEval call doubles, but BinEval is a single
flash-model call per verify attempt — the absolute cost remains negligible
relative to the Execute/Test-Writer ReAct loops.

### 2. Single retry with an enlarged budget on `finish_reason=length` (Option 3)

Add a **Length-Limit Budget Retry** inside `_run_bineval`: when the first call
returns `finish_reason=length` with no parseable result, retry **once** with an
enlarged `max_tokens` budget (`base * _BINEVAL_LENGTH_RETRY_MULTIPLIER`,
default 2×, capped at `_BINEVAL_LENGTH_RETRY_MAX_TOKENS_CAP`, default 16384).
This is the safety net for reasoning-length variability beyond p95 that a fixed
budget cannot statically cover.

Critical property — **this is a budget retry, not a semantic retry**: it does
**NOT** consume `attempts["bineval"]` ([ADR-0047](./0047-split-per-gate-verify-attempt-counters.md)).
The semantic budget counts genuine BinEval `FAIL`s (judgment failures the soft
gate exists to self-correct); a length-limit truncation is an *infrastructure*
failure (the model ran out of output room), categorically different from a
judgment failure. Conflating them would let a reasoning-model truncation
silently deplete the semantic budget, re-introducing the starvation pattern
ADR-0047 was created to fix. The retry is therefore invisible to the counter:
`_run_bineval` is a pure function that never touches `state["attempts"]`, and a
persistent length failure returns `None` → `_run_bineval_phase` treats it as a
degraded `PASS` (no increment), exactly as any other infra failure does.

Bounding: exactly one retry, then degrade. A persistent length failure after
the single retry degrades to `PASS` with `bineval_degraded=true` (preserving
[ADR-0027](./0027-pre-pr-bineval-soft-semantic-gate.md)'s graceful-degradation
contract) — it never loops. The retry budget multiplier and cap are
env-overridable (`BINEVAL_LENGTH_RETRY_MULTIPLIER`,
`BINEVAL_LENGTH_RETRY_MAX_TOKENS_CAP`) so the safety net can be tuned without
code changes.

### Why not Option 2 (route to a non-reasoning model)

Routing `bin_eval` to a non-reasoning model would avoid the failure class
entirely, but it changes the **grading model's semantics**. The flash model was
chosen deliberately for BinEval (cheap, and reasoning aids rubric judgment);
swapping it for a non-reasoning variant would alter the gate's calibration
without evidence that grading quality is preserved. Keeping the model and
tuning the output budget preserves the soft gate's intent and is the
lower-risk, reversible change. Option 2 remains available as a future lever if
reasoning-length variance proves unbounded even at the retry cap.

### finish_reason visibility

[ADR-0040](./0040-structured-output-max-tokens-strict-and-finish-reason-logging.md)
already logs `finish_reason` on parse failure via `_extract_structured_output`.
This ADR makes the length-limit class **distinct** in the logs: the retry path
emits a dedicated warning naming `finish_reason=length` and the enlarged
budget, and the post-retry degradation warning carries the retry's
`finish_reason`. Length-limit failures are now directly diagnosable and
distinguishable from other infra failures (network, auth, rate-limit) —
satisfying the issue's "repeated silent degradation should be noticeable"
edge case.

## Consequences

### Pros
- **The gate is no longer silently disabled for reasoning models.** A
  reasoning model that spends its budget on reasoning gets a second, enlarged
  chance to emit the verdict — restoring real semantic grading in the common
  case.
- **No semantic-budget starvation.** The budget retry is invisible to
  `attempts["bineval"]`, preserving ADR-0047's per-gate independence.
- **Non-reasoning models unaffected.** They never produce
  `finish_reason=length`, so the retry path is dead code for them — no cost or
  behavior change.
- **Reversible and tunable.** The default budget bump is a one-line factory
  change; the retry multiplier/cap are env-overridable.

### Cons
- **Worst-case spend rises** when the retry fires (a second flash call at up to
  2× the budget). Bounded to a single retry, and only on the rare length-limit
  class, so the absolute cost impact is small.
- **A fixed budget cannot statically guarantee coverage** of unbounded
  reasoning length. The retry cap (16384) is the hard ceiling; a model that
  reasons beyond it still degrades. This is an acceptable tail — the post-PR
  Review Judges remain the hard gate (ADR-0027).

## Inspiration & References

- **OpenRouter — Reasoning Tokens guide** (T1, accessed 2026-08-15): reasoning
  tokens are output tokens, charged accordingly, and consume the
  `max_tokens` budget; `finish_reason` is normalized to `stop`, `length`,
  `tool_calls`, `content_filter`, or `error`, with `length` marking
  `max_tokens` truncation.
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
  https://openrouter.ai/docs/api_reference/overview
  *Verification: official OpenRouter docs; confirms reasoning_tokens consume
  the budget and `length` is the truncation signal the retry keys on.*
- **DeepSeek R1 Troubleshooting Guide** (T3, accessed 2026-08-15):
  "Partitioning the `max_tokens` value so that a minimum number of tokens
  remain available for the final answer prevents the reasoning phase from
  consuming everything… Monitor `reasoning_tokens` in the usage response to
  calibrate for your workload." Directly motivates the measured-budget bump
  (Option 1) and the retry safety net (Option 3).
  https://www.sitepoint.com/deepseek-r1-troubleshooting-guide-common-issues-and-solutions-2026
- **koala73/worldmonitor #4983** (T3, accessed 2026-08-15): the identical
  root cause in another codebase — `deepseek-v4-pro` reasoning tier consuming
  `max_tokens` on reasoning tokens and returning empty `message.content`. Their
  enumerated fix options (raise `max_tokens` on short stages; disable
  reasoning for short outputs) mirror this issue's Option 1 / Option 2.
  https://github.com/koala73/worldmonitor/issues/4983
- **ADR-0027** (soft-gate contract), **ADR-0040** (`max_tokens` / `strict` /
  `finish_reason` logging), **ADR-0047** (per-gate retry-counter
  independence), **ADR-0021** (layered retry pattern the single retry
  mirrors).
