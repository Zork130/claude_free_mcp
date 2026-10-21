"""Shell tools: run commands (foreground or background) in the platform's shell."""

from __future__ import annotations

import itertools
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..state import session

MAX_OUTPUT_CHARS = 30_000
MAX_TIMEOUT_SEC = 600
IS_WINDOWS = sys.platform == "win32"
_SENTINEL = f"__CLAUDE_FREE_CWD_{secrets.token_hex(4)}__"


def shell_name() -> str:
    if IS_WINDOWS:
        return "PowerShell"
    return "bash" if shutil.which("bash") else "sh"


def _argv(script: str) -> list[str]:
    if IS_WINDOWS:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", script]
    return [shutil.which("bash") or shutil.which("sh") or "/bin/sh", "-c", script]


def _wrap_with_cwd_probe(command: str) -> str:
    """Append a trailer that prints the final working directory, keeping the exit code."""
    if IS_WINDOWS:
        return (
            f"{command}\n"
            "$__ok = $?; $__ec = if ($LASTEXITCODE) { $LASTEXITCODE } elseif ($__ok) { 0 } else { 1 }\n"
            f'Write-Output ""; Write-Output "{_SENTINEL}$((Get-Location).Path)"\n'
            "exit $__ec"
        )
    return f'{command}\n__ec=$?\nprintf "\\n{_SENTINEL}%s\\n" "$(pwd)"\nexit $__ec'


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Keep tools non-interactive: no pagers, no credential prompts.
    env.update(PAGER="cat", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0")
    return env


def _popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n\n… [{len(text) - MAX_OUTPUT_CHARS} chars truncated] …\n\n{text[-half:]}"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


@dataclass
class Job:
    id: str
    command: str
    proc: subprocess.Popen
    log_path: Path
    read_pos: int = 0
    log_file: object = field(default=None, repr=False)


_jobs: dict[str, Job] = {}
_job_ids = itertools.count(1)


def _start_background(command: str) -> str:
    job_id = f"job{next(_job_ids)}"
    log_path = Path(tempfile.gettempdir()) / f"claude-free-{os.getpid()}-{job_id}.log"
    log = open(log_path, "wb")
    proc = subprocess.Popen(
        _argv(command),
        cwd=session.cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=_env(),
        **_popen_kwargs(),
    )
    _jobs[job_id] = Job(job_id, command, proc, log_path, log_file=log)
    return f"Started {job_id} (pid {proc.pid}) in {session.cwd}. Use job_output('{job_id}') to check on it."


def shell(command: str, timeout_sec: int = 120, run_in_background: bool = False) -> str:
    if run_in_background:
        return _start_background(command)
    timeout_sec = max(1, min(timeout_sec, MAX_TIMEOUT_SEC))
    proc = subprocess.Popen(
        _argv(_wrap_with_cwd_probe(command)),
        cwd=session.cwd,
        stdin=subprocess.DEVNULL,  # stdin is the MCP channel; never let commands read it
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_env(),
        **_popen_kwargs(),
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, _ = proc.communicate()
    text = _decode(out or b"")

    idx = text.rfind(_SENTINEL)
    if idx >= 0:
        new_cwd = text[idx + len(_SENTINEL) :].strip()
        text = text[:idx].rstrip("\n")
        if new_cwd and Path(new_cwd).is_dir():
            session.cwd = Path(new_cwd)

    parts = [_truncate(text) if text.strip() else "(no output)"]
    if timed_out:
        parts.append(f"[killed after {timeout_sec}s timeout; use run_in_background for long-running commands]")
    else:
        parts.append(f"[exit code {proc.returncode}]")
    parts.append(f"[cwd: {session.cwd}]")
    return "\n".join(parts)


def _get_job(job_id: str) -> Job:
    job = _jobs.get(job_id)
    if job is None:
        known = ", ".join(_jobs) or "none"
        raise ValueError(f"No job {job_id!r}. Known jobs: {known}.")
    return job


def job_output(job_id: str) -> str:
    job = _get_job(job_id)
    with open(job.log_path, "rb") as f:
        f.seek(job.read_pos)
        data = f.read()
        job.read_pos = f.tell()
    code = job.proc.poll()
    status = "running" if code is None else f"exited with code {code}"
    text = _decode(data)
    return f"[{job_id}: {status}]\n" + (_truncate(text) if text else "(no new output)")


def kill_job(job_id: str) -> str:
    job = _get_job(job_id)
    _kill_tree(job.proc)
    try:
        job.proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    return f"{job_id} stopped (exit code {job.proc.poll()})."


def list_jobs() -> str:
    if not _jobs:
        return "No background jobs."
    lines = []
    for job in _jobs.values():
        code = job.proc.poll()
        status = "running" if code is None else f"exited {code}"
        lines.append(f"{job.id} [{status}] {job.command}")
    return "\n".join(lines)


def shutdown_jobs() -> None:
    for job in _jobs.values():
        _kill_tree(job.proc)
        if job.log_file:
            job.log_file.close()
        try:
            job.log_path.unlink()
        except OSError:
            pass
