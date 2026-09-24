"""
acc_mcp.py — a read-only MCP server that lets Claude Desktop answer questions about an
Autodesk Construction Cloud (ACC) project: RFIs, issues, submittals, cost, members, and
Docs folders.

Settings live in .env next to this file (copy .env.example). The Autodesk sign-in is
saved to .mcp_token.json next to this file; Autodesk rotates its refresh token on every
renewal, so never copy that file to another machine or share it with another program.

    python acc_mcp.py --test    # one-time browser sign-in + check that data comes back
    python acc_mcp.py --login   # sign in only
    python acc_mcp.py           # MCP server over stdio (Claude Desktop runs this)
"""
import asyncio
import contextlib
import csv
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
load_dotenv(ENV_FILE, override=True)

# ── Settings ───────────────────────────────────────────────────────────────
APS_CLIENT_ID     = os.environ.get("APS_CLIENT_ID", "").strip()
APS_CLIENT_SECRET = os.environ.get("APS_CLIENT_SECRET", "").strip()
CALLBACK_URL      = os.environ.get("APS_CALLBACK_URL", "http://localhost:5002/callback").strip()
DEFAULT_PROJECT   = os.environ.get("DEFAULT_PROJECT_ID", "").strip()
TOKENS_FILE       = HERE / ".mcp_token.json"
LOCK_FILE         = HERE / ".mcp_token.lock"
SCOPES            = "data:read account:read"   # read-only on purpose

APS        = "https://developer.api.autodesk.com"
AUTH_URL   = f"{APS}/authentication/v2/authorize"
TOKEN_URL  = f"{APS}/authentication/v2/token"

EXPORT_DIR   = HERE / "exports"
MAX_ROWS_OUT = 200     # rows returned into the chat; CSV export always has all
MAX_ITEMS    = 5000    # safety cap per list call

READ_ONLY = ToolAnnotations(readOnlyHint=True)
mcp = FastMCP("Autodesk Connector")


def _log(msg: str) -> None:
    # stdout is the MCP channel — anything human-readable goes to stderr.
    print(msg, file=sys.stderr, flush=True)


def _is_placeholder(value: str) -> bool:
    return not value or "<" in value or "REPLACE" in value.upper()


def _settings_problem() -> str | None:
    """A plain-English description of what's missing in .env, or None if it looks usable."""
    if not ENV_FILE.exists():
        return (f"No .env file found in {HERE}. Copy .env.example to .env and fill in your "
                "APS client ID, client secret, and project ID.")
    missing = [name for name, value in (("APS_CLIENT_ID", APS_CLIENT_ID),
                                        ("APS_CLIENT_SECRET", APS_CLIENT_SECRET))
               if _is_placeholder(value)]
    if missing:
        return (f"Open {ENV_FILE} in Notepad and fill in {' and '.join(missing)} "
                "(from your app in the APS developer portal), then save.")
    return None


# ── Tokens ─────────────────────────────────────────────────────────────────
_tokens: dict = {}
_refresh_lock = asyncio.Lock()
_state: str | None = None
_server: ThreadingHTTPServer | None = None


class NeedsSignIn(Exception):
    """Message is the Autodesk sign-in URL."""


@contextlib.contextmanager
def _token_file_lock():
    """Cross-process lock around reading/renewing the sign-in.

    Claude Desktop can run more than one copy of this server at once. Autodesk
    invalidates a refresh token the moment it is used, so two copies renewing at
    the same time would log each other out. Holding this lock while renewing — and
    re-reading the file first — means only one copy renews and the others reuse it.
    """
    LOCK_FILE.touch(exist_ok=True)
    with open(LOCK_FILE, "r+") as fh:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _load_tokens() -> None:
    """Replace the in-memory sign-in with what's on disk (another copy may have renewed it)."""
    if TOKENS_FILE.exists():
        try:
            data = json.loads(TOKENS_FILE.read_text())
        except Exception:
            _log(f"Ignoring unreadable {TOKENS_FILE.name}")
            return
        _tokens.clear()
        _tokens.update(data)


def _write_tokens(data: dict) -> None:
    _tokens["access_token"] = data["access_token"]
    _tokens["refresh_token"] = data.get("refresh_token", _tokens.get("refresh_token", ""))
    _tokens["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_tokens, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, TOKENS_FILE)


def _access_token_valid() -> bool:
    return bool(_tokens.get("access_token")) and time.time() < _tokens.get("expires_at", 0) - 60


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def address_string(self):
        return self.client_address[0]  # skip blocking reverse-DNS lookup

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = params.get("code", [None])[0]
        if params.get("error"):
            status, msg = 400, f"Autodesk returned an error: {params['error'][0]}"
        elif not code or params.get("state", [None])[0] != _state:
            status, msg = 400, "Sign-in link expired or invalid. Run the sign-in again for a new one."
        else:
            try:
                r = httpx.post(
                    TOKEN_URL,
                    data={"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK_URL},
                    auth=(APS_CLIENT_ID, APS_CLIENT_SECRET),
                    timeout=30,
                )
                r.raise_for_status()
                with _token_file_lock():
                    _write_tokens(r.json())
                status, msg = 200, "Signed in to Autodesk. You can close this tab."
            except Exception as exc:
                status, msg = 500, "Token exchange failed — check APS_CLIENT_ID / APS_CLIENT_SECRET in .env."
                _log(f"Token exchange failed: {exc}")
        body = f"<html><body style='font-family:Segoe UI,Arial;padding:48px'><h2>{msg}</h2></body></html>"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


def _open_browser(url: str) -> None:
    # On Windows (and WSL) hand the URL to the default Windows browser.
    if shutil.which("powershell.exe"):
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _start_sign_in() -> str:
    global _state, _server
    _state = secrets.token_urlsafe(16)
    if _server is None:
        port = urllib.parse.urlparse(CALLBACK_URL).port or 80
        try:
            # localhost only — the browser on this machine is the only thing that calls it.
            _server = ThreadingHTTPServer(("127.0.0.1", port), _CallbackHandler)
        except OSError as exc:
            raise RuntimeError(
                f"Port {port} is already in use, so the Autodesk sign-in can't finish. Close the "
                f"program using it (PowerShell: netstat -ano | findstr :{port}), then try again."
            ) from exc
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": APS_CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "scope": SCOPES,
        "state": _state,
    })
    _open_browser(url)
    return url


async def _get_token() -> str:
    problem = _settings_problem()
    if problem:
        raise RuntimeError(problem)
    if _access_token_valid():
        return _tokens["access_token"]
    async with _refresh_lock:
        with _token_file_lock():
            _load_tokens()   # another copy may already have renewed
            if _access_token_valid():
                return _tokens["access_token"]
            if _tokens.get("refresh_token"):
                async with httpx.AsyncClient(timeout=30) as client:
                    r = await client.post(
                        TOKEN_URL,
                        data={"grant_type": "refresh_token", "refresh_token": _tokens["refresh_token"], "scope": SCOPES},
                        auth=(APS_CLIENT_ID, APS_CLIENT_SECRET),
                    )
                if r.status_code == 200:
                    _write_tokens(r.json())
                    return _tokens["access_token"]
                _log(f"Refresh failed ({r.status_code}); a new sign-in is needed.")
    raise NeedsSignIn(_start_sign_in())


# ── HTTP helpers ───────────────────────────────────────────────────────────
def _bare(project_id: str) -> str:
    """ACC module APIs want the project ID without Data Management's 'b.'."""
    return project_id.strip().removeprefix("b.")


def _prefixed(project_id: str) -> str:
    pid = project_id.strip()
    return pid if pid.startswith("b.") else f"b.{pid}"


def _project(project_id: str) -> str:
    pid = (project_id or DEFAULT_PROJECT).strip()
    if _is_placeholder(pid) or pid == "b.":
        raise ValueError("No project selected. Pass a project_id, or set DEFAULT_PROJECT_ID in .env "
                         "(the ID from your ACC project's address bar, with 'b.' in front).")
    return pid


async def _request(method: str, url: str, params: dict | None = None, json_body: dict | None = None) -> dict:
    token = await _get_token()
    if url.startswith("/"):
        url = APS + url
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                 params=params, json=json_body)
    r.raise_for_status()
    return r.json() if r.content else {}


async def _get_offset_paged(path: str, params: dict | None = None, page_size: int = 100) -> list[dict]:
    """limit/offset paging used by Issues, Submittals, Cost and Admin APIs."""
    items: list[dict] = []
    while True:
        data = await _request("GET", path, params={**(params or {}), "limit": page_size, "offset": len(items)})
        batch = data.get("results", [])
        items.extend(batch)
        total = (data.get("pagination") or {}).get("totalResults")
        if len(batch) < page_size or (total is not None and len(items) >= total) or len(items) >= MAX_ITEMS:
            return items


async def _get_jsonapi_paged(path: str) -> list[dict]:
    """links.next paging used by the Data Management (project/v1, data/v1) APIs."""
    items: list[dict] = []
    url: str | None = path
    while url and len(items) < MAX_ITEMS:
        data = await _request("GET", url)
        items.extend(data.get("data", []))
        url = ((data.get("links") or {}).get("next") or {}).get("href")
    return items


def _flatten(record: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, name + "."))
        elif isinstance(value, list):
            out[name] = "; ".join(
                str(v.get("name") or v.get("id") or v) if isinstance(v, dict) else str(v) for v in value
            )
        else:
            out[name] = value
    return out


def _deliver(dataset: str, project_id: str, records: list[dict], save_csv: bool) -> dict:
    rows = [_flatten(r) for r in records]
    result: dict = {"dataset": dataset, "count": len(rows)}
    if len(rows) >= MAX_ITEMS:
        result["truncated_at"] = MAX_ITEMS
    if save_csv and rows:
        EXPORT_DIR.mkdir(exist_ok=True)
        path = EXPORT_DIR / f"{dataset}_{_bare(project_id)[:8]}_{datetime.now():%Y%m%d_%H%M%S}.csv"
        columns = list(dict.fromkeys(col for row in rows for col in row))
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        result["saved_csv"] = str(path)
    result["rows"] = rows[:MAX_ROWS_OUT]
    if len(rows) > MAX_ROWS_OUT:
        result["note"] = f"Showing {MAX_ROWS_OUT} of {len(rows)} rows. Call again with save_csv=true for all rows."
    return result


_HINTS = {
    401: "Autodesk sign-in expired — run 'python acc_mcp.py --test' again to sign in.",
    403: "Access denied — your ACC login can't see this, or the APS app isn't enabled "
         "in ACC Account Admin > Custom Integrations.",
    404: "Not found — check the project ID and that this module is active on the project.",
}


async def _safe(fn):
    try:
        return await fn()
    except NeedsSignIn as e:
        return {
            "sign_in_required": True,
            "sign_in_url": str(e),
            "message": "A browser tab opened for Autodesk sign-in. Sign in, click Allow, then ask again. "
                       "If no tab opened, give the user sign_in_url.",
        }
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        return {"error": f"Autodesk returned {code}", "hint": _HINTS.get(code, ""), "details": e.response.text[:800]}
    except Exception as e:
        return {"error": str(e)}


# ── Tools ──────────────────────────────────────────────────────────────────
async def _projects(hub_id: str) -> list[dict]:
    return [
        {"project_id": p["id"], "name": p["attributes"].get("name")}
        for p in await _get_jsonapi_paged(f"/project/v1/hubs/{hub_id}/projects")
    ]


@mcp.tool(annotations=READ_ONLY)
async def list_hubs() -> dict:
    """List the ACC accounts (hubs) the signed-in user can access."""
    async def run():
        data = await _request("GET", "/project/v1/hubs")
        hubs = [
            {"hub_id": h["id"], "name": h["attributes"].get("name"), "region": h["attributes"].get("region")}
            for h in data.get("data", [])
        ]
        result: dict = {"hubs": hubs, "count": len(hubs)}
        # APS answers 200 with per-region 403s in meta.warnings when hub listing is
        # blocked; that is not the same as "no access to any project".
        denied = [w for w in (data.get("meta") or {}).get("warnings", []) if str(w.get("HttpStatusCode")) == "403"]
        if not hubs and denied:
            result["hub_listing_blocked"] = True
            result["hint"] = ("Autodesk blocked hub listing for this login/app (403), but project-level "
                              "tools may still work. Use DEFAULT_PROJECT_ID or pass a known project_id "
                              "to list_rfis, list_issues, list_submittals, etc.")
        return result
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_projects(hub_id: str) -> dict:
    """List projects in a hub (hub_id from list_hubs). Returns project_id values for the other tools."""
    async def run():
        projects = await _projects(hub_id)
        return {"projects": projects, "count": len(projects)}
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def browse_folder(folder_id: str = "", project_id: str = "", hub_id: str = "") -> dict:
    """Browse ACC Docs. Pass a folder_id to list its subfolders and files. With no folder_id,
    lists the project's top folders (needs hub_id, and may be blocked for some logins)."""
    async def run():
        pid = _prefixed(_project(project_id))
        if folder_id:
            entries = await _get_jsonapi_paged(f"/data/v1/projects/{pid}/folders/{urllib.parse.quote(folder_id, safe='')}/contents")
        elif hub_id:
            entries = await _get_jsonapi_paged(f"/project/v1/hubs/{hub_id}/projects/{pid}/topFolders")
        else:
            return {"error": "Pass a folder_id (e.g. the Project Files folder URN from the ACC address bar, "
                             "folderUrn=...) or a hub_id to list the top folders."}
        out = []
        for e in entries:
            a = e.get("attributes", {})
            out.append({
                "type": e.get("type"),               # folders | items
                "id": e.get("id"),
                "name": a.get("displayName") or a.get("name"),
                "modified": a.get("lastModifiedTime"),
                "modified_by": a.get("lastModifiedUserName"),
            })
        return {"entries": out, "count": len(out)}
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_rfis(project_id: str = "", save_csv: bool = False) -> dict:
    """All RFIs for a project (title, status, due date, assignees...). save_csv=true writes every row to a CSV."""
    async def run():
        pid = _project(project_id)
        path = f"/construction/rfis/v3/projects/{_bare(pid)}/search:rfis"
        items: list[dict] = []
        while True:
            data = await _request("POST", path, json_body={"limit": 100, "offset": len(items)})
            batch = data.get("results", [])
            items.extend(batch)
            total = (data.get("pagination") or {}).get("totalResults")
            if len(batch) < 100 or (total is not None and len(items) >= total) or len(items) >= MAX_ITEMS:
                break
        return _deliver("rfis", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_issues(project_id: str = "", save_csv: bool = False) -> dict:
    """All issues for a project (title, status, due date, assignee, location...)."""
    async def run():
        pid = _project(project_id)
        items = await _get_offset_paged(f"/construction/issues/v1/projects/{_bare(pid)}/issues")
        return _deliver("issues", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_submittals(project_id: str = "", save_csv: bool = False) -> dict:
    """All submittal items for a project (title, spec section, status, due dates, manager...)."""
    async def run():
        pid = _project(project_id)
        items = await _get_offset_paged(f"/construction/submittals/v2/projects/{_bare(pid)}/items")
        return _deliver("submittals", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_budgets(project_id: str = "", save_csv: bool = False) -> dict:
    """Cost budget lines (original, revised, committed, actual amounts)."""
    async def run():
        pid = _project(project_id)
        items = await _get_offset_paged(f"/cost/v1/containers/{_bare(pid)}/budgets")
        return _deliver("budgets", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_contracts(project_id: str = "", save_csv: bool = False) -> dict:
    """Cost contracts (subcontracts / purchase orders, with amounts and companies)."""
    async def run():
        pid = _project(project_id)
        items = await _get_offset_paged(f"/cost/v1/containers/{_bare(pid)}/contracts")
        return _deliver("contracts", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_change_orders(project_id: str = "", change_order_type: str = "pco", save_csv: bool = False) -> dict:
    """Change orders. change_order_type: pco (potential), rfq, rco, oco (owner), sco (subcontractor)."""
    async def run():
        pid = _project(project_id)
        kind = change_order_type.lower().strip()
        if kind not in {"pco", "rfq", "rco", "oco", "sco"}:
            return {"error": "change_order_type must be one of: pco, rfq, rco, oco, sco"}
        items = await _get_offset_paged(f"/cost/v1/containers/{_bare(pid)}/change-orders/{kind}")
        return _deliver(f"change_orders_{kind}", pid, items, save_csv)
    return await _safe(run)


@mcp.tool(annotations=READ_ONLY)
async def list_project_users(project_id: str = "", save_csv: bool = False) -> dict:
    """Project members (name, email, company, role). Use it to turn user IDs in other results into names."""
    async def run():
        pid = _project(project_id)
        items = await _get_offset_paged(f"/construction/admin/v1/projects/{_bare(pid)}/users")
        return _deliver("project_users", pid, items, save_csv)
    return await _safe(run)


# ── CLI ────────────────────────────────────────────────────────────────────
async def _login_and_wait() -> bool:
    try:
        await _get_token()
        return True
    except NeedsSignIn as e:
        print(f"\nOpening your browser for Autodesk sign-in. If nothing opens, visit:\n\n  {e}\n")
    deadline = time.time() + 300
    while not _access_token_valid():
        if time.time() > deadline:
            print("No sign-in within 5 minutes — run again.")
            return False
        await asyncio.sleep(1)
    print(f"Signed in. Sign-in saved to {TOKENS_FILE}")
    return True


async def _selftest() -> None:
    problem = _settings_problem()
    if problem:
        print(f"SETUP NEEDED: {problem}")
        return
    try:
        if not await _login_and_wait():
            return
    except RuntimeError as e:
        print(f"PROBLEM: {e}")
        return
    if _is_placeholder(DEFAULT_PROJECT) or DEFAULT_PROJECT == "b.":
        print("Signed in. Next: set DEFAULT_PROJECT_ID in .env (your ACC project ID with 'b.' in front), "
              "then run --test again to check that data comes back.")
        return
    print(f"Checking project {DEFAULT_PROJECT} ...")
    ok = False
    for name, tool in (("RFIs", list_rfis), ("issues", list_issues), ("submittals", list_submittals)):
        r = await (tool.fn if hasattr(tool, "fn") else tool)()
        if "count" in r:
            ok = True
            print(f"  {name:11s} {r['count']}")
        else:
            print(f"  {name:11s} PROBLEM: {r.get('error')} {r.get('hint', '')}".rstrip())
    print("\nSUCCESS: Claude will be able to read this project." if ok else
          "\nNo data came back — see the hints above (403 usually means Custom Integrations).")


if __name__ == "__main__":
    _load_tokens()
    if "--test" in sys.argv:
        asyncio.run(_selftest())
    elif "--login" in sys.argv:
        asyncio.run(_login_and_wait())
    else:
        mcp.run()  # stdio transport for Claude Desktop
