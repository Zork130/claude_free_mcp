import time
from pathlib import Path

import pytest

from claude_free_mcp.state import session
from claude_free_mcp.tools import files, search, shell, todo


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "cwd", tmp_path)
    session.read_files.clear()
    return tmp_path


def test_read_numbers_lines_and_pages(workdir):
    (workdir / "a.txt").write_text("one\ntwo\nthree\n")
    assert files.read_file("a.txt") == "1\tone\n2\ttwo\n3\tthree"
    out = files.read_file("a.txt", offset=2, limit=1)
    assert out.startswith("2\ttwo") and "lines 2-2 of 3" in out


def test_write_requires_read_before_overwrite(workdir):
    assert "Created" in files.write_file("new/b.txt", "hi\n")
    (workdir / "c.txt").write_text("x")
    with pytest.raises(ValueError, match="Read"):
        files.write_file("c.txt", "y")
    files.read_file("c.txt")
    files.write_file("c.txt", "y")
    assert (workdir / "c.txt").read_text() == "y"


def test_edit_uniqueness_and_replace_all(workdir):
    p = workdir / "d.py"
    p.write_text("x = 1\nx = 1\n")
    files.read_file("d.py")
    with pytest.raises(ValueError, match="2 times"):
        files.edit_file("d.py", "x = 1", "x = 2")
    with pytest.raises(ValueError, match="not found"):
        files.edit_file("d.py", "y = 1", "y = 2")
    files.edit_file("d.py", "x = 1", "x = 2", replace_all=True)
    assert p.read_text() == "x = 2\nx = 2\n"


def test_edit_detects_external_change(workdir):
    p = workdir / "e.txt"
    p.write_text("a")
    files.read_file("e.txt")
    time.sleep(0.01)
    p.write_text("b")
    with pytest.raises(ValueError, match="changed"):
        files.edit_file("e.txt", "b", "c")


def test_edit_preserves_crlf(workdir):
    p = workdir / "w.txt"
    p.write_bytes(b"one\r\ntwo\r\n")
    files.read_file("w.txt")
    files.edit_file("w.txt", "one\ntwo", "1\n2")
    assert p.read_bytes() == b"1\r\n2\r\n"


def test_multi_edit_is_atomic(workdir):
    p = workdir / "f.txt"
    p.write_text("alpha beta")
    files.read_file("f.txt")
    with pytest.raises(ValueError, match="Edit 2"):
        files.multi_edit("f.txt", [{"old_string": "alpha", "new_string": "A"}, {"old_string": "zzz", "new_string": "Z"}])
    assert p.read_text() == "alpha beta"
    files.multi_edit("f.txt", [{"old_string": "alpha", "new_string": "A"}, {"old_string": "beta", "new_string": "B"}])
    assert p.read_text() == "A B"


def test_binary_refused(workdir):
    (workdir / "bin").write_bytes(b"\0\1\2")
    with pytest.raises(ValueError, match="binary"):
        files.read_file("bin")


def test_glob_skips_vendored_dirs(workdir):
    (workdir / "src").mkdir()
    (workdir / "src" / "m.py").write_text("")
    (workdir / "node_modules").mkdir()
    (workdir / "node_modules" / "x.py").write_text("")
    out = search.glob("**/*.py")
    assert "m.py" in out and "node_modules" not in out


@pytest.mark.parametrize("use_rg", [True, False])
def test_grep_modes(workdir, monkeypatch, use_rg):
    if not use_rg:
        monkeypatch.setattr(search.shutil, "which", lambda _: None)
    elif not search.shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    (workdir / "g.py").write_text("def foo():\n    return 1\n")
    (workdir / "h.txt").write_text("nothing\n")
    assert search.grep("foo").strip().endswith("g.py")
    assert "g.py:1" in search.grep("FOO", output_mode="count", case_insensitive=True)
    content = search.grep("return", output_mode="content", context=1)
    assert "def foo" in content and "return 1" in content
    assert search.grep("foo", glob="*.txt") == "No matches found."


def test_shell_runs_and_keeps_cwd(workdir):
    (workdir / "sub").mkdir()
    out = shell.shell("echo hello")
    assert "hello" in out and "[exit code 0]" in out
    shell.shell("cd sub")
    assert session.cwd == workdir / "sub"
    assert "[exit code 3]" in shell.shell("exit 3")


def test_shell_timeout(workdir):
    start = time.time()
    out = shell.shell("sleep 30", timeout_sec=1)
    assert "timeout" in out and time.time() - start < 10


def test_shell_cannot_read_stdin(workdir):
    out = shell.shell("cat", timeout_sec=5)
    assert "[exit code 0]" in out


def test_background_job_lifecycle(workdir):
    msg = shell.shell("echo started; sleep 30", run_in_background=True)
    job_id = msg.split()[1]
    for _ in range(50):
        if "started" in shell.job_output(job_id):
            break
        time.sleep(0.1)
    else:
        pytest.fail("background output never appeared")
    assert "running" in shell.list_jobs()
    assert "stopped" in shell.kill_job(job_id)
    assert "exited" in shell.job_output(job_id)


def test_windows_shell_selection(monkeypatch):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell.shutil, "which", lambda name: r"C:\pwsh.exe" if name == "pwsh" else None)
    argv = shell._argv("dir")
    assert argv[0] == r"C:\pwsh.exe" and "-Command" in argv
    assert "Get-Location" in shell._wrap_with_cwd_probe("dir")
    assert shell.shell_name() == "PowerShell"


def test_todo_validation():
    with pytest.raises(ValueError, match="invalid status"):
        todo.todo_write([{"content": "x", "status": "done"}])
    out = todo.todo_write([{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}])
    assert out == "[x] a\n[~] b"
