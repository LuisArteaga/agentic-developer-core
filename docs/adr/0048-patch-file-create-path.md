# ADR 0048: patch_file File-Creation Path (Empty old_string)

## Status
Accepted (extends [ADR-0006](./0006-block-based-patching-with-read-before-edit.md) and [ADR-0033](./0033-line-range-scoped-read-before-edit.md); addresses [issue #129](https://github.com/LuisArteaga/agentic-developer-core/issues/129))

## Context
The Worker previously had no legitimate tool path to **create a new file**. `patch_file` enforced the Read-Before-Edit constraint universally: a file that does not exist cannot be read, so `patch_file` rejected it, and `read_file` errored on the non-existent path. The Worker fell back to `run_command` with `python -c "...heredoc..."` to write content — token-expensive, brittle (shell-quoting `SyntaxError`s observed in traces), and a budget sink that exhausted the recursion budget ([ADR-0045](./0045-configurable-recursion-budget-and-graceful-exhaustion.md)) before any test file was written (issue #129 root-cause analysis, target-repo run 2026-08-14).

The Read-Before-Edit Constraint ([ADR-0006](./0006-block-based-patching-with-read-before-edit.md), refined by [ADR-0033](./0033-line-range-scoped-read-before-edit.md)) is described as a *universal* safety rule. File creation is a deliberate, narrow **exemption**: there is no prior content to read, so the constraint is meaningless for a genuinely new file. The exemption must apply *only* when the file does not exist, and must not weaken any other guard.

### Industry baseline
Anthropic's `str_replace_editor` text editor tool exposes a dedicated **`create`** command (parameters `path` + `file_text`), distinct from `str_replace`, that creates a new file with provided content. OpenHands mirrors the same `view`/`create`/`str_replace`/`insert` family. The established norm is: a first-class create command, separate from edit, that errors on existing files and reuses the same path-safety surface as edits. ([Anthropic Text editor tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool); [Simon Willison summary, 2025-03-13](https://simonwillison.net/2025/Mar/13/anthropic-api-text-editor-tool))

## Decision
Extend `patch_file` with an explicit **create path** triggered by an **empty `old_string`** (the full file content is passed as `new_string`). This adapts Anthropic's `create` command to our flat `(path, old_string, new_string)` tool signature, preserving the single-tool surface (compatible with [ADR-0011](./0011-prebuilt-langgraph-react-agent-for-worker.md) — `get_worker_tools` stays 7 tools) and the loop-detection middleware ([ADR-0046](./0046-worker-tool-loop-detection-middleware.md), which keys on full canonicalized arguments, so varied creates are not flagged and a stuck repeated create→fail is caught at the hard limit).

Inside `patch_file`, the create branch runs **after** Path Safety Validation and **before** the Read-Before-Edit gate:

1. **Path Safety Validation runs first** ([ADR-0035](./0035-runtime-path-safety-validation-in-worker-tools.md)). Blocked paths (`.env`, `.git/…`, credentials, certificate extensions, VCS/dependency/agent-state dirs) fail closed on create exactly as on edit. No filesystem mutation occurs.
2. **Empty `old_string` + non-existent file → create.** Parent directories are created automatically (`parents=True, exist_ok=True`, `mkdir -p` semantics). Every path component was already validated against the blocklist, so tree creation cannot reach a sensitive directory. The content is written atomically via a single `open(..., "w")`.
3. **Empty `old_string` + existing file → fail (no silent overwrite).** The error directs the Worker to `read_file` then `patch_file` with a non-empty `old_string` (the str_replace path).
4. **Creation is exempt from Read-Before-Edit** (it short-circuits before the `read_files` gate) because there is no prior content to read. The exemption is structurally bounded to the non-existent-file case by step 3.
5. **Creation does NOT register `read_files` membership.** A subsequent str_replace on the freshly-created file still requires a `read_file` first — the Read-Before-Edit invariant ([ADR-0006](./0006-block-based-patching-with-read-before-edit.md)/[ADR-0033](./0033-line-range-scoped-read-before-edit.md)) applies uniformly; the create is not a read.
6. **Empty content is allowed** (e.g. `__init__.py`). Rejecting it would push the Worker back to `run_command` workarounds for a legitimate empty-file case.
7. **Path traversal / absolute out-of-root paths** are rejected identically by `_normalize_path` + `is_safe_path`.

The Worker system prompt documents the create path as a first-class rule, and no longer implies heredoc workarounds are needed for new files.

## Consequences

### Pros
- **Eliminates the brittle heredoc workaround.** New files are created in a single, atomic, path-safety-validated tool call — collapsing the create→read→patch-empty retry chains that burned the recursion budget.
- **No new tool, no schema change.** The empty-`old_string` convention keeps the tool surface and JSON schema unchanged, so the prebuilt ReAct agent ([ADR-0011](./0011-prebuilt-langgraph-react-agent-for-worker.md)) and loop detection ([ADR-0046](./0046-worker-tool-loop-detection-middleware.md)) integrate without modification.
- **Defense-in-depth preserved.** Path Safety Validation runs identically on create; the Read-Before-Edit invariant remains universal for *existing* files; no overwrite of existing files.

### Cons
- **Implicit create signal.** An empty `old_string` is a convention, not a dedicated parameter; an LLM that mistakenly passes an empty `old_string` for an *edit* gets a clear, safe error ("already exists — read + patch instead") rather than a destructive outcome, but the convention must be taught via the docstring/system prompt.
- **Parent-directory creation is a side effect** beyond the explicit path. It is safe (components are pre-validated) but is a broader filesystem mutation than a single-file create.

## Considered Options

### 1. Dedicated `command` / `mode` parameter (e.g. `command="create"`, `file_text`)
Mirrors Anthropic's `str_replace_editor` `create` command most directly and is the most self-documenting (the JSON schema would expose the modes). **Rejected** because it changes the tool signature/schema (touching the [ADR-0011](./0011-prebuilt-langgraph-react-agent-for-worker.md) tool registration and existing call expectations) while offering no safety advantage over the empty-`old_string` convention once the docstring is explicit and the existing-file case fails safely. Adopted instead as the *inspiration* for the semantics (distinct create vs. edit, error on existing file, identical path safety).

### 2. A separate `create_file` tool
A dedicated LangChain tool. **Rejected**: it grows the tool count (breaking the 7-tool `get_worker_tools` contract and tests) and duplicates the path-safety/normalization plumbing; the flat-signature overload reuses all existing guards with zero duplication.

### 3. Keep Read-Before-Edit universal (no create path)
**Rejected** — this is the status quo that produces the heredoc workaround and budget exhaustion documented in issue #129. The constraint is meaningless for a non-existent file, so enforcing it there is pure friction with no safety benefit.

### 4. Create registers `read_files` membership (create counts as a read)
**Rejected** — the Worker just authored the content, so authorizing edits without a fresh read would re-open the stale-edit gap [ADR-0033](./0033-line-range-scoped-read-before-edit.md) exists to close. The issue explicitly requires the create to not count as a read, preserving the invariant uniformly.

## Inspiration & References
- [Anthropic — Text editor tool (`create` command)](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool) — T1 (Authoritative Primary). The `create` command (`path` + `file_text`), distinct from `str_replace`, that errors on existing files: the semantics this ADR adapts to our flat signature. Verified live 2026-07.
- [Simon Willison, "Anthropic API: Text editor tool", 2025-03-13](https://simonwillison.net/2025/Mar/13/anthropic-api-text-editor-tool) — T3 (Community). Summarizes the `view`/`str_replace`/`create`/`insert` command family. Verified live 2026-07.
- [ADR-0006: Block-Based Patching with Read-Before-Edit](./0006-block-based-patching-with-read-before-edit.md) — the universal constraint whose narrow, well-defined exemption this introduces.
- [ADR-0033: Line-Range-Scoped Read-Before-Edit](./0033-line-range-scoped-read-before-edit.md) — the range-scoped refinement; preserved unchanged for existing files.
- [ADR-0035: Runtime Path-Safety Validation in Worker Tools](./0035-runtime-path-safety-validation-in-worker-tools.md) — the runtime blocklist enforced identically on the create path.
- [ADR-0046: Worker Tool Loop Detection Middleware](./0046-worker-tool-loop-detection-middleware.md) — confirms the create path integrates with loop detection without a new repeated-call pattern.
