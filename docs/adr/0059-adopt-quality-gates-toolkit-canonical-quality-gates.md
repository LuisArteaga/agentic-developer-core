# ADR 0059: Adopt quality-gates-toolkit as the Canonical Quality-Gate Provider

* **Status**: Accepted
* **Date**: 2026-09-12
* **Deciders**: Luis Arteaga & The Architect

**Amended 2026-09-26:** the consumed toolkit tag moved from `v1.7.0` to
`v1.8.9` — still an immutable release tag (D-0007) — in
`.github/workflows/ci.yml` and `.pre-commit-config.yaml`, with `TOOLKIT_TAG` in
`scripts/test_ci_workflow_contract.py` following it. The caller shape is
unchanged: every input the caller passes still exists at `v1.8.9` with the same
name and default (diffing the two tags' `workflow_call` input blocks shows one
changed line — the internal `toolkit-ref` default, which this caller does not
pass), and the four consumed hook ids (`secret-scan`, `mypy`,
`semgrep`, `pip-audit`) are unchanged. No lockstep move is needed elsewhere —
the toolkit's `lint.yml` pins still read `RUFF_VERSION 0.16.6` /
`MYPY_VERSION 2.3.1`, so the `ruff-pre-commit` rev and the `pyproject.toml`
dev extras stay exactly as decided below. Behavior the judges gain with the
tag: a bounded completion cap (D-0021, default 32768 — this caller's
`config/factory.json` sets no judge `max_tokens`, so the default applies), a
per-call wall-clock ceiling with timeout re-route (D-0022), severity-blind
findings that must block the merge (D-0023), persistence of an undelivered
review body (D-0024), a bounded Semgrep ruleset-fetch retry shared by the hook
and `security.yml` (D-0025), the `<findings>`-block answer contract with a
one-shot retry and a fallback ladder for a still-unusable answer
(D-0026/D-0027), and a `fallback_model` validation boundary (D-0028 — all four
judge entries here name a fallback distinct from their `model`, so no warning
fires). The composite also gained an optional `batch-budget-chars` input
(judge-diff batching for large diffs); it is deliberately NOT adopted here.
`judge-token` stays mapped to `GH_PAT`: since D-0005 an identity-less run posts
a comment review, so the PAT is no longer required for the review to be
delivered, but the explicit mapping keeps ADR-0014's trusted-author check
satisfied without depending on that fallback.

## Context and Problem Statement

The quality-gate stack of this repository was extracted into the public
[quality-gates-toolkit](https://github.com/LuisArteaga/quality-gates-toolkit)
(D-0017 importable package; DECISIONS.md D-0001..D-0020) and released as
**v1.6.0** (gate extraction) and **v1.7.0** (per-language composites,
D-0020). `ot-telemetry-engine` already consumes the toolkit; this origin
repository remained the last consumer of its own privately maintained fork —
`.pre-commit-config.yaml` (ruff-pre-commit v0.3.0, local secret-scan/mypy
hooks, semgrep v1.65.0, pip-audit v2.10.1) plus the two local workflows
`.github/workflows/pr-checks.yml` (ADR-0057) and
`.github/workflows/llm-pr-review.yml` (ADR-0050).

Consequences of staying on the local fork: the unpinned dev-extra floors
(`ruff>=0.3.0`, `mypy>=1.0.0`) caused the historical local-vs-CI formatter
skew the toolkit's lint.yml comment cites as motivation; the toolkit's
newer lockfile-integrity false-positive handling in `secret-scan` never
reached this repo's pre-commit wiring; and every toolkit gate improvement
had to be manually re-ported into the origin copies.

We decide to make the origin repository dogfood its own product: all CI
quality gates and pre-commit hooks are consumed from the toolkit at the
immutable release tag `v1.7.0` (toolkit D-0007: never a floating ref; moved to
`v1.8.9` by the 2026-09-26 amendment).

## Decision

1. **Pre-commit** (`.pre-commit-config.yaml`): two sources only —
   `ruff-pre-commit` at `v0.16.6` (lockstep with the toolkit lint.yml pin;
   the toolkit ships no ruff hook by design, D-0016) and the toolkit at
   `v1.8.9` providing `secret-scan`, `mypy` (language: system, advisory —
   the CI lint pin stays authoritative), `semgrep` (1.65.0 → 1.177.0,
   toolkit-pinned isolated env), and `pip-audit` (2.10.1). The local
   `scripts/secret_scan.py` STAYS for local runs / `make secret-scan`;
   only the pre-commit wiring moves.
2. **CI** (`.github/workflows/ci.yml`): a single job calling
   `LuisArteaga/quality-gates-toolkit/.github/workflows/python-checks.yml@v1.8.9`
   — the per-language Python composite (toolkit D-0020), so a Python-only
   caller has no `Skipped` check entries by construction (the polyglot
   `pr-checks.yml` remains the toolkit's polyglot single-entry option).
   The caller passes the full documented input set (coverage floor 89,
   `orchestrator scripts` paths, tree-sitter prefetch, otel extras,
   `diff-exclude: uv.lock`) and `enable-llm-review: true` — the toolkit's
   serial cost gate (D-0001, restated inside the language composite)
   reproduces ADR-0020/ADR-0052 behavior parity. Explicit secrets
   mapping: `judge-token: ${{ secrets.GH_PAT }}` — `secrets: inherit` maps
   by NAME only, and an empty judge-token silently degrades to
   `github.token` → `github-actions[bot]` authorship, which the merge
   gate's trusted-identity check (ADR-0014) ignores. The caller grants
   `pull-requests: write` (a called workflow may only narrow scopes; the
   composite's llmreview job requests it — escalation otherwise dies as
   startup_failure). The pre-commit toolkit `rev` equals the composite tag
   (`v1.8.9`) — the single-source-of-truth lockstep pinned by the contract
   test.
3. **Retirement**: `.github/workflows/pr-checks.yml`,
   `.github/workflows/llm-pr-review.yml`, and their structure tests
   (`scripts/test_pr_checks_workflow.py`,
   `scripts/test_llm_pr_review_workflow.py`) are deleted — no external
   repository calls them anymore. A caller-shape contract test
   (`scripts/test_ci_workflow_contract.py`) pins the new surface: tag
   immutability, exact input set, secrets mapping, permissions grant, and
   pre-commit toolkit `rev` == `ci.yml` tag (single source of truth).
4. **Dev-extras lockstep** (`pyproject.toml`): `ruff==0.16.6`,
   `mypy==2.3.1` (the toolkit lint.yml pins exactly these versions).
   Verified on this codebase at decision time: `uvx ruff@0.16.6 format
   --check orchestrator scripts` → 50/50 clean; `mypy==2.3.1` → no issues
   in 50 source files.
5. **Local developer loop unchanged**: `make verify` still runs the local
   venv toolchain (now at pinned versions); `.github/workflows/
   secret-scan.yml` (gitleaks on push/PR to main) stays untouched.

## Consequences

* Check-run names change from `ci / pr-checks` to per-gate names
  (`ci / lint`, `ci / test`, …) with the judge check rendering as
  `ci / llmreview / llm-pr-review` — the default judge fragment
  (`llm-pr-review`, ADR-0055) still matches; a focused unit test covers the
  new names. Branch-protection required-checks need a human-side rename.
* First end-to-end validation is the adoption PR itself: judges review the
  adoption diff through the NEW pipeline, and its verdict block must parse
  with the existing verdict consumer.
* Judge roster parity: `config/factory.json` is flat; the toolkit's
  additive judge-config resolution (D-0015) must resolve the identical
  roster.
* The diff base semantics improve: the toolkit computes
  `git diff -w origin/<base_ref>...HEAD` (parity for main-targeted PRs,
  improvement for non-default bases).
* Production orchestrator runtime (`scripts/telemetry.py`,
  `scripts/entrypoint.py`) is deliberately untouched — orchestrator-runtime
  concerns are out of the toolkit's scope (D-0009).

## Supersedes

* ADR-0057 (pure-reusable `pr-checks.yml`) — its workflow file is retired;
  the deterministic-gate contract now lives in the toolkit.
* ADR-0050's origin-repo `llm-pr-review.yml` — retired; the verdict-block
  protocol (ADR-0019) and trusted-identity semantics (ADR-0014) are
  unchanged and now provided by the toolkit's implementation.

## Inspiration & References

* quality-gates-toolkit DECISIONS.md (D-0001 serial cost gate, D-0004
  neutral defaults, D-0007 immutable release tags, D-0014 pinned tool
  versions, D-0015 additive judge-config union, D-0016 hook ownership
  split, D-0017 importable judge package, D-0019 Python gate contract,
  D-0020 per-language composite contract) —
  [DECISIONS.md at tag v1.7.0](https://github.com/LuisArteaga/quality-gates-toolkit/blob/v1.7.0/DECISIONS.md)
  (T2, accessed 2026-09-12, fetched via `gh api` and cross-read against
  the v1.7.0 workflow sources).
* Consumer precedent: `ot-telemetry-engine` CI calling
  `quality-gates-toolkit/.github/workflows/pr-checks.yml@v1.0.2` —
  [ot-telemetry-engine ci.yml](https://github.com/LuisArteaga/ot-telemetry-engine/blob/main/.github/workflows/ci.yml)
  (T2 — first-hand internal repo, accessed 2026-09-12, verified by
  reading the caller file; bump to v1.7.0 tracked as a follow-up issue
  in that repository).
* Toolkit README "Pre-commit hook" section and hook interface
  [.pre-commit-hooks.yaml at tag v1.7.0](https://github.com/LuisArteaga/quality-gates-toolkit/blob/v1.7.0/.pre-commit-hooks.yaml)
  (T1 — canonical source artifact, accessed 2026-09-12, fetched and
  hook ids/`language_version` pins confirmed verbatim).
* Composite input contract
  [pr-checks.yml at tag v1.6.0](https://github.com/LuisArteaga/quality-gates-toolkit/blob/v1.6.0/.github/workflows/pr-checks.yml)
  (T1 — canonical source artifact, accessed 2026-09-12, fetched and
  input names/defaults verified verbatim against the caller; the caller
  initially wired this polyglot composite).
* Per-language composite contract (D-0020) and the Python entry point
  [python-checks.yml at tag v1.7.0](https://github.com/LuisArteaga/quality-gates-toolkit/blob/v1.7.0/.github/workflows/python-checks.yml)
  (T1 — canonical source artifact, accessed 2026-09-12, fetched and the
  input set / `needs` chain / relative `uses` refs verified verbatim;
  the caller targets this composite as the first consumer).
* Toolkit decision register at the amended tag (D-0021 bounded completion cap,
  D-0022 per-call wall-clock ceiling, D-0023 finding promotion threshold,
  D-0024 undelivered review body, D-0025 ruleset-fetch retry, D-0026
  `<findings>` answer contract, D-0027 fallback ladder, D-0028 `fallback_model`
  validation) —
  [DECISIONS.md at tag v1.8.9](https://github.com/LuisArteaga/quality-gates-toolkit/blob/v1.8.9/DECISIONS.md)
  (T2, accessed 2026-09-26, fetched via `gh api` and cross-read against the
  v1.8.9 `python-checks.yml` input block, `lint.yml` version pins,
  `judge_config.py` and `.pre-commit-hooks.yaml`; the composite's
  `toolkit-ref` default resolves to the same tag).
