"""Browser tool tests. They drive a real headless Edge/Chrome and skip if none is installed."""

import http.server
import shutil
import socket
import threading

import pytest

from claude_free_mcp.tools import browser

PAGE = """<!doctype html><title>Test Form</title>
<label for="name">Your name</label> <input id="name">
<select id="color"><option value="r">Red</option><option value="g">Green</option></select>
<button onclick="const $ = id => document.getElementById(id);
  $('out').textContent = 'Hi ' + $('name').value + ' / ' + $('color').value; console.log('clicked')">Greet</button>
<p id="out"></p>"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    if not browser.find_browser():
        pytest.skip("no Edge/Chrome installed")
    tmp = tmp_path_factory.mktemp("browser")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    old = browser.config
    browser.config = browser.Config(port=port, headless=True, profile_dir=tmp / "profile")
    (tmp / "page.html").write_text(PAGE)
    yield (tmp / "page.html").as_uri()
    browser.close_browser()
    browser.config = old


def test_form_interaction(page):
    assert "Test Form" in browser.navigate(page)
    refs = browser.read_page("interactive")
    assert '[1] input[text] "Your name"' in refs
    assert 'select "color" selected="Red"' in refs
    browser.type_text("1", "Chris")
    browser.select_option("#color", "Green")
    browser.click("3")
    assert "Hi Chris / g" in browser.read_page("text")
    assert "[log] clicked" in browser.console()
    assert browser.screenshot()[:2] == b"\xff\xd8"  # JPEG magic


class _CookieHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path == "/login":
            self.send_header("Set-Cookie", "sid=abc123; Path=/")  # session-only cookie
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<title>t</title><p>cookies: {self.headers.get('Cookie', '')}</p>".encode())

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CookieHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_session_cookies_survive_restart(page, site):
    browser.navigate(f"{site}/login")
    browser.navigate(site)
    assert "sid=abc123" in browser.read_page("text")
    browser.close_browser()  # autosaves on the way out
    autosave = browser._session_path(browser.AUTOSAVE)
    backup = autosave.with_suffix(".bak")
    shutil.copy(autosave, backup)
    autosave.unlink()

    # Without the saved file, a restarted browser has lost the session cookie...
    browser.navigate(site)
    assert "sid=abc123" not in browser.read_page("text")
    browser.close_browser()

    # ...and with it, the cookie is restored on launch.
    shutil.copy(backup, autosave)
    browser.navigate(site)
    assert "sid=abc123" in browser.read_page("text")


def test_named_sessions(page, site):
    browser.navigate(f"{site}/login")
    assert "Saved" in browser.save_session("work")
    assert "work" in browser.list_sessions()
    browser._browser_cdp().send("Storage.clearCookies")
    browser.navigate(site)
    assert "sid=abc123" not in browser.read_page("text")
    assert "Restored" in browser.load_session("work")
    assert "sid=abc123" in browser.read_page("text")
    browser.delete_session("work")
    assert "work " not in browser.list_sessions()
    with pytest.raises(ValueError, match="Session names"):
        browser.save_session("../evil")


def test_errors_are_readable(page):
    browser.navigate(page)
    with pytest.raises(ValueError, match="No element matches"):
        browser.click("#missing")
    with pytest.raises(ValueError, match="JavaScript error"):
        browser.evaluate("null.x")
