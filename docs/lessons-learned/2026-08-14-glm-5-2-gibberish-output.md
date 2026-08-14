# Lessons Learned: GLM-5.2 Gibberish / "Glitch Token" Output in This Repository

**Date:** 2026-08-14
**Context:** Interactive dcode sessions running `openrouter:z-ai/glm-5.2` produced degenerate token-salad output ("glitch token activation") five times while working on this repository (four original occurrences + one recurrence documented below).

## Symptom

Mid-session, the assistant's reply collapses into meaningless markdown-like fragments (slashes, quotes, partial words, no semantic thread). Once it happens in a thread, it tends to recur in subsequent turns of the same thread.

## Investigation

All text files in the repository, the skills directories, and the agent memory were scanned programmatically for classic glitch triggers and pathological content:

- Unicode control characters (C0/C1), format characters (Cf: zero-width, bidi marks, BOM), surrogates, noncharacters
- Extremely long lines
- Token-level repetition: consecutive repeated tokens, dominant repeated 4-grams, unigram diversity

## Findings

### Ruled out: glitch tokens / adversarial content

Zero control, format, surrogate, or noncharacter codepoints exist anywhere in the repo, skills, or instructions. **No file in this repository contains tokenizer-hostile content.** The "code snippet or LLM instruction causes it" hypothesis is disproven by direct measurement.

### Cause 1: Repetition poisoning from machine-generated logs

The repo contains extremely repetitive generated files. When an agentic session reads or greps them, the context becomes dominated by a single repeated n-gram — a documented degeneration attractor that the model begins imitating:

| File | Size | Unigram diversity | Repetition |
|---|---|---|---|
| `agent_logs/review.log` | 463 KB (5,432 lines) | 0.010 (99% redundant) | Same 3-line "empty content from primary model" block ~1,500× |
| `.agent_logs/otel_traces_2026-08-06.jsonl` | 470 KB | 0.081 | Same JSON span template ×564 |
| `uv.lock` | 501 KB | 0.250 | `}, { url =` ×1,228 |
| `.agent_logs/worker_trace_*.jsonl` | ~63 KB each | — | 15–16 KB single lines; 10–25 empty `{"role":"ai","content":""}` records per trace |

### Cause 2: Session self-poisoning

A gibberish assistant message remains in the conversation history and primes further gibberish (self-imitation). dcode persists threads (`~/.deepagents/.state/sessions.db`), so resuming a glitched thread carries the poison forward. This explains why the failure recurred "in this repository" — it was the same resumed thread(s), not independent triggers.

### Cause 3: Provider-stack instability for GLM-5.2

- The repo's own glossary documents the **Empty Content Response** phenomenon (CONTEXT.md, "Empty Content Response"), and `agent_logs/review.log` is 463 KB of exactly that failure from GLM-era judge runs.
- `z-ai/glm-5.2` was used with OpenRouter **auto provider routing**: different upstream providers serve the model with different quantization and effective context length. Sessions in this repo are unusually context-heavy (large system prompt + memory + pasted skill files + 99–175 KB source files). Silent mid-conversation truncation by an overloaded provider produces exactly the observed markdown-fragment salad.

## Countermeasures

1. **Never resume a glitched thread.** Start a fresh session — the gibberish in history keeps priming new gibberish.
2. **Keep high-repetition files out of the context window.** Grep them or read small `offset`/`limit` windows; never whole-file reads of `agent_logs/review.log`, `.agent_logs/*.jsonl`, or `uv.lock`.
3. **Housekeeping:** truncate or archive `agent_logs/review.log` and stale `.agent_logs/otel_traces_*.jsonl` once the incident they record is closed.
4. **Pin the model to a single OpenRouter provider** (provider allowlist) instead of auto-routing, or prefer a model that is stable on this stack (e.g. `moonshotai/kimi-k3`, `deepseek/deepseek-v4-*`) for work in this repository.

## Detection Snippet

The scanner used for this investigation (control/format chars, long lines, n-gram repetition, unigram diversity) is worth re-running whenever a model degenerates in a repo — it distinguishes "poisoned context" (this case) from genuine glitch-token content in under a minute.

## Recurrence: 2026-08-14, issue #123 session (5th occurrence)

### What happened

During a dcode session implementing issue #123 (BinEval counter split, branch `feat/split-verify-attempt-counter`), the assistant's reply degenerated into token salad mid-turn. Distinctive detail: the corruption began **inside a tool-call argument** — a `read_file` call was emitted with a `file_path` of pure garbage and failed with "not found". The immediately preceding `edit_file` (a ruff-format fix in `orchestrator/nodes.py`) had succeeded, and the full test suite (137 tests) had passed moments earlier. No work on disk was corrupted.

### New data points vs. the original findings

1. **No repetition-poisoned context this time.** The session had read only `CONTEXT.md`, ADR files, `orchestrator/nodes.py` / `test_nodes.py` windows, and the issue #123 body. None of the high-repetition files named above (`agent_logs/review.log`, `.agent_logs/*.jsonl`, `uv.lock`) were read or grepped. Cause 1 (repetition poisoning) is therefore **not a necessary trigger** — provider-side degeneration alone suffices.
2. **Failed `read_file` independently confirms the "no hostile file" finding.** The garbage never touched the filesystem; it originated in token *generation*, exactly as the scanner-based investigation concluded.
3. **Model-identity ambiguity.** The session environment reported `moonshotai/kimi-k3` while the observed/configured model was `z-ai/glm-5.2`. Either OpenRouter auto-routing served GLM-5.2 upstream despite the label, or the degeneration is not GLM-specific. Consequence: countermeasure 4 ("switch to a stable model") is unreliable *unless the provider allowlist is pinned* — the label alone does not guarantee the upstream model.

### Recovery actions that worked

- Verified on-disk state before trusting any post-glitch output (`git diff`, 137 passing tests, the format fix had landed). After a glitch, **trust the filesystem, not the transcript**.
- Continued the session and completed the turn without immediate recurrence — but countermeasure 1 (fresh thread) remains the safer default, since this thread now contains a gibberish message that primes self-imitation.

### Additional countermeasure

5. **After any glitched turn, re-verify work state from disk** (`git status`/`git diff`, `make verify`) before continuing. This failure mode is benign when corruption lands in a `read_file` path (fails closed, nothing read), but the same corruption inside an `edit_file` payload could write garbage — post-glitch verification is the safety net.
