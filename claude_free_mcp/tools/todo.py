"""A simple in-memory todo list the model uses to plan and track multi-step work."""

from __future__ import annotations

STATUSES = ("pending", "in_progress", "completed")
_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}

_todos: list[dict] = []


def _render() -> str:
    if not _todos:
        return "Todo list is empty."
    return "\n".join(f"{_MARKS[t['status']]} {t['content']}" for t in _todos)


def todo_write(todos: list[dict]) -> str:
    cleaned = []
    for i, t in enumerate(todos, 1):
        content = str(t.get("content", "")).strip()
        status = t.get("status", "pending")
        if not content:
            raise ValueError(f"Todo {i} has no content.")
        if status not in STATUSES:
            raise ValueError(f"Todo {i} has invalid status {status!r}; use one of {', '.join(STATUSES)}.")
        cleaned.append({"content": content, "status": status})
    _todos[:] = cleaned
    return _render()


def todo_read() -> str:
    return _render()
