# ADR 0018: Flat Factory.json Schema over Grouped Node Taxonomy

## Status
Accepted

## Context
The orchestrator requires a per-node model routing configuration: each orchestrator node (`plan`, `test_writer`, `execute`), worker tool (`web_search`, `url_fetch`, `bin_eval`), and PR judge (`syntax_lint`, `test_coverage`, `architecture`, `security`) needs its own LLM model, routing list, temperature, and optional parameters. A central config file (`config/factory.json`) and a resolver function (`resolve_model_config()`) must map node names to their model settings at runtime, with environment-variable override precedence.

Two schema shapes were considered for organizing ~10–15 node entries:
1. **Grouped schema**: Top-level group buckets (`orchestrator_nodes`, `pr_judges`, `worker_tools`) each containing node-keyed entries.
2. **Flat schema**: Node names as direct top-level keys, no grouping.

## Decision
We adopt a **flat top-level schema** — node names are direct top-level keys, not nested under group buckets:

```json
{
  "plan":        {"model": "...", "routing": [...], "temperature": 0.0},
  "test_writer": {"model": "...", "routing": [...], "temperature": 0.0},
  "execute":     {"model": "...", "routing": [...], "temperature": 0.0, "options": {"thinking": "max"}},
  "bin_eval":    {"model": "...", ...},
  "web_search":  {"model": "...", ...},
  "syntax_lint": {"model": "...", ...}
}
```

`resolve_model_config(node_name)` performs a single dict lookup on the top-level key. No group taxonomy is encoded in the resolver.

## Rationale
A flat schema yields three concrete benefits:

1. **Single dict lookup** — `resolve_model_config("plan")` is `factory.get("plan")`. No `NODE_GROUP` map, no `if node in [...]` branching. Adding a future node (e.g., `bin_eval`, `url_fetch`) requires **zero changes** to the resolver.
2. **No taxonomy coupling** — the resolver's job is to resolve a node name to a Model Config, not to know the node's architectural family. A grouped schema forces the resolver to carry a lookup table mapping every node name to its bucket; adding a node means updating both the JSON *and* the resolver's group map.
3. **Human grouping via ordering and comments, not schema** — a flat file of ~10–15 entries remains readable. Comments in the file (e.g., `# --- Orchestrator Nodes ---`, `# --- PR Judges ---`) separate families for humans without enforcing schema nesting the resolver must traverse.

## Consequences
- Future nodes are added by appending a top-level key and nothing else. This lowers the cost of the cross-cutting concerns named in issue #36 ("all subsequent issues (#37 Web Search, #38 BinEval, #39 URL Fetch, PR Judges) depend on this config system").
- If a future requirement emerges to load a *subset* of nodes conditionally (e.g., only PR-judge nodes when running in CI-review mode), a flat schema requires filtering by node-name prefix or an external list, rather than by group key. This is a minor cost and deferred until that requirement exists.

## Rejected Alternatives

### 1. Grouped schema
Top-level groups (`orchestrator_nodes`, `pr_judges`, `worker_tools`) each containing node-keyed entries.
* **Why rejected**: Forces `resolve_model_config` to carry a `NODE_GROUP` lookup table mapping every node name to its bucket. Adding a node means updating both the JSON *and* the resolver's group map. The grouping serves human readability (already solved by file ordering and comments) at the cost of resolver complexity.

## Inspiration & References
- **ESLint Flat Config Migration** ([ESLint Blog](https://eslint.org/blog/2022/08/new-config-system-part-2)): ESLint migrated from a hierarchical `.eslintrc` with nested `overrides` to a flat array-of-blocks config, citing simpler reasoning, composition, and resolution without the consumer needing to know family/group hierarchies. The same principle applies: a flat schema decouples the resolver from structural knowledge it doesn't need.
