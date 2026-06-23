import os
from pathlib import Path
from typing import Optional
from orchestrator import state

def _normalize_path(path_str: str) -> tuple[Path, str]:
    """Helper to resolve a path and return its absolute Path object and project-relative string path."""
    project_root = Path(__file__).resolve().parent.parent
    p = Path(path_str)
    if not p.is_absolute():
        abs_path = (project_root / p).resolve()
    else:
        abs_path = p.resolve()
    
    try:
        rel_path = abs_path.relative_to(project_root)
        rel_str = str(rel_path)
    except ValueError:
        rel_str = str(abs_path)
    
    return abs_path, rel_str

def read_file(path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> str:
    """Read contents of a file, with optional 1-based start_line and end_line bounds (inclusive).
    
    Registers the file path in the 'read_files' list inside the orchestrator state.
    """
    abs_path, rel_str = _normalize_path(path)
    
    if not abs_path.exists():
        return f"Error: File '{path}' does not exist."
    if not abs_path.is_file():
        return f"Error: '{path}' is a directory, not a file."
        
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
        
    lines = content.splitlines()
    total_lines = len(lines)
    
    # Validation of bounds
    if start_line is not None:
        if not isinstance(start_line, int) or start_line <= 0:
            return "Error: start_line must be a positive integer."
        if start_line > total_lines and total_lines > 0:
            return f"Error: start_line {start_line} exceeds total lines {total_lines}."
            
    if end_line is not None:
        if not isinstance(end_line, int) or end_line <= 0:
            return "Error: end_line must be a positive integer."
        if end_line > total_lines and total_lines > 0:
            return f"Error: end_line {end_line} exceeds total lines {total_lines}."
            
    if start_line is not None and end_line is not None and start_line > end_line:
        return f"Error: start_line {start_line} cannot be greater than end_line {end_line}."
        
    # If the file is completely empty and start_line/end_line are requested
    if total_lines == 0 and (start_line is not None or end_line is not None):
        return f"Error: File '{path}' is empty."
        
    s = start_line - 1 if start_line is not None else 0
    e = end_line if end_line is not None else total_lines
    sliced_lines = lines[s:e]
    
    # Register the file read in orchestrator state
    try:
        curr_state = state.load()
        if "read_files" not in curr_state:
            curr_state["read_files"] = []
        if rel_str not in curr_state["read_files"]:
            curr_state["read_files"].append(rel_str)
            state.save(curr_state)
    except Exception:
        # Log state update warnings, but do not fail the file read if the state is not available
        pass
        
    return "\n".join(sliced_lines)

def list_directory(path: str) -> str:
    """List the contents of a directory, sorted alphabetically with directories first, followed by files."""
    abs_path, _ = _normalize_path(path)
    
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
    abs_path, _ = _normalize_path(path)
    
    if not abs_path.exists():
        return f"Error: Path '{path}' does not exist."
        
    ignored_names = {".git", ".venv", ".agent_logs", ".agents", "node_modules", "__pycache__"}
    
    def search_file(file_path: Path) -> list[str]:
        file_matches = []
        _, rel_str = _normalize_path(str(file_path))
        try:
            # Quick binary check
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                if b"\0" in chunk:
                    return []
            with open(file_path, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f, 1):
                    if query in line:
                        clean_line = line.rstrip("\r\n")
                        file_matches.append(f"{rel_str}:{idx}:{clean_line}")
        except Exception:
            pass
        return file_matches

    matches = []
    if abs_path.is_file():
        matches.extend(search_file(abs_path))
    elif abs_path.is_dir():
        for root, dirs, files in os.walk(abs_path):
            # Prune directory search recursively
            dirs[:] = [d for d in dirs if d not in ignored_names]
            for file in files:
                file_path = Path(root) / file
                matches.extend(search_file(file_path))
                
    if not matches:
        return f"No matches found for query '{query}' in '{path}'."
        
    return "\n".join(matches)

def patch_file(path: str, old_string: str, new_string: str) -> str:
    """Perform exact search-and-replace of old_string with new_string.
    
    Enforces 'Read-Before-Edit' by verifying that the normalized path has been registered in the
    'read_files' list inside the orchestrator state.
    Enforces 'Ambiguity Abort' by verifying that old_string matches exactly once in the file.
    """
    abs_path, rel_str = _normalize_path(path)
    
    # 1. Read-Before-Edit Constraint
    try:
        curr_state = state.load()
        read_files = curr_state.get("read_files", [])
    except Exception:
        read_files = []
        
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
        return f"Error: The old_string was not found in the file. It is possible the file was modified or you have outdated/incorrect context lines. Please call 'read_file' first to synchronize your state with the disk, then try again with the updated content."
    elif matches_count > 1:
        return f"Error: The old_string matches multiple times ({matches_count} occurrences). To resolve this ambiguity, please include more surrounding context lines in 'old_string' so that the match is unique."

    # 5. Perform the edit
    new_content = content.replace(old_string, new_string, 1)
    
    try:
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:
        return f"Error: Failed to write to file '{path}': {e}"
        
    return f"Success: File '{path}' patched successfully. One occurrence replaced."
