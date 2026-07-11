# ADR 0017: tree-sitter-language-pack for Structural Outline Extraction

## Status
Accepted

## Context
The Plan-Node needs Structural Outlines (class/method/function signatures) to improve Localization — pointing the Worker at the right files and symbols so it explores less. The initial proposal (issue #27) suggested pure-Python regex parsers for Python, JS/TS, Java, and Go to avoid compiled dependencies.

Research showed that no maintained pure-Python library exists for parsing TypeScript. The pure-Python JS parsers (`esprima`, `pyjsparser`) are JS-only and abandoned (last releases 2018/2019). Hand-rolling 4 regex parsers would create significant maintenance burden and fragility — each parser would need its own edge-case handling for generics, decorators, multi-line signatures, and language evolution.

The project's existing minimal-dependency stance (urllib over requests, 3 deps in `pyproject.toml`) applies to *infrastructure resilience* — the Process Supervisor and API helpers should be zero-dependency. Structural parsing is a different problem domain where the stdlib cannot help across languages and the community has a standard, maintained tool.

## Decision
We use `tree-sitter-language-pack` — a single pip-installable package bundling pre-built tree-sitter grammar wheels for 306 languages (including Python and TypeScript/TSX). One dependency, one unified query API, robust AST parsing for all supported languages.

Install via: `uv add tree-sitter-language-pack`

Pre-built wheels are available for Linux (manylinux/musllinux), macOS, and Windows — no compiler required on standard platforms.

## Considered Options

### 1. Pure-Python regex parsers (issue #27 proposal)
Rejected. No maintained pure-Python TypeScript parser exists. Four hand-rolled regex parsers would be fragile, high-maintenance, and would misparse edge cases (generics, decorators, multi-line signatures) that evolve with each language release. Each parser would need its own test suite and ongoing fixes.

### 2. stdlib `ast` for Python + regex for TypeScript
Rejected. Splits the codebase into two parsing paradigms. The TypeScript regex parser would be a lone, unmaintained-by-community artifact with no upstream grammar updates. Creates an Outline Fidelity Asymmetry that adds conceptual complexity for no robustness benefit.

### 3. tree-sitter core + individual grammar packages
Not chosen. `tree-sitter` + `tree-sitter-typescript` + `tree-sitter-python` would require managing multiple packages. `tree-sitter-language-pack` bundles all grammars behind one API and is what Aider (the closest analog) uses for its repo map.

## Consequences

- **One dependency, one codepath**: All language outline extraction uses the same `tree_sitter` query mechanism. Adding Go or Rust outlines later requires only confirming their grammar is in the language pack — no new parser code.
- **Pre-built wheels**: No compiler needed on standard platforms. Falls back to source build (requires C toolchain) on exotic platforms — same graceful-degradation posture as other optional infrastructure.
- **Follows the field**: Aider uses `tree-sitter-language-pack` for its repo map; MarsCode Agent uses tree-sitter-equivalent structural analysis across 12 languages. This is the community standard for this problem.
- **Scope of dependency**: Used exclusively by the Plan-Node's outline extractor. The Process Supervisor, Git helpers, and API helpers remain zero-dependency. The minimal-dependency philosophy applies where it serves resilience; parsing is not such a case.
