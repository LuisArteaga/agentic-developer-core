# ADR 0051: BinEval Reasoning Effort Cap

## Status

Accepted (extends [ADR-0049](./0049-bineval-length-limit-budget-retry.md);
addresses [issue #139](https://github.com/LuisArteaga/agentic-developer-core/issues/139))

**Amended 2026-08-26:** the `security` clause of the Decision ("Other reasoning
nodes untouched") was superseded by the judge model swap to
`z-ai/glm-5.3-flash` — see the amended bullet for the documented justification.

## Context

[ADR-0049](./0049-bineval-length-limit-budget-retry.md) responded to BinEval
reasoning-token starvation (4078/4096 `reasoning_tokens` at a 4096 budget) by
raising `bin_eval.max_tokens` to 8192 and adding a single enlarged-budget retry
on `finish_reason=length`. On 2026-08-14 (Target Repository issue #14) the same
call consumed **8192/8192 at the raised cap** — reasoning scaled to fill
whatever budget was configured, again leaving zero tokens for the verdict JSON
and silently degrading the soft gate to `PASS`.

Raising `max_tokens` is a treadmill: the reasoning demand grows to the budget
rather than being bounded by it, and each raise doubles the worst-case spend
while the Length-Limit Budget Retry (2×, capped at 16384) converts persistent
truncation into recurring double-cost calls. The root cause is an **unbounded
reasoning demand**, not an insufficient output budget — so the fix must bound
the demand, not chase it with budget.

## Decision

Cap the reasoning demand at the source: `config/factory.json` `bin_eval` gains
`"options": { "reasoning": { "effort": "low" } }`. The `options` dict is merged
verbatim into the OpenRouter request's `extra_body`
([ADR-0018](./0018-flat-factory-json-schema-over-grouped-node-taxonomy.md) flat
schema; `get_chat_model_from_config`), so this is a config-only change — the
unified `reasoning` object is OpenRouter's current cross-provider parameter,
translated per provider.

- **Effort `low`, not disabled.** [ADR-0049](./0049-bineval-length-limit-budget-retry.md)
  kept a reasoning model deliberately (reasoning aids rubric judgment) and
  held Option 2 (non-reasoning model) in reserve. Effort `low` is the middle
  rung: OpenRouter's effort ratios allocate ~20% of `max_tokens` to reasoning
  (~1638 of 8192), leaving ~6.5k tokens for the ~1–1.5k verdict JSON while
  preserving bounded reasoning for grading quality. `none` would collapse into
  Option 2 by another name.
- **Routing unchanged; provider-ignores-param risk covered.** All five routed
  providers (DeepInfra, SiliconFlow, Novita, Parasail, DeepSeek) declare
  `reasoning` / `reasoning_effort` support for
  `deepseek/deepseek-v4-flash-0731` (verified live, 2026-08-23). For providers
  lacking a specific effort level, OpenRouter maps the request to the nearest
  supported level; ordered provider routing and the ADR-0049 Length-Limit
  Budget Retry remain the safety nets for residual truncation or a misbehaving
  provider. The ADR-0027 soft-gate degradation contract is unchanged.
- **Other reasoning nodes untouched.** `security` (`options.thinking: max`)
  keeps its deliberate unbounded reasoning: the burn is specific to BinEval's
  single-call, fixed-budget, structured-output shape, not a general policy.
  *(Amended 2026-08-26: this clause no longer holds for `security`. The judge
  model swap replaced all four PR-review judges with
  `z-ai/glm-5.3-flash`, which does not support DeepSeek's native
  `thinking` parameter — so `"options": {"thinking": "max"}` was necessarily
  migrated to the standardized cross-provider form
  `{"reasoning": {"effort": "high"}}`, keeping security as the deepest-reasoning
  judge (`high`, not a BinEval-style `low`). The provider-independent
  standardized parameter is supported by every routed endpoint (verified via
  the OpenRouter model endpoints API); layered fallback to glm-5.2 and
  ADR-0021's retry semantics are unchanged. This remains specific to the
  model-platform migration — still not a general effort-cap policy.)*
- **Next lever unchanged.** If truncation persists despite the cap,
  ADR-0049's Option 2 (route `bin_eval` to a non-reasoning model) remains the
  escalation path; rollout should be observed via BinEval `finish_reason` and
  cost in `.agent_logs/metrics.jsonl` (Run Observability).

## Consequences

### Pros
- **The treadmill stops.** Reasoning spend per BinEval call is bounded at
  roughly 20% of the budget instead of empirically filling 100% of it; the
  enlarged-budget retry reverts to a genuinely rare tail rather than a
  recurring double-cost path.
- **Config-only and reversible.** No code change; the cap is one factory.json
  key removed or re-tuned without a deploy.
- **Grading semantics preserved.** The model and its (bounded) reasoning stay;
  no recalibration risk as with Option 2.

### Cons
- **Shallower grading.** Effort-`low` reasoning is less thorough; the soft
  gate may judge less deeply. Acceptable — BinEval is the soft gate; the
  post-PR Review Judges remain the hard merge gate (ADR-0027).
- **Residual truncation risk.** Effort is a heuristic, not a hard token cap;
  low-effort reasoning plus the verdict could still exceed 8192 on pathological
  inputs. Covered by the Length-Limit Budget Retry, then graceful degrade.
- **Provider-dependent semantics.** Effort-to-token mapping varies by provider
  (OpenRouter documents ratios, not guarantees); observed behavior must be
  monitored rather than assumed.

## Inspiration & References

- **OpenRouter — Reasoning Tokens guide** (T1, accessed 2026-08-23): the
  unified `reasoning` parameter (`effort`, `max_tokens`, `exclude`, `enabled`)
  with documented effort-to-allocation ratios (`low` ≈ 20% of `max_tokens`),
  nearest-supported-level mapping when a model lacks a requested effort, and
  effort↔`max_tokens` translation across providers. Direct basis for the
  parameter shape and the ~1638-token bound arithmetic.
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
  *Verification: official OpenRouter documentation; effort table and
  normalization rules read directly from the live page.*
- **OpenRouter — Model Endpoints API for `deepseek/deepseek-v4-flash-0731`**
  (T1, accessed 2026-08-23): every endpoint, including all five routed
  providers (DeepInfra, SiliconFlow, Novita, Parasail, DeepSeek), lists
  `reasoning`, `reasoning_effort`, and `structured_outputs` in
  `supported_parameters` — the parameter is declared-supported everywhere the
  router can send it, covering the issue's provider-ignores-param edge case.
  https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-flash-0731/endpoints
  *Verification: live API response inspected; `supported_parameters` checked
  per routed provider.*
- **LibreChat #11934 — Support new reasoning effort handling for OpenRouter**
  (T3, accessed 2026-08-23): the flat `reasoning_effort` request parameter is
  deprecated on OpenRouter ("only very few models actually support it now");
  the `reasoning` object with nested `effort` is the current pattern OpenRouter
  "will translate into the corresponding parameters for many other models".
  Confirms the unified-object choice over the legacy flat parameter.
  https://github.com/danny-avila/LibreChat/issues/11934
  *Verification: maintainer-facing integration issue describing OpenRouter's
  current parameter deprecation; consistent with the official docs above.*
- **ADR-0027** (soft-gate contract), **ADR-0040** (`max_tokens` /
  `finish_reason` logging), **ADR-0049** (budget raise + Length-Limit Budget
  Retry this ADR extends), **ADR-0018** (`options` verbatim passthrough).
