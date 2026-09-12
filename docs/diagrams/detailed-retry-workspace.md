# Diagram: Retry and workspace state

How the workspace is treated across retry attempts: incremental refinement
while budgets remain, a hard rollback of tracked files on the final attempt,
per-gate verify budgets, and the post-PR Merge-Fix Loop that reuses the same
Execute path. One diagram; rendered natively by GitHub.

```mermaid
flowchart TD
    ATT["Execute attempt starts<br/>derived attempt index =<br/>verify_cmd + bineval + 1<br/>Workspace Snapshot injected"] --> W["Worker edits the workspace"]
    W --> V["Verify<br/>deterministic gate, then BinEval soft gate"]

    V -->|"both gates pass"| PR["Pull Request creation<br/>commit and push"]

    V -->|"a gate failed"| G{"Which gate and budget?"}

    G -->|"verify_cmd or bineval<br/>below its cap of 3"| INC["Incremental refinement<br/>workspace kept intact<br/>Worker reads and patches prior edits<br/>Verification Feedback injected"]

    G -->|"derived attempt index at or above<br/>AGENT_RETRY_HARD_RESET_ATTEMPT<br/>default 3"| RESET["Hard rollback on the final attempt<br/>git reset --hard HEAD<br/>tracked files only"]

    INC --> ATT

    RESET -->|"untracked tests and stubs<br/>are preserved - TDD intact<br/>feedback still injected"| ATT

    G -->|"either gate reached its cap"| REC["Recovery<br/>workspace cleaned<br/>label back to agent-ready"]

    subgraph SNIP["Every attempt starts oriented"]
        SNAP["Workspace Snapshot<br/>directory tree + Structural Outlines<br/>of plan-localized target files<br/>registers no read_files membership<br/>hint, not ground truth"]
    end

    SNAP -.-> ATT

    subgraph POSTPR["Post-PR Merge-Fix Loop"]
        MF["Judge verdicts FAIL or NEEDS REVIEW<br/>with AGENT_PR_FIX_MAX budget left<br/>default 3"] --> MFEX["Execute receives judge findings<br/>via the same Verification Feedback channel<br/>verify counters reset to 0"]
        MFEX --> MFPR["PR node commits fix: and re-pushes<br/>fresh CI run + fresh judge review"]
        MFPR --> MFPOLL["Merge node re-polls<br/>anchored on the new pushed_at"]
        MFPOLL -->|"still actionable, budget left"| MF
        MFPOLL -->|"merged"| DONE["done"]
        MFPOLL -->|"budget exhausted"| ESC["Summary PR comment posted,<br/>then Recovery"]
    end

    PR --> MF
```

## Grounded in

- **ADR-0013** — Incremental Refinement: the workspace stays intact across
  verify failures; the Worker patches its prior edits.
- **ADR-0034** — Hybrid Retry: hard rollback (`git reset --hard`, tracked
  files only) from the threshold attempt onward; never `git clean` (the
  Test-Writer's untracked tests and stubs must survive); feedback is
  preserved across the reset (Reflexion).
- **ADR-0047** — split per-gate counters `attempts["verify_cmd"]` and
  `attempts["bineval"]`, each capped at 3; the derived execute attempt index
  = `verify_cmd + bineval + 1` drives the reset threshold.
- **ADR-0053** — Workspace Snapshot at attempt start (tree + target-file
  outlines, bounded at 20,000 chars, no read registration).
- **ADR-0036** — closed post-PR judge-feedback loop: `merge → execute` edge,
  `fix:` commit, per-cycle verify-budget reset, escalation comment at
  exhaustion.
- **Code** — `orchestrator/nodes.py` (`execute_node` reset logic,
  `verify_node`, `merge_node` merge-fix feedback, `_reset_verify_success`),
  `orchestrator/snapshot.py`, `orchestrator/state.py` (`attempts`).

## Notes

- Configurable values shown: `AGENT_RETRY_HARD_RESET_ATTEMPT` (default 3),
  per-gate verify caps (`VERIFY_MAX_ATTEMPTS`, default 3 each),
  `AGENT_PR_FIX_MAX` (default 3), `AGENT_SNAPSHOT_MAX_CHARS` (default 20,000).
- The hard reset reverts to the branch tip created by the Claim node; Worker
  created untracked files from earlier attempts persist into the final
  attempt (accepted residual — the Worker can read and fix or delete them).
- Crash safety: the reset decision derives from persisted `attempts`, and
  `git reset --hard HEAD` is idempotent, so a resumed run re-entering the
  threshold attempt re-runs it safely.
