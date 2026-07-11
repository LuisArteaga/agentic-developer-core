# ADR 0018: Flat Factory.json Schema over Grouped Node Taxonomy

## Status
Accepted

## Context
Issue #36 instructs: "Port the proven pattern from agentic-planner-core's `config/factory.json` + `resolve_model_config()` — do not invent a new config format." The reference repo (`agentic-planner-core`) uses a *grouped* top-level schema, with nodes clustered under execution-context buckets: `cli_orchestration` (`grill`, `verify`, `draft`), `refine_graph_nodes` (`analyze_sources`, `web_search`, `propose_options`, `evaluate_grade`, `apply_decision`, `publish_issue`), and `ci_cd_pr_judges` (`syntax_lint`, `test_coverage`, `architecture`, `security`). The resolver must know which bucket each node belongs to via a branching `if phase_or_node in [...]` block — a `NODE_GROUP` lookup table living in resolver code.

This repository has a different node taxonomy: a small, flat set of orchestrator nodes (`plan`, `test_writer`, `execute`) plus a forward-looking family of worker tools (`web_search`, `url_fetch`, `bin_eval`) and PR judges (`syntax_lint`, `test_coverage`, `architecture`, `security`).

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
We are porting the *mechanism* — `resolve_model_config()` with environment-override precedence and the `{model, routing, temperature, options}` Model Config schema — not the *grouped JSON shape*. The grouping in `agentic-planner-core` is shaped by *that* repo's three distinct execution contexts (CLI orchestration, refine graph, CI/CD PR judges). Our repo's node taxonomy is different and smaller; forcing it into borrowed buckets would couple the resolver to a node-to-group mapping that adds indirection without value.

A flat schema yields three concrete benefits:

1. **Single dict lookup** — `resolve_model_config("plan")` is `factory.get("plan")`. No `NODE_GROUP` map, no `if node in [...]` branching. Adding a future node (e.g., `bin_eval`, `url_fetch`) requires **zero changes** to the resolver.
2. **No taxonomy coupling** — the resolver's job is to resolve a node name to a Model Config, not to know the node's architectural family. Planner-core's resolver must be updated whenever a node moves between groups or a new group is introduced; ours does not.
3. **Human grouping via ordering and comments, not schema** — a flat file of ~10-15 entries remains readable. Comments in the file (e.g., `# --- Orchestrator Nodes ---`, `# --- PR Judges ---`) separate families for humans without enforcing schema nesting the resolver must traverse.

## Consequences
- The `factory.json` file is not structurally compatible with `agentic-planner-core`'s `factory.json`. The two repos cannot share a config file by copy-paste. This is acceptable — the repos have different node taxonomies; a shared file would require remapping node names anyway.
- Future nodes are added by appending a top-level key and nothing else. This lowers the cost of the cross-cutting concerns named in issue #36 ("all subsequent issues (#37 Web Search, #38 BinEval, #39 URL Fetch, PR Judges) depend on this config system").
- If a future requirement emerges to load a *subset* of nodes conditionally (e.g., only PR-judge nodes when running in CI-review mode), a flat schema requires filtering by node-name prefix or an external list, rather than by group key. This is a minor cost and deferred until that requirement exists.

## Rejected Alternatives

### 1. Grouped schema (mirroring `agentic-planner-core`)
Top-level groups (`orchestrator_nodes`, `pr_judges`, `worker_tools`) each containing node-keyed entries.
* **Why rejected**: Forces `resolve_model_config` to carry a `NODE_GROUP` lookup table mapping every node name to its bucket. Adding a node means updating both the JSON *and* the resolver's group map. The grouping serves human readability (already solved by file ordering and comments) at the cost of resolver complexity. The "port the proven pattern" instruction refers to the resolution mechanism and the Model Config schema, not to the borrowed JSON nesting shaped by another repo's execution contexts.
