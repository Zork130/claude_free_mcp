"""File tools: read, write, edit, multi-edit, list."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..state import session

MAX_LINE_CHARS = 2000
DEFAULT_READ_LIMIT = 2000


@dataclass
class TextFile:
    text: str  # always "\n" line endings
    encoding: str
    crlf: bool


def _load(path: Path) -> TextFile:
    raw = path.read_bytes()
    if b"\0" in raw[:8192]:
        raise ValueError(f"{path} looks like a binary file; refusing to treat it as text.")
    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # latin-1 round-trips every byte, so edits never corrupt unknown encodings.
        text, encoding = raw.decode("latin-1"), "latin-1"
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    return TextFile(text, encoding, crlf)


def _save(path: Path, f: TextFile) -> None:
    text = f.text.replace("\n", "\r\n") if f.crlf else f.text
    path.write_bytes(text.encode(f.encoding))
    session.mark_read(path)


def _require_fresh_read(path: Path) -> None:
    seen = session.read_files.get(path)
    if seen is None:
        raise ValueError(f"Read {path} with read_file before modifying it.")
    if path.stat().st_mtime_ns != seen:
        raise ValueError(f"{path} changed since it was last read. Read it again before modifying it.")


def read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LIMIT) -> str:
    p = session.resolve(path)
    if p.is_dir():
        raise ValueError(f"{p} is a directory. Use list_dir instead.")
    f = _load(p)
    session.mark_read(p)
    lines = f.text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return f"<{p} is empty>"
    start = max(offset, 1)
    chunk = lines[start - 1 : start - 1 + limit]
    width = len(str(start + len(chunk)))
    out = []
    for n, line in enumerate(chunk, start):
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "… [line truncated]"
        out.append(f"{n:>{width}}\t{line}")
    end = start + len(chunk) - 1
    if start > 1 or end < len(lines):
        out.append(f"\n[showing lines {start}-{end} of {len(lines)}; use offset/limit to see more]")
    return "\n".join(out)


def write_file(path: str, content: str) -> str:
    p = session.resolve(path)
    if p.exists():
        _require_fresh_read(p)
        f = _load(p)
        f.text = content.replace("\r\n", "\n")
        _save(p, f)
        return f"Overwrote {p} ({len(content)} chars)."
    p.parent.mkdir(parents=True, exist_ok=True)
    _save(p, TextFile(content.replace("\r\n", "\n"), "utf-8", crlf=False))
    return f"Created {p} ({len(content)} chars)."


def _apply_edit(text: str, old: str, new: str, replace_all: bool) -> str:
    if old == new:
        raise ValueError("old_string and new_string are identical.")
    if old == "":
        raise ValueError("old_string must not be empty. Use write_file to create a file.")
    count = text.count(old)
    if count == 0:
        raise ValueError("old_string not found in file. It must match exactly, including whitespace.")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_string occurs {count} times. Add surrounding context to make it unique, "
            "or set replace_all=true."
        )
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def _snippet(text: str, needle: str, context: int = 3) -> str:
    idx = text.find(needle) if needle else -1
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if idx < 0:
        return ""
    first = text.count("\n", 0, idx)
    last = first + needle.count("\n")
    lo, hi = max(first - context, 0), min(last + context + 1, len(lines))
    return "\n".join(f"{n + 1:>6}\t{lines[n]}" for n in range(lo, hi))


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = session.resolve(path)
    _require_fresh_read(p)
    f = _load(p)
    f.text = _apply_edit(f.text, old_string.replace("\r\n", "\n"), new_string.replace("\r\n", "\n"), replace_all)
    _save(p, f)
    snippet = _snippet(f.text, new_string.replace("\r\n", "\n"))
    return f"Edited {p}." + (f"\n{snippet}" if snippet else "")


def multi_edit(path: str, edits: list[dict]) -> str:
    p = session.resolve(path)
    _require_fresh_read(p)
    f = _load(p)
    text = f.text
    for i, e in enumerate(edits, 1):
        try:
            text = _apply_edit(
                text,
                e["old_string"].replace("\r\n", "\n"),
                e["new_string"].replace("\r\n", "\n"),
                e.get("replace_all", False),
            )
        except ValueError as err:
            raise ValueError(f"Edit {i} failed, no changes written: {err}") from err
    f.text = text
    _save(p, f)
    return f"Applied {len(edits)} edits to {p}."


def list_dir(path: str = ".") -> str:
    p = session.resolve(path)
    if not p.is_dir():
        raise ValueError(f"{p} is not a directory.")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    out = [f"{p}:"]
    for e in entries:
        try:
            if e.is_dir():
                out.append(f"  {e.name}/")
            else:
                out.append(f"  {e.name}  ({e.stat().st_size} bytes)")
        except OSError:
            out.append(f"  {e.name}  (unreadable)")
    if not entries:
        out.append("  <empty>")
    return "\n".join(out)
