"""Search tools: glob and grep."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..state import session

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox"}
MAX_GLOB_RESULTS = 200


def glob(pattern: str, path: str = ".") -> str:
    base = session.resolve(path)
    if not base.is_dir():
        raise ValueError(f"{base} is not a directory.")
    matches = []
    for m in base.glob(pattern):
        rel_parts = m.relative_to(base).parts
        if any(part in SKIP_DIRS for part in rel_parts[:-1]) or not m.is_file():
            continue
        matches.append(m)
    if not matches:
        return "No files found."
    matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    out = [str(m) for m in matches[:MAX_GLOB_RESULTS]]
    if len(matches) > MAX_GLOB_RESULTS:
        out.append(f"[{len(matches) - MAX_GLOB_RESULTS} more not shown; narrow the pattern]")
    return "\n".join(out)


def _rg_args(pattern, base, glob_filter, output_mode, context, case_insensitive):
    args = ["rg", "--no-heading", "--color=never", "--hidden", "--glob=!.git"]
    if case_insensitive:
        args.append("-i")
    if glob_filter:
        args.append(f"--glob={glob_filter}")
    if output_mode == "files_with_matches":
        args.append("-l")
    elif output_mode == "count":
        args.append("-c")
    else:
        args.append("-n")
        if context:
            args.append(f"-C{context}")
    args += ["-e", pattern, "--", str(base)]
    return args


def _python_grep(pattern, base: Path, glob_filter, output_mode, context, case_insensitive) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    files = [base] if base.is_file() else []
    if base.is_dir():
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                fp = Path(root) / n
                if glob_filter and not (
                    fnmatch.fnmatch(n, glob_filter) or fnmatch.fnmatch(fp.relative_to(base).as_posix(), glob_filter)
                ):
                    continue
                files.append(fp)
    out: list[str] = []
    for fp in files:
        try:
            raw = fp.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        hits = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hits:
            continue
        if output_mode == "files_with_matches":
            out.append(str(fp))
        elif output_mode == "count":
            out.append(f"{fp}:{len(hits)}")
        else:
            shown: set[int] = set()
            for i in hits:
                for j in range(max(i - context, 0), min(i + context + 1, len(lines))):
                    if j not in shown:
                        shown.add(j)
                        sep = ":" if j == i or j in hits else "-"
                        out.append(f"{fp}{sep}{j + 1}{sep}{lines[j]}")
    return out


def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    output_mode: str = "files_with_matches",
    context: int = 0,
    case_insensitive: bool = False,
    head_limit: int = 200,
) -> str:
    if output_mode not in ("files_with_matches", "content", "count"):
        raise ValueError("output_mode must be 'files_with_matches', 'content' or 'count'.")
    base = session.resolve(path)
    if not base.exists():
        raise ValueError(f"{base} does not exist.")
    if shutil.which("rg"):
        proc = subprocess.run(
            _rg_args(pattern, base, glob, output_mode, context, case_insensitive),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        if proc.returncode == 2 and not proc.stdout:
            raise ValueError(proc.stderr.strip() or "ripgrep failed.")
        lines = proc.stdout.splitlines()
    else:
        lines = _python_grep(pattern, base, glob, output_mode, context, case_insensitive)
    if not lines:
        return "No matches found."
    out = lines[:head_limit]
    if len(lines) > head_limit:
        out.append(f"[{len(lines) - head_limit} more lines not shown; narrow the search or raise head_limit]")
    return "\n".join(out)
