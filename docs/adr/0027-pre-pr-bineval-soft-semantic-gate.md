# ADR 0027: Pre-PR BinEval Review as a Soft Semantic Gate

* **Status**: Accepted (amended by ADR-0032; shared-counter decision superseded by ADR-0047)
* **Date**: 2026-07
* **Deciders**: Luis Arteaga & Antigravity

## Context and Problem Statement

The Verify phase runs the Target Repository's deterministic `make verify`
(lint, type-check, tests). That catches syntax/behavioral regressions but is
blind to semantic code-quality problems the issue itself cares about:
unnecessary abstraction, scope creep, unhandled edge cases, and ADR
violations. These are exactly the issues that cause an expensive post-PR
Review-Judge `FAIL` and a round-trip back to the Worker. We want a cheap,
in-cycle check that gives the Worker a chance to self-correct *before* PR
creation and the post-PR hard-gate judges run.

## Decision

Add a **Pre-PR BinEval Review** inside `verify_node`, running only after `make
verify` passes and before the graph transitions to PR creation. A single
lightweight LLM call (the `bin_eval` node, flash model) grades the candidate
diff against a 10-check binary rubric (`config/grading_rubric.md`) across four
dimensions — Completeness (3), Simplicity (3), ADR Compliance (2), Robustness
(2). The grading input is strictly language-agnostic: issue body, generated
plan, git diff, and the Target Repository's ADRs (Language Agnosticism,
CONTEXT.md).

Outcome routing reuses the existing `status` field — no graph node was added:

- **PASS** (or empty-diff skip, or LLM/infrastructure failure): reset the shared
  `attempts["verify"]` counter, clear feedback, leave `status="verifying"` →
  `route_after_verify` transitions to PR creation.
- **FAIL**: increment the shared `attempts["verify"]` counter and either
  transition to `executing` (retry with structured feedback listing each failed
  check by id + reasoning) or to `failed` at 3/3 exhaustion.

The BinEval phase is traced as a child span `orchestrator_phase_bineval` nested
under the `orchestrator_phase_verify` parent span (ADR-0016).

## Considered Options

- **A separate LangGraph node** for BinEval between `verify` and `pr`.
  *Rejected*: the issue constrains BinEval to run *inside* the Verify node after
  `make verify` passes, and the PASS/FAIL signal is fully captured by the
  existing `status` field — `route_after_verify` already routes
  `executing`→execute, `failed`→recovery, else→pr. A new node and edge would
  duplicate routing without adding expressive power (Radical Simplicity,
  ADR-0009).
- **One LLM call per dimension** (one rubric criterion per call), the
  LLM-as-judge best practice that avoids correlated, anchor-biased scoring.
  *Rejected as the default*: four+ extra flash-model calls per verify attempt
  is disproportionate for a *soft* gate whose failures are non-blocking and
  whose hard safety net is the post-PR judges. We instead use a single
  structured-output call returning all 10 checks. The rubric file is the
  versionable, editable artifact; if calibration proves poor, splitting into
  per-dimension calls is a localized change behind the same `_run_bineval`
  interface.

## Consequences

- **Soft gate, not a merge gate.** BinEval never permanently blocks; an
  infrastructure failure (API error, rate limit, malformed/empty structured
  output, missing diff, unreachable issue body) degrades to PASS. This is
  deliberate — the post-PR Review Judges (ADR-0014/0019) remain the hard merge
  gate. BinEval checks Completeness/Simplicity/ADR/Robustness; the post-PR
  judges check Syntax/Lint, Test Coverage, Architecture, Security — they do not
  overlap.
- **Shared counter.** `attempts["verify"]` is shared between `make verify`
  failures and BinEval failures (max 3 total), so a Worker cannot spin on
  BinEval forever. The counter resets only on a full verify success
  (make-verify pass AND BinEval pass/skip/infra-fallback).
  *(Superseded by [ADR-0047](./0047-split-per-gate-verify-attempt-counters.md):
  the shared counter let `make verify` failures starve the BinEval gate of
  retries — observed in production on 2026-08-14. BinEval and make-verify now
  track independent per-gate budgets.)*
- **Deterministic no-ADRs auto-pass.** When the Target Repository has no
  `docs/adr/`, the ADR Compliance checks are forced to PASS in code
  (`_apply_no_adr_autopass`), independent of LLM compliance with the prompt.
- **Diff capture.** `diff_cached` stages all changes (incl. untracked files)
  via `git add -A` then returns `git diff --cached`; staging is idempotent and
  never commits (the PR-Node re-stages). The diff is truncated to
  `BINEVAL_DIFF_MAX_CHARS` (default 20000) with an explicit note.

## Inspiration & References

- **LLM-as-Judge binary rubrics (Galtea)** — "Pick the lowest-precision scale
  that captures the distinction you care about. Binary pass/fail when the
  question is 'did the response violate the rubric, yes or no.'" Binary checks
  are recommended for hard/observable thresholds; we apply them to a soft gate.
  https://galtea.ai/blog/llm-as-a-judge-prompts-templates-rubrics-and-best-practices
- **Rubric-Based Evaluations & LLM-as-a-Judge (Medium)** — "Production
  coding-assistant evaluations typically layer rubric grading atop test
  grading: test pass rate first, then per-criterion code quality rubric for
  outputs that pass tests." BinEval layers exactly this way: `make verify`
  (test pass) first, then rubric only on passing diffs.
  https://medium.com/@adnanmasood/rubric-based-evals-llm-as-a-judge-methodologies-and-empirical-validation-in-domain-context-71936b989e80
- **Agentic Evaluation Patterns (github/awesome-copilot)** — the
  Generate→Evaluate→Critique→Refine loop matches the in-cycle retry we wire:
  BinEval FAIL feeds structured critique back to the Worker for one refine
  pass before PR creation. https://github.com/github/awesome-copilot/blob/main/skills/agentic-eval/SKILL.md
- **ADR-0016** (telemetry nesting), **ADR-0018** (`bin_eval` flat factory
  key), **ADR-0009** (Radical Simplicity — single call, no new graph node).
