# ADR 0014: PR Verification and LLM Judge Review Integration

* **Status**: Accepted
* **Date**: 2026-06-27
* **Deciders**: Luis Arteaga & Antigravity

## Context and Problem Statement
Once codebase modifications successfully pass the local verification phase (`Verify-Node`), the orchestrator creates a Pull Request (PR). The PR is then analyzed by an automated "LLM-as-a-Judge" review system (`scripts/review.py`) for security vulnerabilities and architectural compliance.

Checking the raw GitHub merge status alone is insufficient because a PR could be merged prematurely despite failing or pending LLM reviews. Furthermore, since multiple review cycles can occur over a PR's lifetime, the orchestrator must only evaluate reviews that apply to the latest commit (commit-timestamp review validation) to avoid blocks from stale reviews.

## Decision Drivers
* **Security Assurance**: No PR with critical security issues should be merged.
* **Architecture Compliance**: Unconditional blocking on architecture compliance failures to prevent merging invalid code/scaffolding ("kauderwelsch").
* **Timeliness and Freshness**: Stale reviews (those completed before the latest push) must be ignored.

## Considered Options
* **Option 1: Simple PR Merge Status Polling**
  The orchestrator waits solely for the PR to reach the `merged` state (e.g. via manual click or automated hooks).
* **Option 2: Review Polling with Commit-Timestamp Alignment**
  The orchestrator polls PR reviews and compares each review's `submitted_at` timestamp with the committer date of the latest commit (`git show -s --format=%cI HEAD`). Only reviews submitted after or at the latest commit timestamp are evaluated.

## Decision
We chose **Option 2**.

The `Merge-Node` polls the PR reviews via the GitHub REST API. By matching review timestamps with the HEAD commit time, we guarantee that only reviews validating the latest codebase state are evaluated. 

To prevent merging invalid code structures, **Architecture Compliance failures are treated as critical, first-class failures** (just like security vulnerabilities). A verdict of `FAIL` or `NEEDS REVIEW` from the latest Security OR Architecture Compliance review unconditionally blocks the merge and triggers a transition to `Recovery`. No environment variables or bypass flags are permitted.

### Consequences
* **Pros**:
  * **Strong Guarantees**: Security and architecture compliance failures are detected dynamically, preventing invalid merges and keeping the main branch clean of formatting or convention drift.
  * **Resilient Resumption**: Pushing fixes automatically invalidates older review findings on the next cycle, since their timestamps precede the new commit time.
* **Cons**:
  * **API Overhead**: Polling reviews requires periodic GitHub API queries (mitigated by a 10s poll interval).

## Inspiration & References
* **GitHub Branch Protection Rules ([GitHub Docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches))**: Standard branch protection configurations allow dismissing stale PR approvals when new commits are pushed. Our timestamp check replicts this pattern in-process for automated agent feedback.
* **Kubernetes Prow/Munch bot ([Prow Architecture](https://github.com/kubernetes/test-infra/tree/master/prow))**: Prow enforces fine-grained status checks and review requirements, matching current git references to check suites.
