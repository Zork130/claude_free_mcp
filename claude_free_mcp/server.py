"""MCP server exposing Claude Code-style tools to Claude Desktop over stdio."""

from __future__ import annotations

import argparse
import atexit
import os
import sys
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__
from .state import session
from .tools import files, search, shell, todo, web

INSTRUCTIONS = """\
These tools give you direct access to the user's computer so you can work like a coding agent:
read, search and edit files, and run shell commands. Work the way a careful senior engineer would.

- Explore before changing: use glob/grep/list_dir to find relevant code, then read_file it.
- Read a file before editing it; edit_file and write_file refuse otherwise.
- Prefer edit_file / multi_edit for changes to existing files; use write_file for new files
  or full rewrites. Match the surrounding code's style.
- Relative paths resolve against the current working directory, which shell `cd` changes.
- Run tests, linters or the program after changes to verify them. Use run_in_background for
  servers and long builds, then poll with job_output.
- For multi-step tasks, keep a todo list with todo_write and update it as you go.
- Report results faithfully: if something failed or was skipped, say so.
- Ask before destructive or irreversible actions (deleting files, force-pushing, etc.).
"""

CODING_AGENT_PROMPT = """\
You are acting as an autonomous software engineering agent with access to the user's machine
through the claude-free-mcp tools (read_file, write_file, edit_file, multi_edit, list_dir,
glob, grep, shell, job_output, kill_job, list_jobs, todo_write, todo_read, web_fetch).

How to work:
1. Understand the task. If it is ambiguous in a way that matters, ask one focused question.
2. Orient yourself: run `shell` with `pwd` and list the project; read README/config files;
   use grep/glob to locate the code involved instead of guessing.
3. For anything with more than a couple of steps, write a todo list and keep it updated,
   marking exactly one item in_progress at a time.
4. Make changes with edit_file/multi_edit, keeping diffs minimal and matching existing style.
   Don't add features, refactors or comments that weren't asked for.
5. Verify: run the tests, type-checker, build or the program itself. Fix failures you caused.
6. Finish with a short summary of what changed (file paths), how you verified it, and
   anything left undone. Don't claim success you didn't verify.

Safety: never run destructive commands (rm -rf, git reset --hard, force push, dropping
databases) without explicit confirmation. Don't commit or push unless asked.
"""

server = MCPServer(
    name="claude-free-mcp",
    title="Claude Code-style tools",
    instructions=INSTRUCTIONS,
    version=__version__,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
SHELL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def _run(fn, *args, **kwargs) -> str:
    """Call a tool implementation, turning failures into messages the model can read."""
    try:
        return fn(*args, **kwargs)
    except ToolError:
        raise
    except ValueError as e:
        raise ToolError(str(e)) from e
    except Exception as e:  # noqa: BLE001 - surface every failure to the model
        raise ToolError(f"{type(e).__name__}: {e}") from e


class EditOp(BaseModel):
    old_string: str = Field(description="Exact text to replace")
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False, description="Replace every occurrence")


class TodoItem(BaseModel):
    content: str = Field(description="What needs doing, imperative form")
    status: Literal["pending", "in_progress", "completed"]


@server.tool(annotations=READ_ONLY, structured_output=False)
def read_file(
    path: Annotated[str, Field(description="File path, absolute or relative to the working directory")],
    offset: Annotated[int, Field(description="1-based line to start from")] = 1,
    limit: Annotated[int, Field(description="Maximum number of lines to return")] = files.DEFAULT_READ_LIMIT,
) -> str:
    """Read a text file. Returns lines prefixed with their line numbers (cat -n style).
    The line-number prefix is not part of the file: never include it in edit strings."""
    return _run(files.read_file, path, offset, limit)


@server.tool(annotations=WRITES, structured_output=False)
def write_file(
    path: Annotated[str, Field(description="File path to create or overwrite")],
    content: Annotated[str, Field(description="Full file content")],
) -> str:
    """Create a file, or overwrite one you have already read. Parent directories are created.
    Prefer edit_file for changing existing files."""
    return _run(files.write_file, path, content)


@server.tool(annotations=WRITES, structured_output=False)
def edit_file(
    path: Annotated[str, Field(description="File to edit (must have been read first)")],
    old_string: Annotated[str, Field(description="Exact existing text, including indentation")],
    new_string: Annotated[str, Field(description="Text to put in its place")],
    replace_all: Annotated[bool, Field(description="Replace every occurrence instead of requiring a unique match")] = False,
) -> str:
    """Replace an exact string in a file. Fails if old_string is missing or not unique
    (add surrounding lines to disambiguate) or if the file changed since you read it."""
    return _run(files.edit_file, path, old_string, new_string, replace_all)


@server.tool(annotations=WRITES, structured_output=False)
def multi_edit(
    path: Annotated[str, Field(description="File to edit (must have been read first)")],
    edits: Annotated[list[EditOp], Field(description="Edits applied in order; all succeed or none are written")],
) -> str:
    """Apply several exact-string edits to one file atomically."""
    return _run(files.multi_edit, path, [e.model_dump() for e in edits])


@server.tool(annotations=READ_ONLY, structured_output=False)
def list_dir(path: Annotated[str, Field(description="Directory to list")] = ".") -> str:
    """List a directory's entries (directories first) with file sizes."""
    return _run(files.list_dir, path)


@server.tool(annotations=READ_ONLY, structured_output=False)
def glob(
    pattern: Annotated[str, Field(description="Glob pattern such as '**/*.py' or 'src/**/*.ts'")],
    path: Annotated[str, Field(description="Directory to search from")] = ".",
) -> str:
    """Find files by name pattern. Results are sorted by modification time, newest first.
    Skips .git, node_modules, virtualenvs and caches."""
    return _run(search.glob, pattern, path)


@server.tool(annotations=READ_ONLY, structured_output=False)
def grep(
    pattern: Annotated[str, Field(description="Regular expression to search for")],
    path: Annotated[str, Field(description="File or directory to search")] = ".",
    glob: Annotated[str | None, Field(description="Only search files matching this glob, e.g. '*.py'")] = None,
    output_mode: Annotated[
        Literal["files_with_matches", "content", "count"],
        Field(description="files_with_matches: file paths; content: matching lines; count: matches per file"),
    ] = "files_with_matches",
    context: Annotated[int, Field(description="Lines of context around matches (content mode)")] = 0,
    case_insensitive: bool = False,
    head_limit: Annotated[int, Field(description="Maximum output lines")] = 200,
) -> str:
    """Search file contents with a regex (uses ripgrep when installed)."""
    return _run(search.grep, pattern, path, glob, output_mode, context, case_insensitive, head_limit)


@server.tool(
    name="shell",
    annotations=SHELL,
    structured_output=False,
    description=(
        f"Run a command in {shell.shell_name()} on the user's machine ({sys.platform}). "
        "The working directory persists between calls (cd works). Output is stdout+stderr combined, "
        "truncated if very long. Commands cannot read stdin, so use non-interactive flags. "
        "Set run_in_background for servers/watchers/long builds and poll with job_output."
    ),
)
def shell_tool(
    command: Annotated[str, Field(description="The command to run")],
    timeout_sec: Annotated[int, Field(description=f"Kill the command after this many seconds (max {shell.MAX_TIMEOUT_SEC})")] = 120,
    run_in_background: Annotated[bool, Field(description="Start it as a background job and return immediately")] = False,
) -> str:
    return _run(shell.shell, command, timeout_sec, run_in_background)


@server.tool(annotations=READ_ONLY, structured_output=False)
def job_output(job_id: Annotated[str, Field(description="Background job id, e.g. 'job1'")]) -> str:
    """Get new output from a background job since the last check, and whether it is still running."""
    return _run(shell.job_output, job_id)


@server.tool(annotations=WRITES, structured_output=False)
def kill_job(job_id: Annotated[str, Field(description="Background job id")]) -> str:
    """Stop a background job and its child processes."""
    return _run(shell.kill_job, job_id)


@server.tool(annotations=READ_ONLY, structured_output=False)
def list_jobs() -> str:
    """List background jobs and their status."""
    return _run(shell.list_jobs)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False), structured_output=False)
def todo_write(todos: Annotated[list[TodoItem], Field(description="The complete, updated todo list")]) -> str:
    """Replace the todo list. Use it to plan multi-step work and show progress;
    keep exactly one item in_progress while working."""
    return _run(todo.todo_write, [t.model_dump() for t in todos])


@server.tool(annotations=READ_ONLY, structured_output=False)
def todo_read() -> str:
    """Show the current todo list."""
    return _run(todo.todo_read)


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True), structured_output=False)
def web_fetch(
    url: Annotated[str, Field(description="http(s) URL to fetch")],
    max_chars: Annotated[int, Field(description="Truncate the returned text to this many characters")] = 50_000,
) -> str:
    """Fetch a web page or file and return it as plain text (HTML is converted to text)."""
    return _run(web.web_fetch, url, max_chars)


@server.prompt(name="coding_agent", title="Work like Claude Code")
def coding_agent(task: str = "") -> str:
    """Start a session where Claude works as a coding agent on your machine."""
    prompt = CODING_AGENT_PROMPT + f"\nCurrent working directory: {session.cwd}\n"
    if task:
        prompt += f"\nTask:\n{task}\n"
    return prompt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=os.environ.get("CLAUDE_FREE_ROOT", str(Path.home())),
        help="Starting working directory (default: $CLAUDE_FREE_ROOT or your home directory)",
    )
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"--root {root} is not a directory")
    session.cwd = root
    atexit.register(shell.shutdown_jobs)
    server.run("stdio")


if __name__ == "__main__":
    main()
