# Product Requirement Document (PRD) — Stateful Hybrid Orchestrator

## Project: agentic-dev-template — ADR-0005 Migration

> **Scope of this PRD.** This document specifies the migration of the autonomous
> development loop from the Bash entrypoint + direct Claude Code CLI execution to
> a **stateful Python LangGraph orchestrator** that invokes the Claude Code CLI as
> a specialised Worker subagent. It refines and operationalises
> [ADR-0005](docs/adr/0005-hybrid-langgraph-orchestrator-with-claude-code-worker.md).
> It is the product spec for that change only; the base system is described in the
> root [`PRD.md`](PRD.md).
>
> This PRD is the output of a `/grill-with-docs` stress-test session. Decisions
> marked **DECIDED** were resolved during that session. Decisions marked **OPEN**
> still require human judgement before issue elaboration.

---

## 1. Objective & High-Level Summary

Today the orchestration loop lives entirely in `scripts/entrypoint.sh`, which runs
a single headless `claude -p` invocation per cycle. That invocation, driven by the
`.claude/CLAUDE.md` system prompt, performs the whole issue lifecycle (Phases 0–5:
Poll/Claim → Plan → Execute → Verify → PR → Merge). This has three structural
weaknesses (per ADR-0005):

1. **No loop control.** If the Claude CLI crashes on an unparsable tool call
   (upstream provider formatting drift over OpenRouter), the entire container
   cycle aborts and all in-flight context is lost.
2. **No session persistence.** A failed run restarts the issue lifecycle from
   scratch; there is no resume.
3. **No dynamic model control.** Falling back to a reasoning model on stubborn
   test failures cannot be orchestrated from inside the single CLI session.

**Objective:** introduce a **LangGraph stateful orchestrator** in Python that owns
the macro lifecycle (claim, plan, verify, PR, merge, recovery, GitHub API,
persistence) and invokes the Claude Code CLI **only as the Worker** (the Execute
phase). State is continuously persisted to `.agent_logs/state.json` so that a
crashed container can resume in-flight work rather than discarding it.

**Non-goal:** rebuilding Claude Code's file-editing, search-and-replace, and
test-driving capabilities in Python. Those remain inside the CLI Worker.

---

## 2. Architectural Decisions (resolved in grill)

These five decisions are **DECIDED** and frame all requirements below.

### 2.1 LangGraph scope — Deep rewrite *(DECIDED)*

LangGraph owns the macro orchestration as Python nodes:
**Poll/Claim, Plan, Verify, PR, Merge, Recovery**, plus all GitHub API calls,
budget pre-flight, and state persistence. The `claude` CLI is invoked **only** for
the **Execute** phase (the Worker). This matches the ADR-0005 node diagram
literally, rather than a thin retry shell.

> **Implication:** the orchestration logic in `.claude/CLAUDE.md` (Phases 0–5) is
> reimplemented in `orchestrator/nodes.py`. See §2.5 for what happens to the prompt.

### 2.2 Bash = thin supervisor; Python owns the loop *(DECIDED)*

`scripts/entrypoint.sh` is **not** removed. It is reduced to a **thin supervisor**:

- Required env-var checks (`OPENROUTER_API_KEY`, `GH_PAT`).
- OpenRouter / GitHub reachability checks.
- Log-file `touch` + background `tail -f` stream + signal `trap`.
- A `while true` restart loop that invokes `python -m orchestrator` **once per
  iteration** (one issue), inspects its exit code, and applies backoff —
  mirroring today's "launcher restarts the process" contract.

The **git reset/clean** and **`rm .claude/projects`** steps are **removed from
bash** and move into Python (see §2.4). This honours ADR-0002's surviving
rationale — bash remains the process/signal/launcher layer it is good at — while
the stateful logic moves to Python.

### 2.3 True work resume — gated and hedged *(DECIDED)*

`state.json` records the in-flight issue, phase, attempts, branch, and Claude
`session_id`. On container restart, if `state.json` shows an in-flight issue, the
orchestrator **resumes** rather than restarting: it skips the workspace wipe,
checks out the existing feature branch, resumes the Claude session, and re-enters
at the recorded phase. (Contrast: "issue-level resume" — re-claim and redo from
scratch — preserves no WIP.)

**True resume is the target, but it is gated and hedged**, because two facts were
verified against the official Claude Code docs (2026-06-20) that make it risky:

1. **The session transcript is purely client-side** at
   `/home/claude/.claude/projects/<encoded-cwd>/<session-id>.jsonl` and is **not
   mounted** — `docker-compose` only mounts `.agent_logs`. A container crash
   destroys the exact file resume needs. → **A new persistent mount for
   `/home/claude/.claude/projects` is required** (§3.3).
2. **Resume over OpenRouter with a non-Anthropic model** (Kimi/DeepSeek via
   `ANTHROPIC_BASE_URL`) is **undocumented (~40% confidence)**. Client-side replay
   may work, but tool-call/format handling on an OpenAI-compatible endpoint is
   where it could silently break.

Therefore the rollout is hedged:

- **Validation spike first** (§5 AC5): prove `claude --resume <id> -p` rehydrates
  context headlessly over OpenRouter *before* relying on it.
- **`RESUME_MODE` flag** (`true` | `issue`): `true` = true work resume; `issue` =
  **issue-level resume** (detect the orphaned `in-progress` issue, reset it to
  `ready-for-agent`, redo on a fresh branch). Issue-level resume always works and
  survives the hard-reset; it is the documented fallback if the spike fails.
- **Graceful degradation at runtime:** if `--resume` errors or the transcript file
  is missing, the resume path falls back to issue-level redo for that issue.

### 2.4 Python claim-node owns conditional workspace hygiene *(DECIDED)*

Workspace hygiene (`git reset --hard origin/main`, `git clean -fd`,
`rm -rf /home/claude/.claude/projects/`) moves out of bash entirely and into the
Python **claim/poll node**, where it becomes **conditional**:

- **Fresh issue** → wipe, then claim and plan.
- **Resuming an in-flight issue** → **skip the wipe**, preserve branch + session.

This resolves the sequencing problem: the wipe decision depends on `state.json`,
which only Python reads — so bash must not perform the wipe before Python starts.

### 2.5 Fate of `.claude/CLAUDE.md` — becomes the canonical spec *(DECIDED)*

`CLAUDE.md` stops being the runtime `--system-prompt-file`. It is **retained as the
canonical Orchestrator behaviour specification** (same rules, same vocabulary).
`orchestrator/nodes.py` implements that spec. The CLI Worker receives
`.claude/WORKER.md` as its prompt.

- The ~17 phrase assertions in `scripts/test.sh` that grep `CLAUDE.md` stay green
  because the file still exists and still carries the canonical terms.
- **Accepted risk:** spec (`CLAUDE.md`) and implementation (`nodes.py`) can drift.
  Mitigation: the Verify phase and code review treat `CLAUDE.md` as the source of
  truth the Python code must match.

---

## 3. Component Breakdown

### 3.1 The `orchestrator/` Python package

```
orchestrator/
├── __main__.py   # `python -m orchestrator` entry; builds graph, loads state, runs one issue, sets exit code
├── graph.py      # LangGraph state machine: nodes, edges, conditional routing
├── state.py      # TypedDict state schema + atomic load/save to .agent_logs/state.json
├── nodes.py      # Node implementations: Claim, Plan, Execute(=invoke Worker), Verify, PR, Merge, Recovery
└── cli.py        # Claude CLI subprocess wrapper: run, stream logs, capture exit code, extract session_id
```

| Module | Responsibility |
|--------|----------------|
| `graph.py` | Defines nodes and the conditional edges between them (e.g. Verify → PR on pass, Verify → Recovery on fail, Recovery → Execute on retry / → reporter-handback after 3 cycles). Compiles the LangGraph workflow. |
| `state.py` | Defines the `TypedDict` state (§4). Provides `load()` / `save()` with **atomic write** (temp file + `os.replace`) so a crash mid-write cannot corrupt `state.json`. |
| `nodes.py` | One function per macro phase. Owns GitHub API calls (`gh`), budget pre-flight, atomic claim, conditional workspace hygiene (§2.4), test-skeleton planning, `make verify`, PR creation, merge polling, and crash recovery / label reset. Emits telemetry spans by importing the telemetry helpers directly (§3.4). |
| `cli.py` | Builds and runs the `claude` subprocess for the **Worker only**. **Pre-allocates the session id**: the orchestrator generates a UUID, writes it to `state.json` *before* the run, and passes `claude --session-id <uuid>` (verified-supported per §6); resume uses `claude --resume <uuid> -p` headless with `--dangerously-skip-permissions`. Streams stdout/stderr to `AGENT_LOG_PATH` and the console in real time without deadlock; captures the exit code; classifies exit conditions (tests-failed vs CLI-crash). |

### 3.2 `scripts/entrypoint.sh` (thin supervisor)

Retains: env checks, reachability checks, log `touch` + `tail -f` + `trap`, and the
`while true` → `python -m orchestrator` → exit-code/backoff loop. Removes: git
sync and `.claude/projects` deletion (moved to §2.4). Keeps the
`orchestrator_loop` / `orchestrator_phase_*` reference markers required by the test
suite (see §5, AC4).

### 3.3 Dockerfile dependencies

Add `langgraph` and `langchain-core` to the image. **Pin versions** (consistent
with the pinned `CLAUDE_CLI_VERSION`) via a `requirements.txt` rather than floating
installs, for reproducible builds. The image base stays `node:22-alpine` per
ADR-0001 / PRD §2.4.

> **Feasibility note (to validate during elaboration):** `langgraph` /
> `langchain-core` pull `pydantic v2` / `pydantic-core` (Rust). On Alpine (musl)
> this may require build dependencies (`build-base`, `cargo`, `gcc`,
> `musl-dev`, `libffi-dev`) or musl wheels, and increases image size. Confirm the
> Python version shipped by `apk python3` satisfies the chosen `langgraph` floor,
> and confirm no version conflict with the pinned `opentelemetry` stack.

### 3.3a Persistent session mount (required for true resume) *(DECIDED — OPEN-2)*

True work resume (§2.3) requires the Claude session transcript to survive a
container crash. The transcript lives **inside the container** at
`/home/claude/.claude/projects/`, which `docker-compose.yml` does **not** currently
mount. This migration **adds a new persistent mount/volume for
`/home/claude/.claude/projects`** so the `<session-id>.jsonl` that `--resume`
replays is not lost when the container dies.

- Scope this mount carefully against existing behaviour: today the orchestrator
  **deletes** `.claude/projects` for session isolation (moved into the Python
  claim node per §2.4). With the mount, that deletion still runs on the **fresh**
  path; it is **skipped** on the **resume** path. The mount persists the file
  *between* container lifetimes; the claim node controls *when* it is wiped.
- For `cloud`/`ci` modes, the persistent target follows the same per-mode storage
  convention as `.agent_logs` (host-mounted local; file share in cloud).
- If `RESUME_MODE=issue`, the mount is **not required** — issue-level resume keeps
  no session state.

### 3.4 Telemetry integration

Today telemetry spans are driven by `python3 scripts/orchestrator_telemetry.py
{start,end}-{loop,phase}` subprocess calls from shell. With the orchestrator now in
Python, `nodes.py` **imports** the span helpers (`start_orchestrator_loop`,
`start_orchestrator_phase`, etc.) from `scripts/telemetry.py` directly, giving
proper in-process parent/child span nesting instead of process-boundary spans. The
`orchestrator_loop` and `orchestrator_phase_plan|execute|verify` markers must still
be discoverable by the test suite (§5).

**Transcript export and `sync_telemetry.py` stay in the bash supervisor**
*(DECIDED — OPEN-4)*: both remain in the supervisor's `trap EXIT/INT/TERM` block.
This preserves the EXIT-trap guarantee and survives a hard Python crash — if the
Python orchestrator dies mid-run, the supervisor still exports the transcript and
flushes telemetry. Only the span *emission* (`start/end_orchestrator_phase`) moves
into `nodes.py` as in-process imports; the *export and upload* stay bash-side.

---

## 4. State Schema (`.agent_logs/state.json`)

Continuously persisted; written atomically (temp + `os.replace`).

| Field | Type | Purpose |
|-------|------|---------|
| `issue_number` | `int \| null` | The claimed issue, or `null` when idle. |
| `status` | `enum` | `idle \| claimed \| planning \| executing \| verifying \| pr_open \| merging \| done \| failed`. |
| `phase` | `str` | Current macro phase, for re-entry on resume. |
| `attempts` | `dict` | Per-phase attempt counts (Verify/Merge revision cycles, max 3). |
| `branch` | `str \| null` | Feature branch name (`[type]/issue-[ID]-[slug]`) for resume checkout. |
| `session_id` | `str \| null` | **Pre-allocated** Claude CLI session UUID (generated before the run, passed via `--session-id`, replayed via `--resume`). See §3.1, OPEN-3. |
| `model` | `str` | Active model, fixed to `moonshot/kimi-2.7` today. **Forward hook for the deferred DeepSeek fallback** (OPEN-1) — present in state but never switched; Recovery only retries. |
| `last_exit_code` | `int \| null` | Exit code of the last Worker/CLI invocation, for recovery routing. |
| `updated_at` | `str` | ISO-8601 timestamp of last write. |

**Restart sequence (claim/poll node):**

1. `load()` state.
2. If an issue is **in-flight** (`status` not `idle`/`done`/`failed`):
   - **`RESUME_MODE=true`** → resume path: **skip wipe**, `git checkout <branch>`,
     `claude --resume <session_id>`, re-enter at `phase`. If `--resume` errors or
     the transcript file is missing → **fall back to issue-level redo** for this
     issue.
   - **`RESUME_MODE=issue`** → issue-level path: reset the orphaned `in-progress`
     issue back to `ready-for-agent`, then treat as fresh.
3. Else: fresh path → wipe (git reset/clean, rm `.claude/projects`), poll +
   budget pre-flight + atomic claim, then Plan.

**Failure modes to handle:** corrupt/partial `state.json` (atomic write + validate
on load, fall back to fresh on parse error); branch was force-pushed/merged
(detect, fall back to fresh); `session_id` no longer resumable (fall back to
issue-level redo); in-flight issue was closed/merged by a human meanwhile (detect,
mark done, go idle); two containers racing on the same state file (single-state-per-
container assumption — document it).

**Crash-recovery (label reset):** the existing bash logic that, on non-zero exit,
resets a still-`in-progress` issue back to `ready-for-agent` moves into the Python
Recovery node / finalisation, driven by `state.json` rather than the
`.issue_processed` marker.

---

## 5. Acceptance Criteria

From ADR-0005, refined by the grill:

- **AC1.** The orchestrator loop runs as a stateful LangGraph workflow in Python
  (`orchestrator/` package), invoked once per issue by the thin bash supervisor.
- **AC2.** State is continuously persisted to `.agent_logs/state.json` (atomic
  write) to allow resume after a container crash. Resume mode is governed by
  `RESUME_MODE` (`true` = true work resume; `issue` = issue-level redo), with
  runtime graceful degradation to issue-level if `--resume` fails (§2.3, §4).
- **AC3.** Claude CLI execution is encapsulated in a robust Python subprocess
  wrapper (`orchestrator/cli.py`) that streams logs in real time, captures the exit
  code, and **pre-allocates the session id via `--session-id`** (§3.1).
- **AC4.** All existing tests in `scripts/test.sh` (smoke tests and entrypoint
  checks) **remain compatible and pass.** This constrains the design as follows
  (the central risk of this migration):
  - The bash supervisor **keeps** the `while` loop, `touch`/`tail -f`/`trap`, and
    the `orchestrator_loop` + `orchestrator_phase_plan|execute|verify` reference
    markers — so the `#34`, `#56` greps stay green.
  - `.claude/CLAUDE.md` is **retained** with its canonical phrases — so the ~17
    `CLAUDE.md` phrase greps and the `#44`/`#31` greps stay green (§2.5).
  - The `#48`/`#46` greps for git-sync and `.claude/projects` deletion: since that
    logic moves into Python, these assertions must be **re-pointed** at the new
    location (or the markers kept discoverably) — flagged as the AC4 hot-spot.
  - The `#32` block **executes** `entrypoint.sh` with a mocked `claude` and asserts
    exit codes via the `.issue_processed` marker. Because the inner call becomes
    `python -m orchestrator`, the mock must be adjusted (mock `python`/the
    orchestrator boundary) so the supervisor's exit-code/backoff contract is still
    verified. This is the one block that cannot stay byte-identical.

> **AC4 interpretation (DECIDED):** "remain compatible" = the *behaviour coverage*
> stays green. Where an assertion targets bash internals that genuinely moved to
> Python, it is re-pointed at the new implementation rather than deleted. No
> coverage is dropped silently.

- **AC5.** A **resume-validation spike** proves `claude --resume <session_id> -p`
  rehydrates context headlessly over OpenRouter (Kimi/DeepSeek via
  `ANTHROPIC_BASE_URL`) **before** true resume is relied upon. If the spike fails,
  the default ships as `RESUME_MODE=issue` (§2.3, OPEN-2).
- **AC6.** A **persistent mount for `/home/claude/.claude/projects`** is added so
  the session transcript survives a container crash; the fresh-path wipe still runs,
  the resume-path wipe is skipped (§3.3a). Not required when `RESUME_MODE=issue`.
- **AC7.** New runtime dependencies (`langgraph`, `langchain-core`) are **pinned**
  in a `requirements.txt` and install on `node:22-alpine`, with any required musl
  build deps added to the Dockerfile (§3.3).

---

## 6. Resolved Decisions (closed in grill)

All four previously-open questions are now **DECIDED**. They are recorded here with
their rationale; their effects are folded into §2–§5 above.

- **OPEN-1 — Model fallback vs ADR-0001 → DEFER, keep ADR-0001 intact.** ADR-0005
  motivates the rewrite partly with a *dynamic DeepSeek fallback* on stubborn test
  failures, but ADR-0001 mandates a single-model runtime (Kimi 2.7 only, one
  session, no raw-API subagents). **Decision:** ship single-model; the Recovery node
  only retries (no model switch). `state.json` carries a `model` field as a marked
  **forward hook** (§4). The DeepSeek fallback becomes a follow-up ADR + issue when
  needed. *Why:* the fallback is not required for AC1–AC4, and building it would
  supersede a merged ADR and add two-model/provider mechanics to `cli.py`. Keeps
  the migration focused.

- **OPEN-2 — `claude --resume` validation → SPIKE + MOUNT + `RESUME_MODE` flag.**
  Verified facts (Claude Code docs, 2026-06-20): the transcript is purely
  client-side and **not** mounted (crash destroys it), and resume over OpenRouter
  with a non-Anthropic model is **undocumented (~40% confidence)**. **Decision:**
  (a) run a validation spike (AC5) before relying on true resume; (b) add a
  persistent mount for `/home/claude/.claude/projects` (AC6, §3.3a); (c) gate
  behaviour behind `RESUME_MODE` (`true` | `issue`) with issue-level redo as the
  reliable fallback (§2.3). *Why:* true resume is the goal but cannot be trusted
  blind; issue-level resume always works and survives the hard-reset.

- **OPEN-3 — `session_id` source → PRE-ALLOCATE via `--session-id`.** **Decision:**
  the orchestrator generates a UUID, writes it to `state.json` *before* the run, and
  passes `claude --session-id <uuid>`; resume replays it with `--resume`. *Why:*
  verified-supported, deterministic, and avoids fragile post-hoc parsing of JSONL
  filenames or stdout (§3.1).

- **OPEN-4 — Transcript export & telemetry sync placement → STAY IN BASH.**
  **Decision:** the transcript `cp` and `sync_telemetry.py` remain in the
  supervisor's `trap EXIT/INT/TERM`; only span emission moves into `nodes.py` as
  in-process imports (§3.4). *Why:* the EXIT-trap fires even on a hard Python crash,
  preserving the export/flush guarantee.

---

## 7. ADR Impact

| ADR | Action | Rationale |
|-----|--------|-----------|
| ADR-0001 (single-model) | **None (kept intact)** | OPEN-1 resolved to defer the model fallback. ADR-0001 stays valid; `state.model` is only a forward hook. A future ADR supersedes it *if/when* the fallback is built. |
| ADR-0002 (keep Bash) | **Amend** | Bash is no longer the orchestrator, but survives as the thin supervisor/launcher (its process/signal/mock rationale still holds). Record the narrowed scope. |
| ADR-0003 (Claude Code over OpenCode) | **None** | Claude Code remains the Worker runtime; choosing it over OpenCode is unaffected. Note the new tension: Python now owns the git wipe, so `/rewind` safety is no longer the only rollback path. |
| ADR-0004 (hybrid telemetry) | **None** | Span *emission* moves from subprocess to in-process imports; *export/upload* stay bash-side (OPEN-4); ingestion model unchanged. |
| ADR-0005 (this migration) | **Amend, move to Accepted** | Reflect "bash thin supervisor" (not full bash removal), "true work resume gated on the AC5 spike + `RESUME_MODE` flag", and the single-model decision (no DeepSeek fallback yet). |
| **New ADR** (persistent session mount) | **Add-new** *(candidate)* | Adding a persistent volume for `/home/claude/.claude/projects` to enable crash-surviving session resume is a non-obvious, hard-to-reverse infra decision with a real trade-off (session isolation vs. resumability). Worth its own ADR if AC5 confirms true resume. |

---

## 8. Out of Scope

- DeepSeek / multi-model fallback implementation — **deferred** (OPEN-1); only the
  `state.model` forward hook ships now.
- Any change to the issue pipeline, `/to-issues`, CI review agent, or the Worker's
  Lazy constraints.
- Rebuilding Claude Code's editing/test-driving features in Python.

---

## 9. Implementation Sequencing (suggested)

A dependency-ordered path for issue elaboration (not a commitment to scope each as
one issue):

1. **AC5 resume spike** — validate `claude --resume` over OpenRouter. Its outcome
   sets the default `RESUME_MODE` and decides whether AC6 (mount) is needed.
2. **`orchestrator/` skeleton** — `state.py` (schema + atomic load/save) and
   `cli.py` (subprocess wrapper, `--session-id` pre-allocation, log streaming).
3. **`graph.py` + `nodes.py`** — port Phases 0–5 from the `CLAUDE.md` spec; wire
   conditional workspace hygiene (§2.4) and telemetry imports (§3.4).
4. **`entrypoint.sh` reduction** — thin supervisor; keep the test-visible markers
   (§5 AC4); move git-sync + project-deletion out.
5. **Dockerfile / compose** — pinned deps (AC7) + persistent session mount (AC6).
6. **`scripts/test.sh` reconciliation** — re-point the `#32`/`#46`/`#48` assertions
   per the AC4 interpretation; confirm all other greps stay green.
7. **ADR updates** — amend ADR-0002 + ADR-0005; add the mount ADR if AC5 passes.
