"""Browser tools: drive Edge or Chrome over the Chrome DevTools Protocol (CDP).

Pure Python (websocket-client), no Node or browser downloads. The browser runs with
--remote-debugging-port in a dedicated profile; if nothing is listening on the port we
launch one ourselves.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import websocket

MAX_TEXT_CHARS = 30_000
MAX_INTERACTIVE = 300


@dataclass
class Config:
    port: int = 9222
    executable: str | None = None
    headless: bool = False
    profile_dir: Path = field(default_factory=lambda: Path.home() / ".claude-free-mcp" / "browser-profile")


config = Config()


def find_browser() -> str | None:
    """Locate Edge (preferred) or Chrome/Chromium."""
    if config.executable:
        return config.executable
    if env := os.environ.get("CLAUDE_FREE_BROWSER"):
        return env
    candidates: list[str] = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("microsoft-edge", "microsoft-edge-stable", "msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if found := shutil.which(name):
            return found
    return None


def _http(path: str, method: str = "GET") -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{config.port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _browser_running() -> bool:
    try:
        _http("/json/version")
        return True
    except (urllib.error.URLError, OSError):
        return False


def _ensure_browser() -> None:
    if _browser_running():
        return
    exe = find_browser()
    if not exe:
        raise ValueError(
            "No Edge or Chrome found. Start one yourself with "
            f"--remote-debugging-port={config.port}, or set CLAUDE_FREE_BROWSER to its path."
        )
    config.profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={config.port}",
        f"--user-data-dir={config.profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if config.headless:
        args.append("--headless=new")
    args.append("about:blank")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)
    deadline = time.time() + 20
    while time.time() < deadline:
        if _browser_running():
            # A fresh browser has lost its session-only cookies; put back what we saved.
            try:
                _set_cookies(_read_session_file(AUTOSAVE), only_missing=True)
            except (ValueError, OSError, websocket.WebSocketException):
                pass
            return
        time.sleep(0.25)
    raise ValueError(
        f"Started {exe} but its debugging port {config.port} never answered. "
        "Remote debugging may be disabled by policy."
    )


class Tab:
    """A CDP websocket session attached to one page target (or to the browser itself)."""

    def __init__(self, target_id: str, ws_url: str, domains: tuple[str, ...] = ("Page", "Runtime", "Log")) -> None:
        self.id = target_id
        self.ws = websocket.create_connection(ws_url, suppress_origin=True, enable_multithread=True)
        self._ids = itertools.count(1)
        self._pending: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.console: deque[str] = deque(maxlen=500)
        self.dead = False
        threading.Thread(target=self._reader, daemon=True).start()
        for domain in domains:
            self.send(f"{domain}.enable")

    def _reader(self) -> None:
        while True:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:  # noqa: BLE001 - any failure means the socket is gone
                with self._cond:
                    self.dead = True
                    self._cond.notify_all()
                return
            if "id" in msg:
                with self._cond:
                    self._pending[msg["id"]] = msg
                    self._cond.notify_all()
            else:
                self._on_event(msg.get("method", ""), msg.get("params", {}))

    def _on_event(self, method: str, params: dict) -> None:
        if method == "Runtime.consoleAPICalled":
            parts = []
            for a in params.get("args", []):
                parts.append(str(a["value"]) if "value" in a else a.get("description", a.get("type", "")))
            self.console.append(f"[{params.get('type', 'log')}] {' '.join(parts)}")
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            self.console.append(f"[exception] {d.get('exception', {}).get('description') or d.get('text', '')}")
        elif method == "Log.entryAdded":
            e = params.get("entry", {})
            self.console.append(f"[{e.get('level', 'log')}] {e.get('text', '')} {e.get('url', '')}".rstrip())

    def send(self, method: str, params: dict | None = None, timeout: float = 30) -> dict:
        if self.dead:
            raise ValueError("Connection to the tab was lost.")
        mid = next(self._ids)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        with self._cond:
            if not self._cond.wait_for(lambda: mid in self._pending or self.dead, timeout):
                raise ValueError(f"{method} timed out after {timeout}s.")
            if mid not in self._pending:
                raise ValueError("Connection to the tab was lost.")
            msg = self._pending.pop(mid)
        if "error" in msg:
            raise ValueError(f"{method} failed: {msg['error'].get('message')}")
        return msg.get("result", {})

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


_lock = threading.RLock()
_tabs: dict[str, Tab] = {}
_current: str | None = None
_browser_session: Tab | None = None


def _browser_cdp() -> Tab:
    """CDP session on the browser target, for browser-wide calls such as cookies."""
    global _browser_session
    if _browser_session is None or _browser_session.dead:
        ws_url = json.loads(_http("/json/version"))["webSocketDebuggerUrl"]
        _browser_session = Tab("browser", ws_url, domains=())
    return _browser_session


# ------------------------------------------------------------------ saved sessions
# Cookies are saved as plain JSON next to the profile. Session-only cookies (the ones a
# browser restart throws away) are included, which is what keeps SSO logins alive.

AUTOSAVE = "_autosave"
_COOKIE_PARAM_KEYS = (
    "name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "expires",
    "priority", "sourceScheme", "sourcePort", "partitionKey",
)


def _sessions_dir() -> Path:
    return config.profile_dir / "claude-sessions"


def _session_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("Session names may only use letters, digits, '-' and '_' (max 64).")
    return _sessions_dir() / f"{name}.json"


def _read_session_file(name: str) -> list[dict]:
    path = _session_path(name)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["cookies"]


def _get_cookies() -> list[dict]:
    return _browser_cdp().send("Storage.getCookies").get("cookies", [])


def _set_cookies(cookies: list[dict], only_missing: bool = False) -> int:
    now = time.time()
    if only_missing:
        have = {(c["name"], c["domain"], c["path"]) for c in _get_cookies()}
        cookies = [c for c in cookies if (c["name"], c["domain"], c["path"]) not in have]
    params = []
    for c in cookies:
        if not c.get("session") and 0 < c.get("expires", -1) < now:
            continue  # expired
        p = {k: c[k] for k in _COOKIE_PARAM_KEYS if k in c}
        if c.get("session"):
            p.pop("expires", None)
        params.append(p)
    if params:
        _browser_cdp().send("Storage.setCookies", {"cookies": params})
    return len(params)


def _write_session(name: str) -> int:
    cookies = _get_cookies()
    path = _session_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"saved_at": time.time(), "cookies": cookies}), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return len(cookies)


def _autosave() -> None:
    """Best-effort cookie snapshot after actions that may log in or out."""
    try:
        _write_session(AUTOSAVE)
    except (ValueError, OSError, websocket.WebSocketException):
        pass


def _page_targets() -> list[dict]:
    return [t for t in json.loads(_http("/json/list")) if t.get("type") == "page"]


def _connect(target: dict) -> Tab:
    tab = _tabs.get(target["id"])
    if tab is None or tab.dead:
        tab = Tab(target["id"], target["webSocketDebuggerUrl"])
        _tabs[target["id"]] = tab
    return tab


def _current_tab() -> Tab:
    global _current
    _ensure_browser()
    targets = _page_targets()
    if not targets:
        targets = [json.loads(_http("/json/new?about:blank", method="PUT"))]
    match = next((t for t in targets if t["id"] == _current), None)
    if match is None:
        match = targets[0]
        _current = match["id"]
    return _connect(match)


def _find_target(tab_id: str) -> dict:
    matches = [t for t in _page_targets() if t["id"].startswith(tab_id.upper()) or t["id"].startswith(tab_id)]
    if len(matches) != 1:
        raise ValueError(f"No unique tab with id {tab_id!r}. Use browser_tabs to list tabs.")
    return matches[0]


def _eval(tab: Tab, expression: str, timeout: float = 30):
    r = tab.send(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True},
        timeout=timeout,
    )
    if "exceptionDetails" in r:
        d = r["exceptionDetails"]
        raise ValueError(f"JavaScript error: {d.get('exception', {}).get('description') or d.get('text')}")
    return r.get("result", {}).get("value")


def _wait_ready(tab: Tab, timeout: float = 15) -> bool:
    """Wait until the current document has finished loading. Tolerates navigation mid-wait."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _eval(tab, "document.readyState", timeout=5) == "complete":
                return True
        except ValueError:
            pass  # context destroyed while navigating
        time.sleep(0.2)
    return False


def _settle(tab: Tab) -> None:
    """Give a click or key press time to trigger navigation or re-rendering."""
    time.sleep(0.4)
    _wait_ready(tab, timeout=10)


def _header(tab: Tab) -> str:
    info = _eval(tab, "({title: document.title, url: location.href})") or {}
    return f"Page: {info.get('title', '')}\nURL: {info.get('url', '')}"


def _selector_for(target: str) -> str:
    target = target.strip()
    return f'[data-claude-ref="{target}"]' if target.isdigit() else target


def _locate(tab: Tab, target: str) -> dict:
    sel = json.dumps(_selector_for(target))
    point = _eval(
        tab,
        f"""(() => {{
            const el = document.querySelector({sel});
            if (!el) return null;
            el.scrollIntoView({{block: 'center', inline: 'center'}});
            const r = el.getBoundingClientRect();
            return {{x: r.left + r.width / 2, y: r.top + r.height / 2}};
        }})()""",
    )
    if point is None:
        raise ValueError(f"No element matches {target!r}. Call browser_read_page(mode='interactive') to refresh refs.")
    return point


# ---------------------------------------------------------------------------- tools


def tabs() -> str:
    with _lock:
        current = _current_tab()
        lines = []
        for t in _page_targets():
            mark = "*" if t["id"] == current.id else " "
            lines.append(f"{mark} {t['id'][:8]}  {t.get('title', '')[:60]}  {t.get('url', '')}")
        return "\n".join(lines) + "\n(* = selected tab)"


def new_tab(url: str = "about:blank") -> str:
    global _current
    with _lock:
        _ensure_browser()
        target = json.loads(_http(f"/json/new?{urllib.parse.quote(url, safe=':/?&=#%')}", method="PUT"))
        _current = target["id"]
        tab = _connect(target)
        _wait_ready(tab)
        return f"Opened tab {target['id'][:8]}.\n{_header(tab)}"


def select_tab(tab_id: str) -> str:
    global _current
    with _lock:
        _ensure_browser()
        target = _find_target(tab_id)
        _current = target["id"]
        _http(f"/json/activate/{target['id']}")
        return f"Selected tab {target['id'][:8]}.\n{_header(_connect(target))}"


def close_tab(tab_id: str) -> str:
    global _current
    with _lock:
        _ensure_browser()
        target = _find_target(tab_id)
        if tab := _tabs.pop(target["id"], None):
            tab.close()
        _http(f"/json/close/{target['id']}")
        if _current == target["id"]:
            _current = None
        return f"Closed tab {target['id'][:8]}."


def navigate(url: str) -> str:
    with _lock:
        tab = _current_tab()
        if url in ("back", "forward"):
            _eval(tab, f"history.{url}()")
            _settle(tab)
        elif url == "reload":
            tab.send("Page.reload")
            _settle(tab)
        else:
            if "://" not in url and not url.startswith(("about:", "data:", "file:")):
                url = "https://" + url
            r = tab.send("Page.navigate", {"url": url})
            if r.get("errorText"):
                raise ValueError(f"Navigation failed: {r['errorText']}")
            if r.get("loaderId"):
                time.sleep(0.2)
                if not _wait_ready(tab, timeout=30):
                    return f"Page still loading after 30s.\n{_header(tab)}"
        _autosave()
        return _header(tab)


_INTERACTIVE_JS = """(() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link],'
    + '[role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=combobox], [role=textbox],'
    + '[role=switch], [onclick], [contenteditable=""], [contenteditable=true], [tabindex]:not([tabindex="-1"])';
  document.querySelectorAll('[data-claude-ref]').forEach(e => e.removeAttribute('data-claude-ref'));
  const clean = s => (s || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width === 0 || r.height === 0 || st.visibility === 'hidden' || st.display === 'none') continue;
    const ref = ++n;
    el.setAttribute('data-claude-ref', ref);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'input' ? 'input[' + (el.type || 'text') + ']' : tag);
    const label = clean(el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText)
      || (tag === 'select' ? '' : el.innerText) || el.title || el.alt || el.name || el.id);
    let extra = '';
    if (tag === 'a') extra += ' -> ' + (el.getAttribute('href') || '').slice(0, 100);
    if (el.placeholder) extra += ' placeholder="' + clean(el.placeholder) + '"';
    if ((tag === 'input' || tag === 'textarea') && el.value && el.type !== 'password') extra += ' value="' + clean(el.value) + '"';
    if (el.type === 'checkbox' || el.type === 'radio') extra += el.checked ? ' [checked]' : ' [unchecked]';
    if (tag === 'select') extra += ' selected="' + clean(el.selectedOptions[0] && el.selectedOptions[0].text) + '"';
    if (el.disabled) extra += ' [disabled]';
    if (r.bottom < 0 || r.top > innerHeight) extra += ' (offscreen)';
    out.push('[' + ref + '] ' + role + ' "' + label + '"' + extra);
    if (n >= LIMIT) { out.push('[more elements not listed]'); break; }
  }
  return out.join('\\n') || '(no interactive elements found)';
})()"""


def read_page(mode: str = "text", max_chars: int = MAX_TEXT_CHARS) -> str:
    if mode not in ("text", "interactive"):
        raise ValueError("mode must be 'text' or 'interactive'.")
    with _lock:
        tab = _current_tab()
        if mode == "interactive":
            body = _eval(tab, _INTERACTIVE_JS.replace("LIMIT", str(MAX_INTERACTIVE)))
        else:
            body = _eval(tab, "document.body ? document.body.innerText : ''") or ""
            if len(body) > max_chars:
                body = body[:max_chars] + f"\n\n[truncated; {len(body) - max_chars} more chars]"
        return f"{_header(tab)}\n\n{body}"


def click(target: str) -> str:
    with _lock:
        tab = _current_tab()
        p = _locate(tab, target)
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            params = {"type": kind, "x": p["x"], "y": p["y"]}
            if kind != "mouseMoved":
                params |= {"button": "left", "clickCount": 1}
            tab.send("Input.dispatchMouseEvent", params)
        _settle(tab)
        _autosave()
        return f"Clicked {target}.\n{_header(tab)}"


_KEYS = {
    "Enter": ("Enter", 13, "\r"),
    "Tab": ("Tab", 9, None),
    "Escape": ("Escape", 27, None),
    "Backspace": ("Backspace", 8, None),
    "Delete": ("Delete", 46, None),
    "ArrowUp": ("ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", 39, None),
    "PageUp": ("PageUp", 33, None),
    "PageDown": ("PageDown", 34, None),
    "Home": ("Home", 36, None),
    "End": ("End", 35, None),
    "Space": ("Space", 32, " "),
}


def _press(tab: Tab, key: str) -> None:
    name = next((k for k in _KEYS if k.lower() == key.lower()), None)
    if name is None:
        raise ValueError(f"Unsupported key {key!r}. Supported: {', '.join(_KEYS)}.")
    code, keycode, text = _KEYS[name]
    down = {"type": "keyDown" if text else "rawKeyDown", "key": " " if name == "Space" else name, "code": code,
            "windowsVirtualKeyCode": keycode}
    if text:
        down["text"] = text
    tab.send("Input.dispatchKeyEvent", down)
    tab.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": down["key"], "code": code, "windowsVirtualKeyCode": keycode})


def type_text(target: str, text: str, clear: bool = True, submit: bool = False) -> str:
    with _lock:
        tab = _current_tab()
        _locate(tab, target)
        sel = json.dumps(_selector_for(target))
        _eval(
            tab,
            f"""(() => {{
                const el = document.querySelector({sel});
                el.focus();
                if ({'true' if clear else 'false'}) {{
                    if (typeof el.select === 'function') el.select();
                    else document.execCommand('selectAll');
                }}
            }})()""",
        )
        if text:
            tab.send("Input.insertText", {"text": text})
        elif clear:
            _press(tab, "Backspace")
        if submit:
            _press(tab, "Enter")
            _settle(tab)
            _autosave()
        return f"Typed into {target}." + (f"\n{_header(tab)}" if submit else "")


def press_key(key: str) -> str:
    with _lock:
        tab = _current_tab()
        _press(tab, key)
        _settle(tab)
        _autosave()
        return f"Pressed {key}."


def select_option(target: str, option: str) -> str:
    with _lock:
        tab = _current_tab()
        sel, opt = json.dumps(_selector_for(target)), json.dumps(option)
        chosen = _eval(
            tab,
            f"""(() => {{
                const el = document.querySelector({sel});
                if (!el || el.tagName !== 'SELECT') return {{error: 'not a <select> element'}};
                const o = [...el.options].find(o => o.value === {opt} || o.text.trim() === {opt});
                if (!o) return {{error: 'no option ' + {opt} + '; options: ' + [...el.options].map(o => o.text.trim()).join(', ')}};
                el.value = o.value;
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                return {{text: o.text.trim()}};
            }})()""",
        )
        if chosen is None:
            raise ValueError(f"No element matches {target!r}.")
        if "error" in chosen:
            raise ValueError(chosen["error"])
        return f"Selected {chosen['text']!r} in {target}."


def scroll(direction: str = "down", pages: float = 1.0) -> str:
    if direction not in ("up", "down", "top", "bottom"):
        raise ValueError("direction must be 'up', 'down', 'top' or 'bottom'.")
    with _lock:
        tab = _current_tab()
        js = {
            "down": f"window.scrollBy(0, innerHeight * 0.8 * {pages})",
            "up": f"window.scrollBy(0, -innerHeight * 0.8 * {pages})",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.documentElement.scrollHeight)",
        }[direction]
        pos = _eval(tab, f"{js}; ({{y: Math.round(scrollY), h: document.documentElement.scrollHeight, vh: innerHeight}})")
        return f"Scrolled {direction}. Now at {pos['y']}px of {pos['h']}px (viewport {pos['vh']}px)."


def wait_for(text: str | None = None, timeout_sec: float = 10) -> str:
    timeout_sec = max(0.0, min(timeout_sec, 60))
    with _lock:
        tab = _current_tab()
        if not text:
            time.sleep(timeout_sec)
            return f"Waited {timeout_sec}s."
        needle = json.dumps(text)
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                if _eval(tab, f"!!document.body && document.body.innerText.includes({needle})", timeout=5):
                    return f"Found {text!r}."
            except ValueError:
                pass
            time.sleep(0.25)
        raise ValueError(f"{text!r} did not appear within {timeout_sec}s.")


def screenshot() -> bytes:
    with _lock:
        tab = _current_tab()
        r = tab.send("Page.captureScreenshot", {"format": "jpeg", "quality": 75})
        return base64.b64decode(r["data"])


def evaluate(expression: str) -> str:
    with _lock:
        tab = _current_tab()
        value = _eval(tab, expression)
        if value is None:
            return "undefined"
        return value if isinstance(value, str) else json.dumps(value, indent=2, default=str)


def save_session(name: str) -> str:
    with _lock:
        _ensure_browser()
        count = _write_session(name)
        return f"Saved {count} cookies as session {name!r} ({_session_path(name)})."


def load_session(name: str) -> str:
    with _lock:
        _ensure_browser()
        cookies = _read_session_file(name)
        if not cookies:
            raise ValueError(f"No saved session {name!r}. Use browser_list_sessions to see saved ones.")
        count = _set_cookies(cookies)
        tab = _current_tab()
        if _eval(tab, "location.protocol").startswith("http"):
            tab.send("Page.reload")
            _settle(tab)
        _autosave()
        return f"Restored {count} cookies from session {name!r} and reloaded the tab."


def list_sessions() -> str:
    d = _sessions_dir()
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    if not files:
        return "No saved sessions."
    lines = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        domains = sorted({c["domain"].lstrip(".") for c in data.get("cookies", [])})
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("saved_at", f.stat().st_mtime)))
        shown = ", ".join(domains[:8]) + (f" (+{len(domains) - 8} more)" if len(domains) > 8 else "")
        label = f"{f.stem} (automatic)" if f.stem == AUTOSAVE else f.stem
        lines.append(f"{label}  saved {when}  {len(data.get('cookies', []))} cookies  {shown}")
    return "\n".join(lines)


def delete_session(name: str) -> str:
    path = _session_path(name)
    if not path.exists():
        raise ValueError(f"No saved session {name!r}.")
    path.unlink()
    return f"Deleted session {name!r}."


def close_browser() -> None:
    """Quit the browser listening on the debugging port and forget all tab sessions."""
    global _current, _browser_session
    with _lock:
        for tab in _tabs.values():
            tab.close()
        _tabs.clear()
        _current = None
        if not _browser_running():
            return
        _autosave()
        session = _browser_cdp()
        try:
            session.send("Browser.close", timeout=5)
        except ValueError:
            pass  # the browser may drop the socket as it exits
        session.close()
        _browser_session = None
        deadline = time.time() + 10
        while _browser_running() and time.time() < deadline:
            time.sleep(0.2)


def console(clear: bool = True) -> str:
    with _lock:
        tab = _current_tab()
        msgs = list(tab.console)
        if clear:
            tab.console.clear()
        return "\n".join(msgs) if msgs else "(no console messages since the tab was attached or last checked)"
