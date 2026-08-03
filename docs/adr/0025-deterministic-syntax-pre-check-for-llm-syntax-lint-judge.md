# ADR 0025: Deterministic Syntax Pre-Check for LLM Syntax/Lint Judge

* **Status**: Accepted
* **Date**: 2026-07-24
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The PR Review Judges (`scripts/review.py`) include a **Syntax/Lint** judge whose Q1 (Syntax Validation) criterion asks an LLM to visually inspect a git diff and identify Python syntax errors, indentation issues, and compilation problems.

On PR #68, this judge produced a **false positive**: it reported an `IndentationError` in `scripts/telemetry.py` claiming that `api_key = ...` and `headers = {}` were indented inconsistently. In reality, the file compiled cleanly — `py_compile`, `ruff`, `ruff format --check`, `mypy`, and 222 tests all passed. The LLM had misread the diff's visual whitespace (a blank line between the two statements created an illusion of an indentation shift).

This is a structural weakness: **an LLM reading a diff is an unreliable arbiter of Python syntax**, because diffs are visual artifacts where blank lines and context markers can create whitespace ambiguity. Python syntax validation is a deterministic operation that `py_compile` answers definitively.

This is hard to reverse because it changes the `syntax_lint` judge's prompt contract — future readers need to understand why the judge receives deterministic verification context and why the LLM is instructed not to flag syntax issues when `py_compile` passes. There were genuine alternatives (LLM-only, deterministic-only, hybrid).

## Decision Drivers

* **False-positive elimination**: The Syntax/Lint judge is a hard merge gate (ADR-0014). A false positive on Q1 blocks auto-merge, requiring manual intervention. Deterministic verification eliminates this failure mode entirely.
* **LLM reliability for whitespace**: LLMs are fundamentally unreliable at reading diff whitespace — blank lines, context markers, and `-w` diff normalization create visual ambiguity that a compiler resolves trivially.
* **Deterministic-first principle**: Syntax validation is a solved problem. The LLM should focus on what it's good at (naming conventions, JSON schema adherence, semantic issues) and defer to deterministic tools for what they're good at (compilation, linting).

## Considered Options

* **Option 1: Deterministic pre-check that augments the LLM prompt (chosen)**
  Run `py_compile` on all modified `.py` files in the workspace checkout before the LLM judge. Inject the result into the `syntax_lint` judge's prompt as ground truth. If all files compile, Q1 is marked PASS and the LLM is instructed not to flag syntax/indentation issues. If compilation fails, the error is injected as a confirmed finding.
  * *Pros*: Eliminates false positives on Q1 while preserving the LLM's judgment for Q2 (JSON Schema) and Q3 (Naming Conventions). Follows the existing prompt-augmentation pattern (architecture judge's `=== REPOSITORY ARCHITECTURE CONTEXT ===` block). Minimal code change.
  * *Cons*: The LLM still runs for Q1 (cannot be fully short-circuited without splitting Q1/Q2/Q3 into separate judge calls, which would be a larger refactor).

* **Option 2: Deterministic-only — replace Q1 entirely with py_compile**
  Remove Q1 from the LLM judge entirely; run `py_compile` as a standalone gate.
  * *Pros*: Simplest, no LLM cost for Q1.
  * *Cons*: Breaks the existing judge contract (one LLM call per judge producing a single verdict). Q1, Q2, Q3 share a single LLM call and a single `<findings>` block — splitting Q1 out would require restructuring the prompt, the response parser, and the verdict aggregation. Over-engineered for the problem.

* **Option 3: Rely on existing CI checks (ruff, mypy, pytest)**
  The CI workflow already runs `ruff check`, `mypy`, and `pytest` — all of which would catch real syntax errors. Remove Q1 from the LLM judge entirely and trust the CI pipeline.
  * *Pros*: Zero new code.
  * *Cons*: The LLM review runs with `if: always()` in CI — it runs even when earlier steps fail. If we remove Q1, the LLM judge can no longer report syntax issues in its review body, losing visibility. Also, the LLM judge runs in the orchestrator's merge-polling path, which may not have access to CI step results.

## Decision

We chose **Option 1**.

Add a `verify_python_syntax(workspace_dir, diff)` function to `scripts/review.py` that:

1. Uses `split_diff_by_file(diff)` to extract modified filenames from the diff.
2. Filters for `.py` files.
3. Runs `py_compile.compile(filepath, doraise=True)` on each file in the workspace checkout (`GITHUB_WORKSPACE`).
4. Returns `(all_passed, errors, files_checked)`.

In `main()`, before the judge loop, call `verify_python_syntax()`. When `judge_key == "syntax_lint"` and `files_checked > 0`, augment the prompt:

- **All pass**: Inject `=== DETERMINISTIC SYNTAX VERIFICATION ===` block stating Q1 is PASS, instruct the LLM not to flag syntax/indentation/compilation issues, and focus on Q2 and Q3.
- **Any fail**: Inject the `py_compile` errors as confirmed findings and mark Q1 as FAIL.

This follows the exact same prompt-augmentation pattern as the architecture judge (ADR-0022's `=== REPOSITORY ARCHITECTURE CONTEXT ===` block) — a clearly delimited section appended to the system prompt with ground-truth context.

### Edge cases

* **No Python files in diff** (`files_checked == 0`): No prompt augmentation — the LLM handles Q1 as before (for JSON, YAML, etc.).
* **Deleted files**: Silently skipped (file doesn't exist in workspace).
* **Non-Python files**: Not checked (only `.py` files are compiled).

### Consequences

* **Pros**:
  * False positives on Q1 (Syntax Validation) are eliminated for Python files — the LLM is explicitly told the code compiles.
  * Real syntax errors are caught deterministically and injected as confirmed findings — the LLM cannot miss or dismiss them.
  * The LLM's attention is redirected to Q2 (JSON Schema) and Q3 (Naming Conventions), where it adds value.
  * Follows the established prompt-augmentation pattern — no architectural change to the judge loop or verdict aggregation.
* **Negatives**:
  * The LLM still runs for Q1 (an LLM call is still made). This is acceptable because Q2 and Q3 share the same call.
  * `py_compile` only covers Python files. Non-Python syntax issues (JSON, YAML) remain LLM-judged — but Q2 already covers JSON schemas separately.

## Inspiration & References

* **G-Research: Building a Code Review Tool — LLM Patterns That Work ([G-Research Blog](https://www.gresearch.com/news/building-a-code-review-tool-the-llm-patterns-that-actually-work))**: "Treat LLM output as unverified input. Validate against a source of truth and derive what you can deterministically. We also avoid letting the LLM specify fields we can derive deterministically." This is the canonical statement of the deterministic-first principle for LLM code review tools.
* **Sourcegraph: Automated Code Review Tools ([Sourcegraph Blog](https://sourcegraph.com/blog/automated-code-review-tools))**: "Rule-based static analysis... output is deterministic, which makes these tools the right substrate for compliance gates. AI code review... is more variable and harder to use as a hard compliance gate." Confirms that deterministic tools should be the gate, with AI as a supplementary layer.
* **False positive incident: PR #68**: The Syntax/Lint judge reported an `IndentationError` in `scripts/telemetry.py` that did not exist. `py_compile`, `ruff`, `mypy`, and 222 tests all passed. The LLM had misread diff whitespace — a blank line between `api_key = ...` and `headers = {}` created an illusion of inconsistent indentation.
* **ADR-0022: Enclosing Function Context Enrichment**: Established the `=== CONTEXT BLOCK ===` prompt-augmentation pattern that this ADR follows. The architecture judge already receives `=== REPOSITORY ARCHITECTURE CONTEXT ===`; the syntax judge now receives `=== DETERMINISTIC SYNTAX VERIFICATION ===`.
