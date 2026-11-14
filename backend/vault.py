"""The vault (Instinct's Vault, locally): saved logins in the system keyring, filled in by the backend.

A login is added from a terminal (`python vault.py add amazon.in`), never said aloud. An errand calls the vault's
sign_in tool (an MCP server this app serves) on a site's sign-in page; Nova asks the user by voice, and the backend
fills the username, password and 6-digit code itself over CDP, so the agent never sees them. A login is filled only
on an https page of the site it was saved for (the password-manager rule), and the password is cleared from the
page before the tool returns: Playwright's page snapshots show a typed password in plain text.

Errands share one browser profile that stays signed in (data_dir/browser-profile, denied to the agent). On a site
with a saved login the purchase guard refuses every paying step outright (hooks.py): the site may have a saved card.
"""

import asyncio
import base64
import getpass
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger

APP = "voice-instinct"  # the keyring attribute every login carries
SETTLE = 4  # seconds after submitting before the password is cleared and the tool returns


# --- the keyring (secret-tool, libsecret) ---

def _secret_tool(*args, secret=None):
    try:
        r = subprocess.run(["secret-tool", *args], input=secret, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


class Keyring:
    def save(self, site, entry):
        if _secret_tool("store", f"--label=Nova login: {site}", "app", APP, "site", site,
                        secret=json.dumps(entry)) is None:
            raise RuntimeError("the keyring refused it (is it unlocked?)")

    def get(self, site):
        out = _secret_tool("lookup", "app", APP, "site", site)
        try:
            return json.loads(out) if out else None
        except ValueError:
            return None

    def sites(self):
        out = _secret_tool("search", "--all", "app", APP) or ""
        return sorted({line.split("=", 1)[1].strip() for line in out.splitlines()
                       if line.startswith("attribute.site")})

    def remove(self, site):
        _secret_tool("clear", "app", APP, "site", site)


# --- matching and codes ---

def site_of(host, sites):
    """The saved site a page's host belongs to: the host itself or a parent domain (www.amazon.in -> amazon.in)."""
    host = (host or "").lower().rstrip(".")
    return next((s for s in sorted(sites, key=len, reverse=True) if host == s or host.endswith("." + s)), None)


def https_site(url, sites):
    parts = urlsplit(url or "")
    return site_of(parts.hostname, sites) if parts.scheme == "https" else None


def totp(secret_b32, now=None, step=30, digits=6):
    """RFC 6238, as authenticator apps do it (SHA-1, 30 s, 6 digits)."""
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32.replace(" ", "")) % 8))
    mac = hmac.new(key, struct.pack(">Q", int((time.time() if now is None else now) // step)), hashlib.sha1).digest()
    o = mac[-1] & 15
    return str((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


# --- filling the page over CDP ---

# Runs in the page. Refuses unless the page itself is https on the saved site. Fills what this step of the sign-in
# shows (many sites ask for the username, then the password, then the code, on separate pages) and focuses the
# last field filled, for the Enter key that submits it. Never clicks anything.
_FILL = """(a) => {
  const h = location.hostname;
  if (location.protocol !== 'https:' || !(h === a.site || h.endsWith('.' + a.site))) return {error: 'wrong-site'};
  const shown = e => e.offsetParent !== null && !e.disabled && !e.readOnly;
  const inputs = [...document.querySelectorAll('input')].filter(shown);
  const words = e => [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), e.autocomplete].join(' ');
  const pw = inputs.find(e => e.type === 'password');
  const code = inputs.find(e => e !== pw && (e.autocomplete === 'one-time-code' ||
    /\\b(otp|totp|2fa|mfa|one.?time|verification|auth\\w*.?code|security.?code)\\b/i.test(words(e))));
  const text = inputs.filter(e => ['text', 'email', 'tel', ''].includes(e.type) && e !== code);
  const user = text.find(e => /username|email/.test(e.autocomplete) || e.type === 'email')
    || text.find(e => /user|email|login|phone|mobile|account/i.test(words(e)))
    || (pw ? text.filter(e => e.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING).pop() : null);
  const set = (e, v) => {
    e.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, v);
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const filled = [];
  if (user && a.username) { set(user, a.username); filled.push('username'); }
  if (pw) { set(pw, a.password); filled.push('password'); }
  if (code && a.code && !pw) { set(code, a.code); filled.push('code'); }
  const wantsCode = !!code && !pw && !a.code;
  if (filled.length) (filled.includes('password') ? pw : filled.includes('code') ? code : user).focus();
  return {filled, wantsCode};
}"""
# Clears every field still holding the password or the code (a failed sign-in leaves them on the page).
_CLEAR = """(a) => {
  let n = 0;
  for (const e of document.querySelectorAll('input')) {
    if (e.value && (e.value === a.password || e.value === a.code)) {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(e, '');
      e.dispatchEvent(new Event('input', {bubbles: true})); n++;
    }
  }
  return n;
}"""


async def _cdp(ws, method, params=None, _ids=iter(range(1, 10 ** 9))):
    i = next(_ids)
    await ws.send_json({"id": i, "method": method, "params": params or {}})
    async for msg in ws:
        d = msg.json()
        if d.get("id") == i:
            if "error" in d:
                raise RuntimeError(d["error"].get("message", method))
            return d.get("result", {})
    raise RuntimeError("the browser closed the connection")


async def _call(ws, fn, arg):
    r = await _cdp(ws, "Runtime.evaluate", {"expression": f"({fn})({json.dumps(arg)})", "returnByValue": True,
                                            "awaitPromise": True})
    if "exceptionDetails" in r:
        raise RuntimeError("the page refused the script")
    return r.get("result", {}).get("value")


async def _enter(ws):
    for kind in ("keyDown", "keyUp"):
        await _cdp(ws, "Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                  "windowsVirtualKeyCode": 13, **({"text": "\r"} if kind == "keyDown" else {})})


async def fill_page(ws_url, site, username, password, code, settle=SETTLE):
    """Fill this step of the sign-in on the page at ws_url, submit it, then clear the secrets. (filled, wants_code)."""
    secrets = {"password": password or "\0none", "code": code or "\0none"}
    async with aiohttp.ClientSession() as s, s.ws_connect(ws_url, max_msg_size=0) as ws:
        r = await _call(ws, _FILL, {"site": site, "username": username, "password": password, "code": code})
        if not isinstance(r, dict) or r.get("error"):
            return None, False
        try:
            if r["filled"]:
                await _enter(ws)
                await asyncio.sleep(settle)
        finally:  # whatever happened, the password never stays on a page the agent will snapshot
            for _ in range(3):
                try:
                    if not await _call(ws, _CLEAR, secrets):
                        break
                except RuntimeError:  # mid-navigation: the old page and its fields are gone
                    await asyncio.sleep(0.5)
        return r["filled"], r.get("wantsCode", False)


# --- the vault: the errand browser, the sign_in tool, the purchase-guard check ---

ADD_HINT = f"cd {Path(__file__).resolve().parent} && uv run python vault.py add {{site}}"


class Vault:
    def __init__(self, data_dir, bridge, launch, exe, keyring=None, fill=fill_page):
        """launch: debugbrowser.launch (with hide_env bound); exe: the headless Chromium, or None (no vault)."""
        self.profile = Path(data_dir) / "browser-profile"
        self.bridge, self._launch, self.exe = bridge, launch, exe
        self.keys, self._fill = keyring or Keyring(), fill
        self._browsers, self._approved = {}, {}  # session -> its browser; session -> sites the user said yes to

    async def open(self, sid):
        """The errand browser for this run: our Chromium on the shared, signed-in profile. None: not installed,
        or it would not start (the errand then gets the fresh-profile browser, without sign-in)."""
        if self.exe is None:
            return None
        try:
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            b = await self._launch(self.exe, profile=self.profile)
        except Exception as e:
            logger.warning(f"vault browser did not start: {e!r}")
            return None
        self._browsers[sid] = b
        return b

    async def close(self, sid):
        self._approved.pop(sid, None)
        if b := self._browsers.pop(sid, None):
            await b.close()

    async def signed_in(self, url):
        """The page is on a site with a saved login (any scheme: http is still the same account)."""
        host = urlsplit(url or "").hostname
        return bool(host) and site_of(host, await asyncio.to_thread(self.keys.sites)) is not None

    async def sign_in(self, sid):
        b = self._browsers.get(sid)
        if b is None:
            return "Signing in isn't available in this errand's browser."
        async with aiohttp.ClientSession() as s, s.get(f"{b.endpoint}/json/list") as r:
            pages = [t for t in await r.json(content_type=None) if t.get("type") == "page"]
        want = self.bridge.page_of(sid)
        page = next((t for t in pages if want and t.get("url") == want), pages[0] if pages else None)
        if page is None:
            return "No page is open. Open the site's sign-in page first."
        url, sites = page.get("url", ""), await asyncio.to_thread(self.keys.sites)
        site = https_site(url, sites)
        if site is None:
            host = urlsplit(url).hostname or url
            if urlsplit(url).scheme != "https" and site_of(host, sites):
                return "I only sign in on https pages. Open the site's https sign-in page."
            return (f"There is no saved login for {host}. Stop and report that the user can add one by running "
                    f"this in a terminal: {ADD_HINT.format(site=host.removeprefix('www.'))}")
        entry = await asyncio.to_thread(self.keys.get, site)
        if not entry:
            return f"The saved login for {site} could not be read (is the keyring unlocked?)."
        if site not in self._approved.setdefault(sid, set()):
            answer = await self.bridge.ask(sid, "permission", f"sign in to {site} with your saved login "
                                           f"({entry.get('username', '')})", "vault_sign_in", {"site": site})
            if answer != "allow":
                return "The user did not allow signing in. Don't try again in this errand."
            self._approved[sid].add(site)
        code = totp(entry["totp"]) if entry.get("totp") else None
        try:
            filled, wants_code = await self._fill(page["webSocketDebuggerUrl"], site, entry.get("username"),
                                                  entry.get("password"), code)
        except Exception as e:
            logger.warning(f"vault fill failed ({sid[:8]}): {e!r}")
            return "Filling the sign-in failed. Take a snapshot and try sign_in once more."
        logger.info(f"vault: filled {filled} on {site} ({sid[:8]})")
        if filled is None:
            return "This page isn't on the saved site, so nothing was filled."
        if wants_code:
            return ("The site wants a code sent by text or email, which I can't fill. Stop and report that "
                    "sign-in needs that code.")
        if not filled:
            return "No sign-in fields on this page. Open the sign-in page, then call sign_in again."
        return (f"Filled {' and '.join(filled)} and submitted. Take a snapshot: if the site asks for the next "
                "step (a password or a 6-digit code), call sign_in again.")


TOOL = {"name": "sign_in", "description": (
    "Sign in to the site open in the browser with the user's saved login: the vault fills and submits the "
    "username, password and 6-digit code itself (the user is asked first). Call it on the site's sign-in page, "
    "and again for each step it asks for. Never type a password or code yourself."),
    "inputSchema": {"type": "object", "properties": {}}}


def make_router(vault, token):
    """The vault's MCP server (Streamable HTTP, JSON replies), under /hook/ so the origin guard lets the claude CLI
    in (it sends no Origin, guard.py) and the hook token keeps everyone else out."""
    router = APIRouter()

    @router.api_route("/hook/{tok}/vault/{sid}", methods=["GET", "POST", "DELETE"])
    async def mcp(tok: str, sid: str, request: Request):
        if not hmac.compare_digest(tok.encode(), token.encode()):
            raise HTTPException(status_code=404)
        if request.method != "POST":
            return Response(status_code=405)  # no server-to-client stream
        msg = await request.json()
        if "id" not in msg:  # a notification
            return Response(status_code=202)
        method, ok = msg.get("method"), lambda result: JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if method == "initialize":
            return ok({"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                       "capabilities": {"tools": {}}, "serverInfo": {"name": "vault", "version": "1"}})
        if method == "tools/list":
            return ok({"tools": [TOOL]})
        if method == "tools/call" and (msg.get("params") or {}).get("name") == "sign_in":
            try:
                text = await vault.sign_in(sid)
            except Exception as e:
                logger.error(f"vault sign_in failed: {e!r}")
                text = "Signing in failed."
            return ok({"content": [{"type": "text", "text": text}]})
        if method == "ping":
            return ok({})
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unknown"}})

    return router


def with_vault(mcp_json, sid, port, token, runs_dir):
    """The run's --mcp-config with the vault server added, written 0600 under runs/ (denied to the agent): its URL
    holds the hook token. Returns the file's path."""
    servers = json.loads(mcp_json)
    servers["mcpServers"]["vault"] = {"type": "http", "url": f"http://127.0.0.1:{port}/hook/{token}/vault/{sid}"}
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    path = Path(runs_dir) / f"{sid}.mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(servers))
    return str(path)


def main(argv):
    keys = Keyring()
    cmd, site = (argv + ["", ""])[:2]
    site = site.lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
    if cmd == "add" and site:
        username = input(f"Username or email for {site}: ").strip()
        password = getpass.getpass("Password (not shown): ")
        secret = getpass.getpass("Authenticator secret, for 6-digit codes (Enter to skip): ").strip()
        entry = {"username": username, "password": password}
        if secret:
            try:
                print(f"Current code: {totp(secret)} (check it matches your authenticator app)")
            except ValueError:
                sys.exit("That isn't an authenticator secret (it's the base32 text behind the QR code).")
            entry["totp"] = secret
        keys.save(site, entry)
        print(f"Saved. Nova can now sign in to {site}.")
    elif cmd == "remove" and site:
        keys.remove(site)
        print(f"Removed {site}.")
    elif cmd == "list":
        print("\n".join(keys.sites()) or "No saved logins.")
    else:
        sys.exit("usage: python vault.py add <site> | remove <site> | list")


if __name__ == "__main__":
    main(sys.argv[1:])
