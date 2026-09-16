# Runbook — Credential Scoping: Fine-Grained PAT Migration (FR-6)

With untrusted execution contained (Execution Sandbox, FR-1–FR-5), the
remaining blast radius of a compromised control plane is the orchestrator's
own credentials. ADR-0056's third protection goal — *unscoped GitHub access*:
PAT rights exceed the single Target Repository's needs — is closed by
migrating `GH_PAT` from a classic token (broad `repo` scope over **all** of
the owner's repositories) to a **fine-grained PAT scoped to the single
Target Repository**, and by regression-testing that no orchestrator secret
ever reaches the sandbox environment
(`orchestrator/test_sandbox.py::TestForbiddenSandboxEnvMatrix`).

Scope boundary: judges running inside GitHub CI authenticate with their own
Target-Repository secret (`JUDGE_GH_TOKEN`) — that credential is configured
per target repository and is **out of scope** here (issue #163 note).

## 1. Required permission matrix (fine-grained PAT)

Repository access: **"Only select repositories" → the single Target
Repository. Never "All repositories"** (that would restore the unscoped
blast radius this slice exists to close).

| Fine-grained permission | Level | Why the orchestrator needs it | Validated at startup |
| --- | --- | --- | --- |
| Metadata | Read | Resolve the Target Repository and its default branch (`orchestrator/nodes.py` repo polling, PR operations) | Yes — `GET /repos/{repo}` |
| Contents | Read and write | Workspace clone/fetch and branch pushes (`orchestrator/git.py`, control-plane PR-Node); head-SHA resolution | Read side yes; **write side operationally** (first cycle) |
| Checks | Read | Merge-Node judge check-run polling (`orchestrator/nodes.py::_fetch_check_runs`, ADR-0014) — missing access breaks merge gating **silently** | Yes — `GET /repos/{repo}/commits/{sha}/check-runs` |
| Issues | Read and write | Issue polling, claiming, label updates, comments | Read side yes; write side operationally |
| Pull requests | Read and write | PR creation (`POST /repos/{repo}/pulls`) and judge-review polling (ADR-0014) | Read side yes; write side operationally |
| Workflows | Read and write | Pushes touching `.github/workflows/*` (see §2) | **No API probe** — see §4 |

The startup preflight (`orchestrator/preflight.py::check_pat_scopes`) probes
the read side behaviorally: GitHub exposes no API to enumerate a fine-grained
PAT's granted permissions ([community discussion
#156115](https://github.com/orgs/community/discussions/156115)), so the
preflight issues the loop's own read requests and any refusal becomes a
fail-closed gap report naming the permission to grant. A 403 refusal also
carries the `X-Accepted-GitHub-Permissions` response header, which GitHub
populates with the exact permission the endpoint requires
([Troubleshooting the REST API](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api)).

## 2. The Workflows scope gotcha

Pushing a commit that modifies `.github/workflows/*` requires the
**Workflows** permission in addition to Contents write. Classic tokens fail
loudly with `refusing to allow ... without 'workflow' scope` when the token
lacks it; fine-grained tokens must have **Workflows: read and write**
granted for the same reason. This is why the matrix grants a *write*
permission for what looks like a read-only integration: the Worker may
legitimately push workflow files to the Target Repository
([community discussion
#26254](https://github.com/orgs/community/discussions/26254), [required
permissions for fine-grained PATs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)).

## 3. Known platform gap: Checks permission on fine-grained PATs

The REST endpoint docs list **"Checks: read"** as the fine-grained
permission for `GET .../check-runs`, but community reports document it as a
platform gap: the permission is not reliably grantable for fine-grained
PATs, with 403s on Checks endpoints
([community discussion #129512](https://github.com/orgs/community/discussions/129512),
[gh cli #8842](https://github.com/cli/cli/issues/8842)). The preflight probe
surfaces exactly this condition at startup — the error names the known gap
and the supported postures:

1. **Keep the classic token** until GitHub resolves the gap (rollback
   posture, §5) — the broader blast radius is the known, accepted cost.
2. **GitHub App** with `Checks: read` installed on the Target Repository —
   GitHub staff's recommended alternative; a separate provisioning slice,
   out of scope here.

## 4. Migration steps

1. **Inventory the current token.** `gh auth status` (or any authenticated
   REST response's `x-oauth-scopes` header) shows the classic token's
   scopes; note whether `workflow` is present (§2). The preflight logs the
   token class and scopes it observes when validating.
2. **Generate the fine-grained PAT.** GitHub → Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → *Generate new
   token*: Resource owner = the Target Repository's owner; Repository
   access = "Only select repositories" → the Target Repository; permissions
   per the §1 matrix; expiration ≤ 366 days (record it in the deployment
   calendar — an expired token is a loud 401 at the next startup preflight).
3. **Stage it.** Set `GH_PAT` to the new value in the deployment
   environment (`.env`, systemd unit, or secret store) — do not restart yet.
4. **Validate the read scope from any machine** with the orchestrator
   checkout and the staged token in the environment:
   `GH_PAT=<new token> python -m orchestrator.preflight --scopes-only`
   Exit 0 = all read probes passed. Exit 1 = the gap report names every
   missing permission (fix the PAT and re-run; a failing Checks probe with
   a fine-grained token is §3 — choose a posture there before proceeding).
5. **Restart the supervisor.** The full startup preflight runs the scope
   validation plus the sandbox runtime/egress checks (FR-4/FR-5) and
   refuses to start on any failure.
6. **First-cycle write validation.** Write permissions have no
   non-destructive API probe, so the first real cycle proves them. Failure
   signatures and their missing grants:
   - `git push` rejected with HTTP 403 → **Contents** write missing.
   - `POST /repos/{repo}/pulls` → 403 → **Pull requests** write missing.
   - Issue comment/label operations → 403 → **Issues** write missing.
   - Workflow-file push fails with a `workflow`-scope refusal → **Workflows**
     write missing (§2).

## 5. Rollback notes

- **Keep the classic token** (documented, scoped-down if possible) until the
  fine-grained PAT has passed `--scopes-only` **and** one full cycle — this
  is the supported rollback path, not an afterthought.
- Rollback = set `GH_PAT` back to the classic value and restart the
  supervisor. No data migration is involved; the workspace, state files, and
  branches are token-independent.
- If the §3 Checks gap applies to your token, staying on the classic token
  (or moving to a GitHub App) is the supported posture — do not run with a
  fine-grained PAT whose Checks probe fails: the Merge-Node's judge polling
  would break silently mid-cycle.
- The classic token's broad `repo` scope remains the larger blast radius;
  treat its removal as the trigger to re-run this runbook.

## References

- Fine-grained PAT permission model and endpoint mappings — [GitHub Docs:
  permissions required for fine-grained PATs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) (T1, accessed 2026-09-16, verified against the Check runs endpoint docs).
- `X-Accepted-GitHub-Permissions` refusal header — [GitHub Docs:
  Troubleshooting the REST API](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api) (T1, accessed 2026-09-16).
- No scope-enumeration API for fine-grained PATs; behavioral probing as the
  workaround — [community discussion #156115](https://github.com/orgs/community/discussions/156115) (T2, accessed 2026-09-16).
- Token-type signals: `github_pat_` prefix, `x-oauth-scopes` header presence
  — [community discussion #25259](https://github.com/orgs/community/discussions/25259) (T2, accessed 2026-09-16).
- Workflows-scope push requirement — [community discussion
  #26254](https://github.com/orgs/community/discussions/26254) (T2, accessed 2026-09-16, verified against the classic-token error message).
- Checks-permission known gap on fine-grained PATs — [community discussion
  #129512](https://github.com/orgs/community/discussions/129512), [gh cli
  #8842](https://github.com/cli/cli/issues/8842) (T2, accessed 2026-09-16, contradiction between endpoint docs and grantable permissions confirmed by staff reply).
