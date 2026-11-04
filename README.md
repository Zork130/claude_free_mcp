# claude-free-mcp

A local MCP server that gives **Claude Desktop** (including the free plan) a Claude Code-style
toolset, so it can read, search and edit files and run commands on your machine.

| Tool | What it does |
|---|---|
| `read_file` | Read a file with line numbers (`offset`/`limit` for big files) |
| `write_file` | Create a file, or overwrite one already read |
| `edit_file` / `multi_edit` | Exact-string replacement; refuses ambiguous or stale edits |
| `list_dir`, `glob`, `grep` | Explore the codebase (`grep` uses ripgrep if installed) |
| `shell` | Run commands (bash on macOS/Linux, PowerShell on Windows); `cd` persists |
| `job_output`, `kill_job`, `list_jobs` | Manage background jobs (dev servers, long builds) |
| `todo_write`, `todo_read` | Plan and track multi-step work |
| `web_fetch` | Fetch a URL as plain text |
| `browser_*` | Drive Edge/Chrome: tabs, navigate, read page, click, type, select, keys, scroll, wait, screenshot, run JS, console |

There is also a **`coding_agent` prompt** (Desktop's **+** → *Add from claude-free-mcp*)
that tells Claude to work like Claude Code: explore, plan, edit, verify, summarize.

> ⚠️ This gives Claude full access to your files and shell, just like Claude Code. Claude Desktop
> asks you to approve each tool call. Read what it's about to run before you click **Allow**,
> especially `shell` commands.

## Install

Requires Python 3.10+.

**macOS / Linux**
```bash
cd claude_free_mcp
python3 -m venv .venv
.venv/bin/pip install -e .
```

**Windows (PowerShell)**
```powershell
cd claude_free_mcp
py -m venv .venv
.venv\Scripts\pip install -e .
```

## Register with Claude Desktop

Open Claude Desktop → **Settings → Developer → Edit Config**. This opens
`claude_desktop_config.json`:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux (community builds): `~/.config/Claude/claude_desktop_config.json`

Add the server. Use **absolute paths**. `--root` is the directory Claude starts in.

macOS / Linux:
```json
{
  "mcpServers": {
    "claude-code": {
      "command": "/home/you/claude_free_mcp/.venv/bin/claude-free-mcp",
      "args": ["--root", "/home/you/projects"]
    }
  }
}
```

Windows (backslashes must be doubled in JSON):
```json
{
  "mcpServers": {
    "claude-code": {
      "command": "C:\\Users\\you\\claude_free_mcp\\.venv\\Scripts\\claude-free-mcp.exe",
      "args": ["--root", "C:\\Users\\you\\projects"]
    }
  }
}
```

Fully quit Claude Desktop (including the tray/menu-bar icon) and reopen it. The tools show up under
the 🔨 / connectors menu in the chat box.

## Using it

Start a chat with the **coding_agent** prompt, or just ask, for example:

> In ~/projects/myapp, find why `npm test` fails and fix it.

Tips:
- Free-plan usage limits still apply. Long agentic sessions use them up quickly, so give focused tasks.
- In the tool permission dialog, "Allow always" for the read-only tools (`read_file`, `grep`, `glob`,
  `list_dir`) saves clicks; leave `shell` and the edit tools on per-call approval.
- Server logs: Windows `%APPDATA%\Claude\logs\mcp-server-claude-code.log`,
  macOS `~/Library/Logs/Claude/mcp-server-claude-code.log`.

## Browser control

The `browser_*` tools drive **Edge or Chrome** through the Chrome DevTools Protocol, in pure
Python. They need no Node.js, Playwright or browser downloads.

- On first use, the server starts Edge (or Chrome if Edge isn't found) with remote debugging on
  port 9222, using a **separate profile** at `~/.claude-free-mcp/browser-profile`. Sign in there
  to any sites you want Claude to use; the profile remembers those sign-ins.
- If a browser is already listening on that port, for example one you started yourself with
  `msedge.exe --remote-debugging-port=9222 --user-data-dir=C:\temp\claude-edge`, the server uses it.
- Typical flow for Claude: `browser_navigate` → `browser_read_page(mode="interactive")`
  (numbered refs for links, buttons and fields) → `browser_click("3")` / `browser_type("1", "text")`.
- Pages inside iframes aren't reachable yet.

### Staying logged in

The browser profile keeps normal "remember me" logins on disk. Edge and Chrome discard
**session-only cookies** when they close, though, and many SSO and intranet logins use exactly
those. To keep them:

- **Automatic:** after each navigation, click, submit or key press, the server saves every
  cookie to `<profile>/claude-sessions/_autosave.json`. When it next launches the browser, it
  restores any cookies that are missing.
- **Named sessions:** ask Claude to "save this browser session as work". It calls
  `browser_save_session("work")`, and later `browser_load_session("work")` swaps those logins
  back in. Useful for switching between accounts. `browser_list_sessions` and
  `browser_delete_session` manage them.

> 🔒 Session files are **plain JSON containing your login cookies**. Anyone who can read them
> can use your logins. They live in your user profile folder (on macOS/Linux readable only by
> you), but don't copy, sync or commit them. Delete a session file to forget it.

Optional arguments (add them to `"args"` in the Desktop config):

| Argument | Default |
|---|---|
| `--browser PATH` | auto-detect, Edge first (or env `CLAUDE_FREE_BROWSER`) |
| `--browser-port N` | `9222` |
| `--browser-profile DIR` | `~/.claude-free-mcp/browser-profile` |
| `--browser-headless` | off (shows a window) |

If the browser can't be started or reached, your organisation may have turned off remote
debugging by policy (`RemoteDebuggingAllowed`).

## Development

```bash
.venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```
