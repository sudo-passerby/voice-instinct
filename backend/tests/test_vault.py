import asyncio
import base64
import json
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from debugbrowser import headless_shell, launch
from hooks import SIGNED_IN_NO_PAY, HookBridge
from registry import Registry
from tests.test_hooks import FakeRelay, _answer, _click, _page
from vault import Vault, fill_page, https_site, make_router, site_of, totp, with_vault


def test_codes_match_rfc_6238():
    secret = base64.b32encode(b"12345678901234567890").decode()  # the RFC's SHA-1 key
    assert totp(secret, now=59) == "287082" and totp(secret, now=1111111109) == "081804"
    assert totp(secret.lower()[:20] + " " + secret.lower()[20:], now=59) == "287082"  # as apps show it


@pytest.mark.parametrize("url,site", [
    ("https://www.amazon.in/ap/signin", "amazon.in"), ("https://amazon.in/", "amazon.in"),
    ("https://accounts.amazon.in./x", "amazon.in"),
    ("http://www.amazon.in/ap/signin", None),  # never on plain http
    ("https://evilamazon.in/", None), ("https://amazon.in.evil.com/", None), ("https://in/", None)])
def test_a_login_is_only_for_https_pages_of_its_own_site(url, site):
    assert https_site(url, ["amazon.in", "netflix.com"]) == site


def test_the_longest_saved_site_wins():
    assert site_of("mail.google.com", ["google.com", "mail.google.com"]) == "mail.google.com"


class FakeKeys:
    def __init__(self, entries):
        self.entries = entries

    def sites(self):
        return list(self.entries)

    def get(self, site):
        return self.entries.get(site)


class FakeBrowser:
    endpoint = "http://fake"


class FakeBridge:
    def __init__(self, answer="allow", page="https://www.amazon.in/ap/signin"):
        self.answer, self.page, self.asked = answer, page, []

    def page_of(self, sid):
        return self.page

    async def ask(self, sid, kind, prompt, tool_name=None, tool_input=None):
        self.asked.append(prompt)
        return self.answer


def _vault(monkeypatch, entries, bridge, url=None):
    filled = []

    async def fill(ws, site, username, password, code):
        filled.append((site, username, password, code))
        return ["username", "password"], False

    v = Vault("/nonexistent", bridge, None, None, keyring=FakeKeys(entries), fill=fill)
    v._browsers["s1"] = FakeBrowser()

    class Resp:
        async def json(self, content_type=None):
            return [{"type": "page", "url": url or bridge.page, "webSocketDebuggerUrl": "ws://page"}]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session(Resp):
        def get(self, u):
            return Resp()

    monkeypatch.setattr("vault.aiohttp.ClientSession", Session)
    return v, filled


def test_sign_in_asks_once_per_site_then_fills_without_the_agent_seeing_it(monkeypatch):
    bridge = FakeBridge()
    v, filled = _vault(monkeypatch, {"amazon.in": {"username": "me@x.in", "password": "pw"}}, bridge)
    first = asyncio.run(v.sign_in("s1"))
    second = asyncio.run(v.sign_in("s1"))  # the password page after the username page
    assert len(bridge.asked) == 1 and "amazon.in" in bridge.asked[0] and "pw" not in bridge.asked[0]
    assert filled == [("amazon.in", "me@x.in", "pw", None)] * 2
    assert "Filled" in first and "pw" not in first + second


def test_a_no_means_nothing_is_filled(monkeypatch):
    v, filled = _vault(monkeypatch, {"amazon.in": {"username": "u", "password": "pw"}}, FakeBridge(answer="deny"))
    assert "did not allow" in asyncio.run(v.sign_in("s1")) and filled == []


def test_no_saved_login_tells_the_user_how_to_add_one(monkeypatch):
    v, filled = _vault(monkeypatch, {}, FakeBridge(page="https://www.netflix.com/login"))
    text = asyncio.run(v.sign_in("s1"))
    assert "vault.py add netflix.com" in text and filled == []


def test_a_saved_site_on_plain_http_is_refused(monkeypatch):
    v, filled = _vault(monkeypatch, {"amazon.in": {"username": "u", "password": "pw"}},
                       FakeBridge(page="http://www.amazon.in/ap/signin"))
    assert "https" in asyncio.run(v.sign_in("s1")) and filled == []


def test_the_code_comes_from_the_saved_authenticator_secret(monkeypatch):
    secret = base64.b32encode(b"12345678901234567890").decode()
    v, filled = _vault(monkeypatch, {"amazon.in": {"username": "u", "password": "pw", "totp": secret}}, FakeBridge())
    monkeypatch.setattr("vault.totp", lambda s: "654321")
    asyncio.run(v.sign_in("s1"))
    assert filled[0][3] == "654321"


def test_the_mcp_server_lists_and_runs_sign_in_only_with_the_token():
    class V:
        async def sign_in(self, sid):
            return f"signed in {sid}"

    app = FastAPI()
    app.include_router(make_router(V(), "tok"))
    c = TestClient(app)
    rpc = lambda m, **p: {"jsonrpc": "2.0", "id": 1, "method": m, "params": p}
    assert c.post("/hook/nope/vault/s1", json=rpc("tools/list")).status_code == 404
    assert c.post("/hook/tok/vault/s1", json=rpc("initialize", protocolVersion="2025-06-18")).json()[
        "result"]["protocolVersion"] == "2025-06-18"
    assert c.post("/hook/tok/vault/s1", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
    assert [t["name"] for t in c.post("/hook/tok/vault/s1", json=rpc("tools/list")).json()["result"]["tools"]] == ["sign_in"]
    r = c.post("/hook/tok/vault/s1", json=rpc("tools/call", name="sign_in", arguments={})).json()
    assert r["result"]["content"][0]["text"] == "signed in s1"


def test_the_run_mcp_file_holds_the_token_and_only_its_owner_reads_it(tmp_path):
    path = with_vault(json.dumps({"mcpServers": {"browser": {}}}), "s1", 7860, "tok", tmp_path / "runs")
    servers = json.loads(open(path).read())["mcpServers"]
    assert servers["vault"] == {"type": "http", "url": "http://127.0.0.1:7860/hook/tok/vault/s1"}
    assert "browser" in servers and (tmp_path / "runs/s1.mcp.json").stat().st_mode & 0o077 == 0


# --- the purchase guard on a signed-in site ---

def _guard(sites):
    async def signed_in(url):
        return any(s in (url or "") for s in sites)

    reg = Registry()
    return HookBridge(reg, FakeRelay(), permission_timeout=5, signed_in=signed_in), reg


def _on(b, url, node):
    _page(b, url)
    b._snapshots["s1"] = node


def test_a_paying_step_on_a_signed_in_site_is_refused_without_asking():
    b, reg = _guard(["amazon.in"])
    _on(b, "https://www.amazon.in/cart", '- button "Place your order" [ref=e12]')
    out, asked = _answer(b, reg, _click("Place your order"), "allow")
    assert asked == [] and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == SIGNED_IN_NO_PAY


def test_a_cancellation_on_a_signed_in_site_is_still_asked():
    b, reg = _guard(["netflix.com"])
    _on(b, "https://www.netflix.com/cancelplan", '- button "Cancel subscription" [ref=e12]')
    out, asked = _answer(b, reg, _click("Cancel subscription"), "allow")
    assert len(asked) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_a_paying_step_on_any_other_site_is_still_asked_as_before():
    b, reg = _guard(["amazon.in"])
    _on(b, "https://www.zomato.com/checkout", '- button "Place order" [ref=e12]')
    out, asked = _answer(b, reg, _click("Place order"), "allow")
    assert len(asked) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "allow"


# --- real Chromium: the fill, the submit and the clearing ---

LOGIN = """<form action="/done" method="get"><input name="email" type="email" placeholder="Email">
<input name="pw" type="password"><button>Sign in</button></form>"""
STAYS = """<form onsubmit="event.preventDefault()"><input name="email" type="email">
<input name="pw" type="password"></form>"""  # a sign-in that fails and leaves the page as it was
CODE = """<form action="/done"><input name="otp" autocomplete="one-time-code"></form>"""


async def _serve(exe, tmp_path, html, url, run):
    """Our Chromium with every request answered by `html` over CDP (an https page, no certificate needed)."""
    b = await launch(exe, profile=tmp_path / "profile")
    seen = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{b.endpoint}/json/list") as r:
                page = next(t for t in await r.json(content_type=None) if t["type"] == "page")
            async with s.ws_connect(page["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                ids = iter(range(1000, 10 ** 6))

                async def send(method, params=None):
                    await ws.send_json({"id": next(ids), "method": method, "params": params or {}})

                await send("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
                await send("Page.navigate", {"url": url})

                async def answer():
                    async for msg in ws:
                        d = msg.json()
                        if d.get("method") == "Fetch.requestPaused":
                            seen.append(d["params"]["request"]["url"])
                            await send("Fetch.fulfillRequest", {
                                "requestId": d["params"]["requestId"], "responseCode": 200,
                                "responseHeaders": [{"name": "Content-Type", "value": "text/html"}],
                                "body": base64.b64encode(html.encode()).decode()})

                server = asyncio.create_task(answer())
                await asyncio.sleep(1)
                try:
                    out = await run(page["webSocketDebuggerUrl"])
                    values = await _values(page["webSocketDebuggerUrl"])
                finally:
                    server.cancel()
    finally:
        await b.close()
    return out, values, seen


async def _values(ws_url):
    async with aiohttp.ClientSession() as s, s.ws_connect(ws_url) as ws:
        await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": "[...document.querySelectorAll('input')].map(e => e.value)", "returnByValue": True}})
        async for msg in ws:
            if msg.json().get("id") == 1:
                return msg.json()["result"]["result"]["value"]


def _exe():
    import pathlib
    exe = headless_shell(pathlib.Path.home())
    if exe is None:
        pytest.skip("the errand browser is not installed")
    return exe


def test_real_a_login_is_filled_submitted_and_never_left_on_the_page(tmp_path):
    run = lambda ws: fill_page(ws, "example.test", "me@example.com", "HUNTER2secret", None, settle=1.5)
    out, values, seen = asyncio.run(_serve(_exe(), tmp_path, LOGIN, "https://login.example.test/", run))
    assert out == (["username", "password"], False)
    sent = parse_qs(urlsplit(seen[-1]).query)
    assert sent == {"email": ["me@example.com"], "pw": ["HUNTER2secret"]}  # Enter submitted the real form
    assert "HUNTER2secret" not in values


def test_real_a_failed_sign_in_has_its_password_cleared_before_the_agent_looks(tmp_path):
    run = lambda ws: fill_page(ws, "example.test", "me@example.com", "HUNTER2secret", None, settle=0.5)
    out, values, _ = asyncio.run(_serve(_exe(), tmp_path, STAYS, "https://login.example.test/", run))
    assert out[0] == ["username", "password"] and values == ["me@example.com", ""]


def test_real_nothing_is_filled_on_another_site_or_over_http(tmp_path):
    for url in ("https://example.test.evil.com/", "http://login.example.test/"):
        run = lambda ws: fill_page(ws, "example.test", "u", "HUNTER2secret", None, settle=0)
        out, values, _ = asyncio.run(_serve(_exe(), tmp_path, LOGIN, url, run))
        assert out == (None, False) and "HUNTER2secret" not in values


def test_real_the_code_step_is_filled_from_the_authenticator(tmp_path):
    run = lambda ws: fill_page(ws, "example.test", "u", "pw", "123456", settle=1)
    out, _, seen = asyncio.run(_serve(_exe(), tmp_path, CODE, "https://login.example.test/", run))
    assert out == (["code"], False) and "otp=123456" in seen[-1]


def test_real_a_code_sent_by_text_is_reported_not_guessed(tmp_path):
    run = lambda ws: fill_page(ws, "example.test", "u", "pw", None, settle=0)
    out, _, _ = asyncio.run(_serve(_exe(), tmp_path, CODE, "https://login.example.test/", run))
    assert out == ([], True)


def test_real_the_profile_stays_signed_in_between_errands(tmp_path):
    async def go():
        for _ in range(2):
            b = await launch(_exe(), profile=tmp_path / "profile")
            await b.close()
        return (tmp_path / "profile").is_dir()
    assert asyncio.run(go())
