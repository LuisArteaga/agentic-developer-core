import contextlib
import fcntl
import os
import re
from pathlib import Path

from orchestrator import state
from orchestrator.path_safety import is_safe_path

# Can be overridden for testing purposes
_PROJECT_ROOT: Path | None = None

# ---------------------------------------------------------------------------
# ADR-0037 layer 1: run_command command allowlist + secret-stripped env.
# ---------------------------------------------------------------------------

# Curated set of binaries the Worker may execute via run_command. Network
# binaries (curl, wget, nc, ssh, scp, ...) are deliberately EXCLUDED — outbound
# research must go through fetch_url, which has SSRF protection. File-reading
# binaries (cat, ls, ...) are also EXCLUDED: they read arbitrary paths with no
# is_safe_path check, so a prompt-injected Worker could `cat .env` / `ls .git`
# and leak secrets into the LLM context, defeating ADR-0037 layer 2. File
# inspection must go through the is_safe_path-protected read_file /
# list_directory / grep_search tools. Overridable as a comma-separated list
# via AGENT_RUN_COMMAND_ALLOWLIST.
DEFAULT_RUN_COMMAND_ALLOWLIST = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "pip",
        "make",
        "git",
        "ruff",
        "mypy",
        "semgrep",
        "pip-audit",
        "echo",
    }
)

# Env vars that must never leak into a Worker subprocess: the named
# orchestrator secrets plus any var whose name ends in KEY/TOKEN/SECRET/
# PASSWORD (case-insensitive). A prompt-injected Worker cannot then exfiltrate
# credentials via `printenv`/`env` or inherit them into a child process.
_SECRET_ENV_NAMES = frozenset(
    {"GH_PAT", "GH_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY"}
)
_SECRET_ENV_NAME_RE = re.compile(r"(?i).*(?:KEY|TOKEN|SECRET|PASSWORD)$")

# ---------------------------------------------------------------------------
# ADR-0037 layer 6: bounded reads — prevent a planted oversized file from
# exhausting Worker context / memory (CWE-400).
# ---------------------------------------------------------------------------

# read_file refuses to read more than this many bytes in one call. The Worker
# can still read a large file in line ranges via start_line/end_line.
MAX_READ_FILE_BYTES = 512 * 1024  # 512 KB
# grep_search stops collecting matches after this many, returning a truncation
# note, so a query that matches millions of lines cannot OOM the context.
MAX_GREP_MATCHES = 500


def _resolve_run_command_allowlist() -> frozenset[str]:
    """Resolve the run_command allowlist, honoring the AGENT_RUN_COMMAND_ALLOWLIST override."""
    override = os.getenv("AGENT_RUN_COMMAND_ALLOWLIST", "").strip()
    if not override:
        return DEFAULT_RUN_COMMAND_ALLOWLIST
    return frozenset(name.strip() for name in override.split(",") if name.strip())


def _sanitize_subprocess_env() -> dict[str, str]:
    """Return a copy of os.environ with secret-bearing variables removed."""
    sanitized: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in _SECRET_ENV_NAMES:
            continue
        if _SECRET_ENV_NAME_RE.match(name):
            continue
        sanitized[name] = value
    return sanitized


def _truncate_output(output: str) -> str:
    """Truncate ``output`` to a maximum of 150 lines and 10 KB (10240 bytes).

    Single source of truth for command-output truncation, shared by
    ``run_command`` (Worker tool) and ``verify_node`` (orchestrator). Enforces
    the 150-line cap with a first-30 + last-100 window, then the 10 KB byte cap
    on the (possibly already line-truncated) result. This was unified
    so both surfaces apply the same bounds and the prior ``<= 130`` special case
    (which let oversized-but-short output pass untruncated) is gone.
    """
    lines = output.splitlines()
    if len(lines) > 150:
        first_part = lines[:30]
        last_part = lines[-100:]
        output = (
            "\n".join(first_part)
            + "\n\n... [Output truncated: exceeded 150 lines] ...\n\n"
            + "\n".join(last_part)
        )

    output_bytes = output.encode("utf-8", errors="replace")
    if len(output_bytes) > 10240:
        # Keep the first 10000 bytes and append a note (safe against partial
        # UTF-8 sequences).
        truncated_bytes = output_bytes[:10000]
        truncated_text = truncated_bytes.decode("utf-8", errors="replace")
        output = truncated_text + "\n\n... [Output truncated: exceeded 10 KB limit] ..."

    return output


@contextlib.contextmanager
def _state_lock():
    """Exclusive flock on a dedicated lock file beside state.json.

    Guards the load→mutate→save critical section in read_file/patch_file
    against parallel tool calls: create_react_agent runs a batch
    of tool calls concurrently, and two concurrent read_file/patch_file calls
    racing on state.json could lose a read-range mutation. The lock is held on
    a sidecar ``.lock`` file (not state.json itself) so it does not interfere
    with state.save's atomic temp-file replace.
    """
    lock_path = state.get_state_filepath().with_suffix(".lock")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    lock_file = open(lock_path, "w")  # noqa: SIM115 — flock manages the handle
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield lock_file
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def get_workspace_root() -> Path:
    """Resolve the active workspace root path, honoring GITHUB_WORKSPACE and _PROJECT_ROOT overrides."""
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    workspace_env = os.getenv("GITHUB_WORKSPACE", ".")
    p = Path(workspace_env)
    if not p.is_absolute():
        # Resolve relative to the orchestrator's project root (parent of orchestrator package)
        orchestrator_root = Path(__file__).resolve().parent.parent
        p = (orchestrator_root / p).resolve()
    else:
        p = p.resolve()
    return p


def _normalize_path(path_str: str) -> tuple[Path, str]:
    """Helper to resolve a path and return its absolute Path object and project-relative string path.

    Enforces path safety by raising ValueError if the resolved path is outside the project root.
    """
    project_root = get_workspace_root()
    p = Path(path_str)
    if not p.is_absolute():
        abs_path = (project_root / p).resolve()
    else:
        abs_path = p.resolve()

    try:
        rel_path = abs_path.relative_to(project_root)
        rel_str = str(rel_path)
    except ValueError:
        raise ValueError("Access denied: Path is outside the project root directory.")

    return abs_path, rel_str


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    """Sort and merge overlapping/adjacent [start, end] (1-based inclusive) ranges.

    Returns a canonical list of disjoint, ascending ranges. Invalid ranges where
    start > end are dropped.
    """
    cleaned = sorted(
        (int(s), int(e))
        for s, e in ranges
        if s is not None and e is not None and int(s) <= int(e)
    )
    merged: list[list[int]] = []
    for s, e in cleaned:
        if not merged:
            merged.append([s, e])
            continue
        last = merged[-1]
        if s <= last[1] + 1:  # overlapping or immediately adjacent
            last[1] = max(last[1], e)
        else:
            merged.append([s, e])
    return merged


def _ranges_contain(ranges: list[list[int]], lo: int, hi: int) -> bool:
    """Return True if the inclusive interval [lo, hi] is fully covered by the union of ranges."""
    merged = _merge_ranges(ranges)
    return any(s <= lo and hi <= e for s, e in merged)


def _recompute_ranges(
    ranges: list[list[int]], edit_start: int, edit_end: int, delta: int
) -> list[list[int]]:
    """Recompute read ranges for a file after a patch at [edit_start, edit_end].

    - Ranges entirely above the edit (end < edit_start) keep their line numbers (content
      above the edit is untouched).
    - Ranges entirely below the edit (start > edit_end) shift by `delta` (the change in
      total line count), since content below the edit moves up/down in lockstep.
    - Ranges overlapping the edit are split: the above-portion is kept unchanged, the
      below-portion is shifted; the overlapped span (the rewritten content) is dropped,
      forcing a fresh read before re-editing that region.

    `delta` = new_total_lines - old_total_lines (the single replaced region is the only
    change, so the shift at the edit point equals the total line-count delta).
    """
    out: list[list[int]] = []
    for s, e in ranges:
        if e < edit_start:
            out.append([s, e])
        elif s > edit_end:
            out.append([s + delta, e + delta])
        else:
            if s < edit_start:
                out.append([s, edit_start - 1])
            if e > edit_end:
                out.append([edit_end + 1 + delta, e + delta])
    return _merge_ranges(out)


def read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    """Read contents of a file, with optional 1-based start_line and end_line bounds (inclusive).

    Registers the file path in the 'read_files' list inside the orchestrator state.
    """
    try:
        abs_path, rel_str = _normalize_path(path)
    except ValueError as e:
        return f"Error: {e}"

    # Runtime Path-Safety Validation (ADR-0035): re-validate against the shared
    # sensitive-path blocklist at tool-call time, on the normalized relative
    # path. Gates reads so secrets never enter the agent's context and so a
    # blocked path is never registered in read_files (closing the
    # read-then-patch bypass of ADR-0012's pre-flight-only validation).
    if not is_safe_path(rel_str):
        return f"Error: Path '{path}' is blocked by the path-safety policy and cannot be read."

    if not abs_path.exists():
        return f"Error: File '{path}' does not exist."
    if not abs_path.is_file():
        return f"Error: '{path}' is a directory, not a file."

    try:
        # Binary check: search for null byte in the first chunk
        with open(abs_path, "rb") as f:
            raw = f.read(MAX_READ_FILE_BYTES + 1)
        if b"\0" in raw[:1024]:
            return f"Error: File '{path}' is a binary file."
        # Bounded read (ADR-0037 layer 6): refuse oversized files so a planted
        # huge file cannot exhaust context/memory (CWE-400). The Worker can
        # still read a large file in line ranges via start_line/end_line.
        if len(raw) > MAX_READ_FILE_BYTES:
            return (
                f"Error: File '{path}' exceeds the {MAX_READ_FILE_BYTES}-byte "
                f"read cap. Read it in smaller portions using start_line/end_line."
            )
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: File '{path}' cannot be decoded with UTF-8 encoding."
    except Exception as e:
        return f"Error: Failed to read file '{path}': {e}"

    lines = content.splitlines()
    total_lines = len(lines)

    # Validation of bounds
    if start_line is not None:
        if not isinstance(start_line, int) or start_line <= 0:
            return "Error: start_line must be a positive integer."
        if start_line > total_lines > 0:
            return f"Error: start_line {start_line} exceeds total lines {total_lines}."

    if end_line is not None:
        if not isinstance(end_line, int) or end_line <= 0:
            return "Error: end_line must be a positive integer."
        if end_line > total_lines > 0:
            return f"Error: end_line {end_line} exceeds total lines {total_lines}."

    if start_line is not None and end_line is not None and start_line > end_line:
        return f"Error: start_line {start_line} cannot be greater than end_line {end_line}."

    # If the file is completely empty and start_line/end_line are requested
    if total_lines == 0 and (start_line is not None or end_line is not None):
        return f"Error: File '{path}' is empty."

    s = start_line - 1 if start_line is not None else 0
    end_idx = end_line if end_line is not None else total_lines
    sliced_lines = lines[s:end_idx]

    # Compute the 1-based inclusive line range actually read and register it in
    # orchestrator state. A full read (no bounds) authorizes [1, total_lines].
    read_lo = start_line if start_line is not None else 1
    read_hi = end_line if end_line is not None else total_lines
    read_range: list[int]
    if total_lines == 0:
        # Empty file: nothing to authorize; record an empty range list for the path.
        read_range = None  # type: ignore[assignment]
    else:
        read_range = [read_lo, read_hi]

    # Register the read range in orchestrator state (range-scoped Read-Before-Edit, ADR-0033).
    # state.load() normalizes legacy list read_files to {}, so curr_state["read_files"] is a dict.
    # The load→mutate→save critical section is guarded by an exclusive flock
    # so two concurrent tool calls cannot lose a read-range
    # mutation (create_react_agent runs a batch of tool calls concurrently).
    try:
        with _state_lock():
            curr_state = state.load()
            read_ranges: dict = curr_state["read_files"]
            existing = read_ranges.get(rel_str, [])
            if read_range is not None:
                existing = _merge_ranges(existing + [read_range])
            else:
                existing = _merge_ranges(existing)
            read_ranges[rel_str] = existing
            state.save(curr_state)
    except Exception:
        # Do not fail the file read if state persistence is unavailable
        pass

    return "\n".join(sliced_lines)


def list_directory(path: str) -> str:
    """List the contents of a directory, sorted alphabetically with directories first, followed by files."""
    try:
        abs_path, rel_str = _normalize_path(path)
    except ValueError as e:
        return f"Error: {e}"

    # Runtime Path-Safety Validation (ADR-0037 layer 2): validate the resolved
    # relative path against the sensitive-path blocklist before listing, so
    # `list_directory(path=".git")` returns the policy error, not contents.
    if not is_safe_path(rel_str):
        return f"Error: Path '{path}' is blocked by the path-safety policy and cannot be listed."

    if not abs_path.exists():
        return f"Error: Directory '{path}' does not exist."
    if not abs_path.is_dir():
        return f"Error: '{path}' is a file, not a directory."

    try:
        entries = list(abs_path.iterdir())
    except Exception as e:
        return f"Error: Failed to list directory '{path}': {e}"

    if not entries:
        return "(empty directory)"

    # Sort: directories first, then files, alphabetically by name
    entries.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

    formatted = []
    for entry in entries:
        if entry.is_dir():
            formatted.append(f"[DIR] {entry.name}")
        else:
            try:
                size = entry.stat().st_size
            except Exception:
                size = 0
            formatted.append(f"[FILE] {entry.name} ({size} bytes)")

    return "\n".join(formatted)


def grep_search(query: str, path: str) -> str:
    """Search for the literal query string inside the target path (recursively if directory).

    Ignores common non-code / environment directories.
    """
    try:
        abs_path, rel_str = _normalize_path(path)
    except ValueError as e:
        return f"Error: {e}"

    # Runtime Path-Safety Validation (ADR-0037 layer 2): validate the target
    # path before any read, so `grep_search(query="x", path=".env")` returns
    # the policy error, not live secrets.
    if not is_safe_path(rel_str):
        return f"Error: Path '{path}' is blocked by the path-safety policy and cannot be searched."

    if not abs_path.exists():
        return f"Error: Path '{path}' does not exist."

    ignored_names = {
        ".git",
        ".venv",
        ".agent_logs",
        ".agents",
        "node_modules",
        "__pycache__",
    }

    matches: list[str] = []
    match_limit = MAX_GREP_MATCHES

    def search_file(file_path: Path) -> None:
        # Bounded matches (ADR-0037 layer 6): stop once the cap is reached.
        if len(matches) >= match_limit:
            return
        try:
            _, file_rel = _normalize_path(str(file_path))
        except ValueError:
            return
        # Re-check each walked file's relative path against the blocklist
        # (ADR-0037 layer 2): a safe target dir can still contain a sensitive
        # file (e.g. .env) that must not be read into the Worker context.
        if not is_safe_path(file_rel):
            return
        file_matches: list[str] = []
        try:
            # Quick binary check
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                if b"\0" in chunk:
                    return
            with open(file_path, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f, 1):
                    if query in line:
                        clean_line = line.rstrip("\r\n")
                        file_matches.append(f"{file_rel}:{idx}:{clean_line}")
                        if len(matches) + len(file_matches) >= match_limit:
                            break
        except Exception:
            pass
        matches.extend(file_matches)

    if abs_path.is_file():
        search_file(abs_path)
    elif abs_path.is_dir():
        for root, dirs, files in os.walk(abs_path):
            # Prune directory search recursively
            dirs[:] = [d for d in dirs if d not in ignored_names]
            if len(matches) >= match_limit:
                break
            for file in files:
                file_path = Path(root) / file
                search_file(file_path)
                if len(matches) >= match_limit:
                    break

    if not matches:
        return f"No matches found for query '{query}' in '{path}'."

    truncated = len(matches) >= match_limit
    if truncated:
        matches = matches[:match_limit]
    result = "\n".join(matches)
    if truncated:
        result += (
            f"\n\n... [Results truncated: reached the {match_limit}-match cap] ..."
        )
    return result


def patch_file(path: str, old_string: str, new_string: str) -> str:
    """Perform exact search-and-replace of old_string with new_string.

    Enforces 'Read-Before-Edit' by verifying that the normalized path has been read in the
    current execution cycle, and that the line span occupied by old_string falls within the
    previously read line ranges (range-scoped, ADR-0033).
    Enforces 'Ambiguity Abort' by verifying that old_string matches exactly once in the file.
    """
    try:
        abs_path, rel_str = _normalize_path(path)
    except ValueError as e:
        return f"Error: {e}"

    # 0. Runtime Path-Safety Validation (ADR-0035): re-validate against the
    #    shared sensitive-path blocklist at tool-call time, on the normalized
    #    relative path, BEFORE the Read-Before-Edit gate. A blocked path is a
    #    hard stop even if it was somehow registered in read_files, closing the
    #    bypass of ADR-0012's pre-flight-only validation (defense in depth).
    if not is_safe_path(rel_str):
        return f"Error: Path '{path}' is blocked by the path-safety policy and cannot be modified."

    # 1. Read-Before-Edit Constraint (path-level): the file must have been read at least
    #    once in this cycle (its path is a key in read_files). Whether the *lines* targeted
    #    by old_string were read is checked later by the range gate. state.load() normalizes
    #    legacy list read_files to {}, so read_files is a dict here.
    try:
        curr_state = state.load()
        read_files: dict = curr_state["read_files"]
    except Exception:
        read_files = {}

    file_ranges = read_files.get(rel_str, [])
    if rel_str not in read_files:
        return f"Error: Read-Before-Edit validation failed. File '{path}' has not been read in the current execution cycle. Please call 'read_file' first."

    # 2. Path Validation & Existence
    if not abs_path.exists():
        return f"Error: File '{path}' does not exist."
    if not abs_path.is_file():
        return f"Error: '{path}' is a directory, not a file."

    # 3. Binary & Decode checks
    try:
        # Binary check: search for null byte in the first chunk
        with open(abs_path, "rb") as f:
            chunk = f.read(1024)
            if b"\0" in chunk:
                return f"Error: File '{path}' is a binary file."

        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"Error: File '{path}' cannot be decoded with UTF-8 encoding."
    except Exception as e:
        return f"Error: Failed to read file '{path}': {e}"

    # 4. Ambiguity Abort Rule (Uniqueness Check)
    matches_count = content.count(old_string)
    if matches_count == 0:
        return "Error: The old_string was not found in the file. It is possible the file was modified or you have outdated/incorrect context lines. Please call 'read_file' first to synchronize your state with the disk, then try again with the updated content."
    elif matches_count > 1:
        return f"Error: The old_string matches multiple times ({matches_count} occurrences). To resolve this ambiguity, please include more surrounding context lines in 'old_string' so that the match is unique."

    # 5. Range-Scoped Read-Before-Edit: the old_string's line span must fall within a
    #    previously read range. Locate the unique match and compute its 1-based inclusive
    #    line span, then verify it is fully covered by the union of read ranges.
    idx = content.find(old_string)
    edit_start = 1 + content.count("\n", 0, idx)
    edit_end = edit_start + old_string.count("\n")

    if not _ranges_contain(file_ranges, edit_start, edit_end):
        ranges_repr = (
            ", ".join(f"[{s}-{e}]" for s, e in _merge_ranges(file_ranges)) or "(none)"
        )
        return (
            f"Error: Read-Before-Edit range validation failed. The edit targets lines "
            f"{edit_start}-{edit_end} of '{path}', which fall outside the line ranges "
            f"previously read for this file ({ranges_repr}). Please call 'read_file' on "
            f"lines {edit_start}-{edit_end} (or the whole file) first, then retry the patch."
        )

    # 6. Perform the edit
    new_content = content.replace(old_string, new_string, 1)

    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"Error: Failed to write to file '{path}': {e}"

    # 7. Recompute read ranges for this file: shift/split around the edited region so the
    #    recorded ranges remain aligned with the post-edit line numbers (ADR-0033).
    delta = len(new_content.splitlines()) - len(content.splitlines())
    try:
        with _state_lock():
            post_state = state.load()
            post_ranges: dict = post_state["read_files"]
            existing = post_ranges.get(rel_str, file_ranges)
            post_ranges[rel_str] = _recompute_ranges(
                existing, edit_start, edit_end, delta
            )
            state.save(post_state)
    except Exception:
        pass

    return f"Success: File '{path}' patched successfully. One occurrence replaced."


def run_command(command: str) -> str:
    """Execute a shell command safely in a subprocess with shell=False.

    Captures stdout and stderr together. If the output exceeds 150 lines or 10 KB,
    it is truncated (first 30 + last 100 lines, then a 10 KB byte cap).
    A timeout of 300 seconds is enforced.

    Security (ADR-0037 layer 1): only a curated allowlist of binaries may run
    (network binaries are excluded — outbound research must use fetch_url),
    and the child receives a secret-stripped environment so a prompt-injected
    Worker cannot exfiltrate credentials via `printenv`/`env`.
    """
    import shlex
    import subprocess

    project_root = get_workspace_root()

    try:
        args = shlex.split(command)
    except Exception as e:
        return f"Error: Failed to parse command string: {e}"

    if not args:
        return "Error: Empty command provided."

    # ADR-0037 layer 1: command allowlist. Only a curated set of binaries may
    # run; network binaries are excluded so exfiltration must go through
    # fetch_url (SSRF-protected). PATH tricks are neutralized by matching on
    # the basename of args[0].
    allowlist = _resolve_run_command_allowlist()
    binary = os.path.basename(args[0])
    if binary not in allowlist:
        return (
            f"Error: Command '{binary}' is not in the run_command allowlist. "
            f"Allowed binaries: {', '.join(sorted(allowlist))}. Network access "
            f"is not permitted from run_command; use the fetch_url tool for "
            f"outbound requests."
        )

    try:
        # Run the command with a 300 second timeout, capturing stdout and
        # stderr together. The child receives a sanitized env with all
        # secret-bearing variables removed (ADR-0037 layer 1).
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project_root,
            timeout=300,
            env=_sanitize_subprocess_env(),
        )
        output_bytes = result.stdout
        timed_out = False
    except subprocess.TimeoutExpired as e:
        output_bytes = e.stdout or b""
        timed_out = True
    except Exception as e:
        return f"Error: Failed to run command: {e}"

    # Decode gracefully
    output = output_bytes.decode("utf-8", errors="replace")

    # Handle empty output
    if not output and not timed_out:
        return "(Command completed successfully with no output.)"
    elif not output and timed_out:
        return "Error: Command timed out after 300 seconds with no output."

    # Unified truncation: 150-line + 10 KB byte cap, shared with
    # verify_node via _truncate_output. The prior `<= 130` special case (which
    # let oversized-but-short output pass untruncated) is removed.
    output = _truncate_output(output)

    if timed_out:
        return f"Error: Command '{command}' timed out after 300 seconds.\nOutput captured before timeout:\n{output}"

    return output
