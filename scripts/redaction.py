"""Secret redaction for Worker traces and telemetry span values.

Defense-in-depth layer (ADR-0037, layer 4): even if a secret reaches a tool
result or a telemetry attribute, it is scrubbed before being persisted to the
``worker_trace_*.jsonl`` sidecar or exported to the OTLP endpoint. This is a
best-effort scrub, not a guarantee — the structural controls (``run_command``
allowlist + env stripping, ``is_safe_path``, ``fetch_url`` DNS pinning) are
the primary defense; this layer ensures a residual secret is not durably
exposed.

Two redaction passes:

1. **Assignment lines** — shell-style ``NAME=value`` (incl. ``export NAME=…``)
   and colon-style bare-value ``NAME: value`` lines whose name ends in
   ``KEY`` / ``TOKEN`` / ``SECRET`` / ``PASSWORD`` have their value replaced
   with ``[REDACTED]``. This catches leaked env output (``printenv``, ``.env``
   contents) and YAML/config secrets whose value is not a known token shape.
   The patterns are deliberately anchored and constrained so they do not
   corrupt code: a ``==``/``!=``/``<=``/``>=`` comparison is never mistaken
   for an assignment, and JSON object lines (which start with ``{`` or use a
   quoted key) are not matched.
2. **Known token shapes** — ``ghp_…`` (GitHub classic PAT), ``github_pat_…``
   (GitHub fine-grained PAT) and ``sk-or-v1-…`` (OpenRouter key) substrings
   are replaced with ``[REDACTED:<kind>]`` anywhere in the text. This is the
   safety net for secrets that appear in JSON values, free text, or already-
   redacted assignment values.
"""

import re

# Known secret token shapes (order matters only for the replacement label).
_SECRET_TOKEN_RE = re.compile(
    r"github_pat_[A-Za-z0-9_]{40,}"
    r"|ghp_[A-Za-z0-9]{36,}"
    r"|sk-or-v1-[A-Za-z0-9_\-]{20,}"
)

# Shell-style `NAME=value` assignment (also `export NAME=value`). The name must
# end in KEY/TOKEN/SECRET/PASSWORD. `=(?!=)` rejects `==` comparisons so code
# like `if api_key == expected:` is never corrupted. Greedy value to end of
# line matches `FOO_KEY=a b c` env output.
_SHELL_ASSIGNMENT_RE = re.compile(
    r"(?i)^(?P<head>\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD)\s*=\s*(?!=))(?P<val>.+)$"
)

# Colon-style bare-value assignment (YAML/config `api_key: value`). The value
# is an unquoted, bracket/brace/comma-free token, so JSON object lines
# (`{"api_key": "…"}`) — which start with `{` or use a quoted key — are never
# matched here (their secrets are still scrubbed by the token-shape pass).
_COLON_ASSIGNMENT_RE = re.compile(
    r"(?i)^(?P<head>\s*(?:-\s+)?[A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD)\s*:\s*)"
    r"(?P<val>[^\s\"'{}\[\],]+)\s*$"
)


def _token_replacement(match: re.Match) -> str:
    token = match.group(0)
    if token.startswith("github_pat_"):
        kind = "github_pat"
    elif token.startswith("ghp_"):
        kind = "ghp"
    else:
        kind = "sk-or-v1"
    return f"[REDACTED:{kind}]"


def _redact_line(line: str) -> str:
    for pattern in (_SHELL_ASSIGNMENT_RE, _COLON_ASSIGNMENT_RE):
        m = pattern.match(line)
        if m:
            return m.group("head") + "[REDACTED]"
    return line


def redact_secrets(text: str) -> str:
    """Return ``text`` with secret token shapes and secret-assignment values
    replaced by redaction markers.

    Non-str / empty input is returned unchanged. A trailing newline, if
    present, is preserved so multi-line tool results keep their shape. The
    replacement is structural (``[REDACTED]`` / ``[REDACTED:<kind>]``) so a
    reviewer can tell what was scrubbed without recovering the secret.
    """
    if not isinstance(text, str) or not text:
        return text

    had_trailing_newline = text.endswith("\n")
    redacted = "\n".join(_redact_line(line) for line in text.splitlines())
    if had_trailing_newline:
        redacted += "\n"
    return _SECRET_TOKEN_RE.sub(_token_replacement, redacted)
