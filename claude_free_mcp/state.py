"""Session state shared by all tools for the lifetime of the server process."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Session:
    cwd: Path = field(default_factory=Path.home)
    # Absolute path -> mtime_ns at the time the model last read or wrote the file.
    read_files: dict[Path, int] = field(default_factory=dict)

    def resolve(self, path: str) -> Path:
        """Resolve a user-supplied path against the session's working directory."""
        p = Path(os.path.expandvars(os.path.expanduser(path)))
        if not p.is_absolute():
            p = self.cwd / p
        return p.resolve()

    def mark_read(self, path: Path) -> None:
        self.read_files[path] = path.stat().st_mtime_ns


session = Session()
