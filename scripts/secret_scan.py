#!/usr/bin/env python3
"""Lightweight secret scanner for pre-commit and CI.

AGENT_DECISION: uses regex heuristics instead of detect-secrets/trufflehog
so the template needs no new dependencies and works offline in CI.
Upgrade path: swap to trufflehog pre-commit hook when dependency install is okay.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "github-pat": re.compile(r"ghp_[a-zA-Z0-9]{10,}"),
    "openrouter-key": re.compile(r"sk-or-v1-[a-zA-Z0-9_-]+"),
    "pem-private-key": re.compile(
        r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
    ),
    "high-entropy-base64": re.compile(r"(?:[A-Za-z0-9+/]{40,}(?:={0,2})\n?){2,}"),
}


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
        return b"\0" in chunk
    except Exception:
        return True


def scan_text(text: str, filename: str):
    findings = []
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            findings.append((filename, name, match.group().split("\n", 1)[0][:80]))
    return findings


def scan_file(path: Path) -> list:
    if path.name in (".env.example", "uv.lock"):
        return []
    if is_binary(path):
        return []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return scan_text(f.read(), str(path))


def get_staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(p) for p in result.stdout.splitlines() if p]


def get_all_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(p) for p in result.stdout.splitlines() if p]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true", help="scan staged files only")
    parser.add_argument("paths", nargs="*", help="specific paths to scan")
    args = parser.parse_args()

    if args.paths:
        paths = [Path(p) for p in args.paths if Path(p).is_file()]
    elif args.staged:
        paths = [p for p in get_staged_files() if p.is_file()]
    else:
        paths = [p for p in get_all_files() if p.is_file()]

    findings = []
    for path in paths:
        findings.extend(scan_file(path))

    if not findings:
        print("No secrets detected")
        return 0

    print("Secrets detected:", file=sys.stderr)
    for filename, name, snippet in findings:
        print(f"  {filename}: [{name}] {snippet!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
