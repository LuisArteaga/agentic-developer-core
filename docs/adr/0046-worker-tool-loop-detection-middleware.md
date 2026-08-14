# ADR 0046: Worker Tool Loop Detection via LangChain Middleware

## Status

Accepted

## Context

Issue #121 (`z-ai/glm-5.2` Worker) is the root-cause failure behind issue #13: on
every Execute attempt the Worker repeated `python3 -m pip install -e ".[dev]"`,
received pip's usage text back each time (exit 0, no install performed), and
retried the *identical* call until `GraphRecursionError` — exhausting the
Recursion Budget (ADR-0045, default 50 ≈ 25 tool iterations) with 25–66 tool
invocations per attempt and never creating the planned files. All 3 verify
attempts failed.

ADR-0045 already established the three-layer defense for the reasoning budget,
with the `recursion_limit` as a **crash guard** — not a real budget — and made
raising the limit alone a documented anti-pattern ("if there's a genuine cycle,
a bigger cap just delays the crash"). What was missing was an *earlier, cheaper*
layer that detects the specific pathology — repeating an identical failing tool
call — and breaks the loop **before** the budget is spent, so the partial
trajectory is meaningful and the existing Hybrid Retry / Test-Writer retry loop
takes over with something to work from.

Two constraints shaped the design (both inherited from issue #121):

- The Recursion Budget (ADR-0045) remains the crash guard; loop detection is an
  earlier layer, **not** a replacement. The Graceful Budget Exhaustion external
  contract must be preserved: a loop-detection force-terminate counts as one
  failed attempt, the partial trajectory is still captured via
  `stream(stream_mode="values")`, and the Worker Trace sidecar + Run
  Observability metrics still run on it (ADR-0016 / ADR-0029).
- Configuration (warn threshold, hard limit, window size) must be resolvable
  per-node alongside Model Config, consistent with how `recursion_limit` is
  resolved — co-located, not a parallel config path.

The failure is **model-agnostic by necessity**: non-OpenRouter/non-OpenAI models
are more prone to this behaviour, which is exactly the case here.

## Decision

Add a `ToolLoopDetector` (pure helper) + `LoopDetectionMiddleware` (a LangChain
`AgentMiddleware`, `langchain.agents.middleware`) attached to the Worker's
prebuilt `create_agent`. This is the framework-idiomatic interception point:
LangChain 1.x middleware wraps the `create_agent` runtime with `before_model` /
`wrap_tool_call` hooks (verified empirically — see References).

### Observation — `wrap_tool_call` (sync)

Every tool invocation is observed *before* it executes: the detector records
`(name, _canonicalize_args(args))` into a bounded `deque(maxlen=window_size)`.
Arguments are canonicalized via `json.dumps(..., sort_keys=True, default=str)`,
so **name + full arguments** is the key. This is deliberate:

- Paginated reads that differ only by offset are *different* keys → not flagged.
- A transient retry that succeeds on the second identical attempt (count 2) is
  not stopped because `warn_threshold` defaults to 3 (≥ 2, per the issue's
  edge case).
- The sliding window catches short oscillations (A→B→A→B…) once a single
  signature accumulates within it (window default 10 = 2 × hard_limit).

### Decision — `before_model` (sync, `can_jump_to=["end"]`)

`before_model` runs **before every model call**, including after tools (the
`loop_entry_node` in the agent graph is the first `before_model` node; tools
route back to it). It inspects the detector and acts:

- At `loop_warn_threshold` (default 3) repeats within the window → inject a
  steering `SystemMessage` (`[LOOP DETECTION] … try a different approach or
  finish …`). The warn fires **once per signature per window episode** (the
  signature is added to a `_warned` set, pruned when it leaves the window), so
  the steering message is not re-injected every turn — the model gets one clear
  signal and a chance to justify continuation before the hard stop.
- At `loop_hard_limit` (default 5) → return `{"jump_to": "end"}`, which routes
  the graph to `END` (the `jump_to` state field + `can_jump_to=["end"]` is
  LangChain's supported termination path from `before_model`; `Command goto` is
  **not** supported in middleware — verified from source).

The agent force-terminates **before** the next model call, so the model never
makes a `(hard_limit+1)`-th identical call. The partial trajectory (all tool
calls + results + the steer message) was already captured by
`execute_worker`'s `stream(stream_mode="values")` loop.

### Integration with `execute_worker` (ADR-0045 contract preserved)

`execute_worker` resolves the loop config per-node, constructs the detector +
middleware, and passes `middleware=[loop_mw]` to `create_agent`. After the
stream, it checks `loop_middleware.terminated` and — mirroring the
`[RECURSION_LIMIT_EXHAUSTED]` path — returns a deterministic `[LOOP_DETECTED]`
message **after** the Worker Trace sidecar and `_record_run_metrics` have run on
the partial trajectory. One force-terminate is one failed attempt: the caller's
retry/verify logic drives self-correction (Hybrid Retry for Execute, ADR-0034;
the 3-attempt loop for Test-Writer). The Recursion Budget remains the crash
guard for genuine runaways that do *not* repeat (loop detection never raises;
it routes via `jump_to`).

### Configuration — co-resolved per-node

Three new Model Config fields, resolved by `_resolve_loop_config(node_name,
factory_cfg)` via the shared `_resolve_int_env` helper (mirrors
`_resolve_recursion_limit`'s precedence):

1. Node-specific env: `{NODE}_LOOP_WARN_THRESHOLD` / `_LOOP_HARD_LIMIT` /
   `_LOOP_WINDOW_SIZE`
2. General env: `AGENT_LOOP_*`
3. `factory.json` `loop_warn_threshold` / `loop_hard_limit` / `loop_window_size`
4. Hardcoded defaults (`3` / `5` / `10`)

A malformed value (env or factory) is logged and ignored, degrading to the next
tier. A `loop_hard_limit <= 0` disables loop detection for that node (no
middleware attached), leaving the Recursion Budget as the sole guard. Loop
detection is active for both Execute and Test-Writer by default (both resolve
the same defaults unless overridden per-node).

## Consequences

- A Worker stuck repeating an identical failing tool call no longer burns the
  full Recursion Budget: it is steered at 3 repeats and force-terminated at 5,
  leaving a usable partial trajectory for the retry loop.
- The ADR-0045 contract is unchanged: force-termination counts as one failed
  attempt; the partial trajectory is observable (trace + TL/token metrics).
- Loop detection is model-agnostic and configurable per-node, co-located with
  Model Config — no magic constants in the worker.
- The Recursion Budget still fires (and is still logged) for genuine runaways
  that do not repeat an identical call — the two layers compose.

## Considered Options

### 1. Built-in `ToolCallLimitMiddleware`
Rejected — LangChain ships `ToolCallLimitMiddleware` / `ModelCallLimitMiddleware`
(verified importable from `langchain.agents.middleware`), but they cap **total**
call counts keyed on tool **name** only (not arguments), with `continue`/`error`/
`end` exit behaviour and no warn-then-steer gradient. Keying on tool name would
false-positive on legitimate varied-args repeats (e.g. `read_file` called 10×
with different paths) — the exact failure the LangChain forum flags for Deep
Agents. The issue explicitly requires name + **full arguments** keying with a
warn-then-terminate gradient, which the built-in does not provide.

### 2. Raise `recursion_limit` only
Rejected — the documented anti-pattern (ADR-0045 / official LangGraph docs): "if
there's a genuine cycle, a bigger cap just delays the crash." Also leaves the
crash-into-failure behaviour intact and does not preserve a meaningful partial
trajectory. Explicitly out of scope per the issue.

### 3. Wait for native progress-aware termination (langchain-ai/langchain#36139)
Rejected as a dependency — that issue ("progress-aware termination: detect
no-progress loops in agent tool execution") is **open and unshipped** as of
mid-2026. No native no-progress middleware ships yet; custom middleware is the
correct, current approach (Framework-First Research, ADR-0042 — the native
alternative was checked and named, and rejected because it does not exist yet).

### 4. Key on `(name, args, result)` (include the tool result)
Rejected for this issue — keying on the result would be stricter, but the issue
explicitly specifies name + full arguments only. The run_command signal-quality
concern (pip's usage text returned with exit 0) is a **separate, out-of-scope**
concern per the issue's cross-cutting section; loop detection handles the
repetition layer regardless of result content.

## Inspiration & References

- **LangChain `AgentMiddleware` — `before_model` / `wrap_tool_call`** (installed source, `langchain` 1.3.14) — T1 (authoritative primary, source code)
  `langchain/agents/middleware/types.py`; `langchain/agents/factory.py`
  Accessed: 2026-07. Verified: read directly via `inspect.getsource` —
  `before_model` returns state updates (dict) and is the `loop_entry_node`
  (factory.py:1602-1603, tools route back to it); `wrap_tool_call` intercepts
  each tool execution for "retries, monitoring, or modification". `Command goto`
  is **not** supported in middleware (factory.py:217-220) — the `jump_to` state
  field with `can_jump_to=["end"]` is the supported termination path from
  `before_model` (`_resolve_jump`, factory.py:1804-1816; `_get_can_jump_to`,
  factory.py:491-525). `AgentState` carries `jump_to` (NotRequired,
  `EphemeralValue`). Confirms the framework-native interception points.

- **End-to-end experiment with a synthetic repeat-model** (this repository, ephemeral) — T1 (empirical)
  A `RepeatModel` always requesting `run_command("pip install x")` was driven
  through a real `create_agent(..., middleware=[LoopDetectionMiddleware])` at
  `recursion_limit=50` via `stream(stream_mode="values")`. Result:
  `before_model` ran 6×, `wrap_tool_call` recorded 5 identical calls, the
  agent force-terminated cleanly via `jump_to="end"` (**no** `GraphRecursionError`),
  12-message partial trajectory captured, steering `SystemMessage` injected.
  Verified: run and observed directly on 2026-07.

- **"Stop AI Agents Looping on the Same Failed Tool Call"** (particula.tech) — T2 (vendor engineering blog)
  https://particula.tech/blog/stop-ai-agents-looping-same-tool-call-no-progress
  Accessed: 2026-07. Verified: fetched via `web_search` — articulates that
  `recursion_limit`/`max_iterations` "only cap total steps, not repetition" and
  that "the natural home for [a no-progress guard] is middleware" in LangChain
  1.0+, hashing recent `(tool, arguments, result)` tuples. Confirms middleware
  is the idiomatic interception layer (we key on name+args only, per the issue).

- **"Progress-aware termination: detect no-progress loops in agent tool execution"** (langchain-ai/langchain #36139) — T1 (authoritative primary, issue tracker)
  https://github.com/langchain-ai/langchain/issues/36139
  Accessed: 2026-07. Verified: fetched via `web_search` — confirms "Current
  safeguards (recursion/tool-call limits) only cap total steps. They do not
  detect stuck states." The native feature is an **open, unshipped** issue, so
  custom middleware is the current correct approach (Framework-First, ADR-0042).

- **"Loop prevention in Deep Agents with repeated tool calls"** (LangChain Forum) — T3 (community)
  https://forum.langchain.com/t/loop-prevention-in-deep-agents-with-repeated-tool-calls/4034
  Accessed: 2026-07. Verified: fetched via `web_search` — documents that the
  built-in `ToolCallLimitMiddleware`/`ModelCallLimitMiddleware` cap totals (not
  repetition) and false-positive on intentional repeated calls in Deep Agents;
  `wrap_tool_call` is the recommended custom hook. Confirms the built-in is
  unsuitable and the argument-keyed, warn-then-steer gradient is needed.

- **"Why Your LangChain Agent Keeps Calling the Same Tool in a Loop"** (TokenCircuit / dev.to) — T3 (community blog)
  https://dev.to/gabrielanhaia/why-your-langchain-agent-keeps-calling-the-same-tool-in-a-loop-and-how-to-stop-it-57gk
  Accessed: 2026-07. Verified: fetched via `web_search` — describes using
  LangGraph's pre-model hook to intercept before each LLM call and perform
  "transcript surgery" on loop detection (the older 0.x `pre_model_hook` name
  for the 1.x `before_model` middleware used here). Independent corroboration
  of the detect-then-steer-then-stop pattern.

- **ADR-0045: Configurable Worker Recursion Budget and Graceful Budget Exhaustion** (this repository) — T1 (internal canonical)
  ./0045-configurable-recursion-budget-and-graceful-exhaustion.md
  Accessed: 2026-07. Verified: read in-repo — establishes the three-layer
  budget defense, the `stream(stream_mode="values")` partial-trajectory
  capture, the per-node `_resolve_recursion_limit` precedence, and the
  "one exhaustion = one failed attempt" contract this ADR preserves and extends
  with an earlier layer.
