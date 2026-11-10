import asyncio
import json
import re
import stat
from pathlib import Path

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hooks import STEP_MATCHER, HookBridge, hook_response, load_token, make_router, write_run_settings
from registry import Registry


class FakeRelay:
    def __init__(self):
        self.raised, self.closed = [], []

    async def raise_question(self, qid):
        self.raised.append(qid)

    def question_closed(self, qid, expired=False):
        self.closed.append(qid)
        if expired:
            self.expired = getattr(self, "expired", []) + [qid]


PAYLOAD = {"session_id": "s1", "cwd": "/w", "hook_event_name": "PermissionRequest",
           "tool_name": "Bash", "tool_input": {"command": "rm -r build"}}


def make(timeout=5):
    reg, relay = Registry(), FakeRelay()
    return HookBridge(reg, relay, permission_timeout=timeout), reg, relay


def test_permission_blocks_until_resolved_then_allows():
    async def go():
        b, reg, relay = make()
        t = asyncio.create_task(b.on_permission_request(PAYLOAD))
        await asyncio.sleep(0)
        q = reg.open_questions()[0]
        assert relay.raised == [q["id"]] and reg.get_session("s1")["state"] == "waiting_input"
        assert b.resolve(q["id"], "allow") is True
        return await t, reg, relay, q["id"]

    out, reg, relay, qid = asyncio.run(go())
    assert out == hook_response("allow")
    assert reg.get_question(qid)["status"] == "answered" and relay.closed == [qid]
    assert reg.get_session("s1")["state"] == "running"


def test_deny_response_has_a_message():
    r = hook_response("deny")
    assert r["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert r["hookSpecificOutput"]["decision"]["message"]


def test_timeout_returns_no_decision_and_expires_question():
    async def go():
        b, reg, relay = make(timeout=0.05)
        return await b.on_permission_request(PAYLOAD), reg, relay

    out, reg, relay = asyncio.run(go())
    assert out == {} and reg.get_question(1)["status"] == "expired" and relay.closed == [1]


def test_duplicate_delivery_shares_one_question_and_one_prompt():
    async def go():
        b, reg, relay = make()
        t1 = asyncio.create_task(b.on_permission_request(PAYLOAD))
        t2 = asyncio.create_task(b.on_permission_request(PAYLOAD))
        await asyncio.sleep(0)
        b.resolve(1, "deny")
        return await t1, await t2, relay

    r1, r2, relay = asyncio.run(go())
    assert r1 == r2 == hook_response("deny") and relay.raised == [1]


def test_resolve_unknown_or_finished_returns_false():
    b, _, _ = make()
    assert b.resolve(99, "allow") is False


def test_stop_and_session_end_update_registry():
    async def go():
        b, reg, _ = make()
        await b.on_event("SessionStart", {"session_id": "s2", "cwd": "/x"})
        await b.on_event("Stop", {"session_id": "s2", "last_assistant_message": "done"})
        assert reg.get_session("s2")["state"] == "idle"
        await b.on_event("SessionEnd", {"session_id": "s2", "reason": "other"})
        return reg

    reg = asyncio.run(go())
    s = reg.get_session("s2")
    assert (s["state"], s["end_reason"], s["origin"]) == ("ended", "other", "external")


def test_router_rejects_wrong_token_and_accepts_right_one():
    b, reg, _ = make()
    app = FastAPI()
    app.include_router(make_router(b, "sekret"))
    c = TestClient(app)
    assert c.post("/hook/wrong/Stop", json={"session_id": "s3"}).status_code == 404
    ok = c.post("/hook/sekret/Stop", json={"session_id": "s3"})
    assert ok.status_code == 200 and ok.json() == {}
    assert reg.get_session("s3")["state"] == "idle"


def test_router_ignores_payload_without_session_id():
    b, _, _ = make()
    app = FastAPI()
    app.include_router(make_router(b, "t"))
    assert TestClient(app).post("/hook/t/Stop", json={}).json() == {}


def test_token_is_created_once_with_private_mode(tmp_path):
    p = tmp_path / "hook-token"
    t1, t2 = load_token(p), load_token(p)
    assert t1 == t2 and len(t1) >= 32
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_run_settings_point_at_token_url(tmp_path):
    path = write_run_settings(tmp_path, "sess-1", "tok", 7860)
    hooks = json.loads(open(path).read())["hooks"]
    assert set(hooks) >= {"PermissionRequest", "Stop", "SessionEnd", "SessionStart"}
    h = hooks["PermissionRequest"][0]["hooks"][0]
    assert h["type"] == "http" and h["url"] == "http://127.0.0.1:7860/hook/tok/PermissionRequest"
    assert h["timeout"] == 100


def test_cancelled_handler_cleans_up_waiter_question_and_relay():
    async def go():
        b, reg, relay = make()
        t = asyncio.create_task(b.on_permission_request(PAYLOAD))
        await asyncio.sleep(0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return b, reg, relay

    b, reg, relay = asyncio.run(go())
    assert b._waiters == {} and reg.get_question(1)["status"] == "expired" and relay.closed == [1]
    assert reg.get_session("s1")["state"] == "running"


def test_relay_failure_does_not_leave_a_zombie_question():
    class BoomRelay(FakeRelay):
        async def raise_question(self, qid):
            raise RuntimeError("boom")

    async def go():
        reg, relay = Registry(), BoomRelay()
        b = HookBridge(reg, relay, permission_timeout=5)
        try:
            await b.on_permission_request(PAYLOAD)
        except RuntimeError:
            pass
        return b, reg, relay

    b, reg, relay = asyncio.run(go())
    assert b._waiters == {} and reg.get_question(1)["status"] == "expired" and relay.closed == [1]


def test_expire_session_releases_only_that_sessions_waiters():
    async def go():
        b, reg, relay = make()
        other = dict(PAYLOAD, session_id="s2")
        t1 = asyncio.create_task(b.on_permission_request(PAYLOAD))
        t2 = asyncio.create_task(b.on_permission_request(other))
        await asyncio.sleep(0)
        b.expire_session("s1")
        r1 = await asyncio.wait_for(t1, 2)
        assert not t2.done()
        b.resolve(2, "allow")
        return r1, await t2

    r1, r2 = asyncio.run(go())
    assert r1 == {} and r2 == hook_response("allow")


def test_settings_file_is_private_and_access_log_hides_tokens(tmp_path):
    import logging

    from hooks import hide_hook_tokens

    path = write_run_settings(tmp_path, "s", "tok", 7860)
    assert stat.S_IMODE(__import__("os").stat(path).st_mode) == 0o600
    fmt = '%s - "%s %s HTTP/%s" %d'  # uvicorn's access-log record
    hook_line = logging.LogRecord("uvicorn.access", 20, "", 0, fmt, ("127.0.0.1:1", "POST", "/hook/sekret/Stop", "1.1", 403), None)
    other = logging.LogRecord("uvicorn.access", 20, "", 0, fmt, ("127.0.0.1:1", "GET", "/", "1.1", 200), None)
    # Hook calls stay in the log (a refused hook must be visible) with the token blanked out.
    assert hide_hook_tokens(hook_line) is True and hide_hook_tokens(other) is True
    assert "sekret" not in hook_line.getMessage() and "/hook/…/Stop" in hook_line.getMessage() and "403" in hook_line.getMessage()
    for path in ("/hook/sekret", "/hook/sekret?x=1"):  # a probe with no event after the token
        bare = logging.LogRecord("uvicorn.access", 20, "", 0, fmt, ("127.0.0.1:1", "POST", path, "1.1", 404), None)
        hide_hook_tokens(bare)
        assert "sekret" not in bare.getMessage() and "/hook/…" in bare.getMessage()


def test_a_timeout_tells_the_relay_the_question_expired_but_an_answer_does_not():
    async def go():
        b, reg, relay = make(timeout=0.05)
        await b.on_permission_request(PAYLOAD)
        t = asyncio.create_task(b.on_permission_request({**PAYLOAD, "tool_input": {"command": "ls"}}))
        await asyncio.sleep(0)
        b.resolve(reg.open_questions()[0]["id"], "deny")
        await t
        return relay

    relay = asyncio.run(go())
    assert relay.closed == [1, 2] and relay.expired == [1]


def test_a_non_ascii_token_in_the_url_is_rejected_not_a_server_error():
    b, reg, _ = make()
    app = FastAPI()
    app.include_router(make_router(b, "sekret"))
    assert TestClient(app).post("/hook/sékret/Stop", json={"session_id": "s3"}).status_code == 404


def test_an_existing_token_file_is_made_private_again(tmp_path):
    p = tmp_path / "hook-token"
    load_token(p)
    p.chmod(0o644)
    load_token(p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_web_requests_are_described_in_words_not_json():
    from hooks import describe
    assert describe("WebFetch", {"url": "https://example.com/deals", "prompt": "find prices"}) == "WebFetch: https://example.com/deals"
    assert describe("WebSearch", {"query": "cheap flights to Goa"}) == "WebSearch: cheap flights to Goa"
    assert describe("Edit", "not a dict").startswith("Edit")  # never crashes into a silent denial


def test_every_run_gets_the_step_hook_for_the_time_budget(tmp_path):
    # Not only debug runs: the time budget (budget.py) answers through it on every errand.
    settings = json.loads(Path(write_run_settings(tmp_path, "a", "tok", 7860)).read_text())
    (entry,) = settings["hooks"]["PostToolUse"]
    assert entry["matcher"] == STEP_MATCHER
    assert entry["hooks"][0]["url"] == "http://127.0.0.1:7860/hook/tok/PostToolUse"
    assert entry["hooks"][0]["timeout"] == 15


def test_the_step_hook_sees_every_tool_the_agent_uses():
    # An errand researched with WebSearch alone recorded "0 steps" when only browser tools were hooked.
    assert STEP_MATCHER == "*"  # Claude Code's matcher for every tool


def _click(element, target="e12", sid="s1", tool="mcp__browser__browser_click", **inp):
    """Playwright MCP 0.0.83's shape (install.sh pins it): the element is named by `target`, a snapshot ref."""
    return {"session_id": sid, "cwd": "/w", "hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"element": element, "target": target, **inp} if element else {"target": target, **inp}}


def _answer(b, reg, payload, decision):
    async def go():
        t = asyncio.create_task(b.on_pre_tool_use(payload))
        for _ in range(5):
            await asyncio.sleep(0)
        qs = reg.open_questions()
        if qs and decision:
            b.resolve(qs[0]["id"], decision)
        return await asyncio.wait_for(t, 1), qs

    return asyncio.run(go())


@pytest.mark.parametrize("element", ["Pay now", "Make payment", "Buy now", "Purchase", "Place order", "Checkout",
                                     "Check out", "Subscribe", "Start subscription", "Confirm booking", "Book now",
                                     "Reserve a table", "Confirm", "Donate", "Bid", "Place bid",
                                     "Book", "Book table", "Book ride", "Complete booking", "Complete reservation",
                                     "Start free trial", "Upgrade", "Request ride", "Get tickets",
                                     "Complete purchase", "Renew", "Make a reservation"])
def test_a_purchase_like_click_always_asks_the_user_first(element):
    # Auto mode's classifier approves clicks on its own; anything that may spend money goes to the user.
    b, reg, relay = make()
    out, qs = _answer(b, reg, _click(element), "allow")
    assert len(qs) == 1 and relay.raised == [qs[0]["id"]]
    assert out["hookSpecificOutput"] == {"hookEventName": "PreToolUse", "permissionDecision": "allow"}


@pytest.mark.parametrize("url", ["https://secure.booking.com/book.html", "https://www.opentable.com/booking/details"])
def test_a_booking_page_gates_even_a_harmless_click(url):
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_navigate",
                 "tool_response": f"- Page URL: {url}\n"})
    out, qs = _answer(b, reg, _click("Continue"), "allow")
    assert len(qs) == 1 and url in qs[0]["prompt"]


def test_a_no_or_no_answer_blocks_the_purchase():
    b, reg, _ = make(timeout=0.05)
    out, _ = _answer(b, reg, _click("Place order"), "deny")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out, _ = _answer(b, reg, _click("Pay now", target="e13"), None)  # times out
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_ordinary_click_is_left_to_claude_code():
    b, reg, relay = make()
    for el in ("Search", "Next page", "Sort by price", "Facebook", "Bookmarks", "Bookmark this page",
               "Add to cart", "Reorder", "Tickets FAQ", "Facebook login"):
        out, qs = _answer(b, reg, _click(el), None)
        assert out == {} and qs == [], el
    assert relay.raised == []


def test_the_question_names_the_real_target_and_the_page():
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_navigate",
                 "tool_response": [{"type": "text", "text": "- Page URL: https://shop.example/checkout\n"}]})
    out, qs = _answer(b, reg, _click("Continue", target="e42"), "allow")  # harmless words, but a checkout page
    assert qs[0]["prompt"] == "browser_click: Continue (ref e42) on https://shop.example/checkout"


SNAPSHOT = """- Page URL: https://shop.example/cart
- Page Title: Cart
### Snapshot
```yaml
- generic [ref=e2]:
  - heading "Your cart" [level=1] [ref=e3]
  - button "Continue shopping" [ref=e41] [cursor=pointer]
  - button "Pay ₹5000" [ref=e42] [cursor=pointer]
```
"""


def test_a_click_is_judged_by_the_snapshots_real_name_not_the_agents_description():
    # The agent writes `element` itself: "Continue" on a ref that is really the "Pay ₹5000" button.
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot",
                 "tool_response": [{"type": "text", "text": SNAPSHOT}]})
    out, qs = _answer(b, reg, _click("Continue", target="e42"), "deny")
    assert len(qs) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert 'button "Pay ₹5000"' in qs[0]["prompt"] and "e42" in qs[0]["prompt"]
    # The real "Continue shopping" button on the same page is left alone.
    assert _answer(b, reg, _click("Continue", target="e41"), None) == ({}, [])


SNAP_FILE = "./.playwright-mcp/page-2026-11-10T12-00-00-000Z.yml"
# A page-changing step's response in 0.0.83: the snapshot is written to a file and only linked.
LINKED = f"""### Ran Playwright code
```js
await page.getByRole('link', {{ name: 'Cart' }}).click();
```
### Page
- Page URL: https://shop.example/cart
- Page Title: Cart
### Snapshot
- [Snapshot]({SNAP_FILE})"""


def test_a_snapshot_linked_as_a_file_is_read_when_the_step_is_judged(tmp_path):
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "cwd": str(tmp_path), "tool_name": "mcp__browser__browser_click",
                 "tool_response": [{"type": "text", "text": LINKED}]})
    f = tmp_path / SNAP_FILE
    f.parent.mkdir()
    f.write_text(SNAPSHOT.split("```yaml\n")[1].split("```")[0])  # read at PreToolUse time, not when linked
    out, qs = _answer(b, reg, _click("Continue", target="e42"), "deny")
    assert len(qs) == 1 and 'button "Pay ₹5000"' in qs[0]["prompt"]


def test_a_linked_file_outside_the_errands_folder_is_never_read(tmp_path):
    b, _, _ = make()
    (tmp_path / "w").mkdir()
    (tmp_path / "x.yml").write_text('- button "Pay" [ref=e42]')
    b.note_page({"session_id": "s1", "cwd": str(tmp_path / "w"), "tool_name": "mcp__browser__browser_click",
                 "tool_response": "- Page URL: https://a.example/\n- [Snapshot](../x.yml)"})
    assert b._snapshot_text("s1") == ""


def test_a_ref_inside_a_name_or_text_is_never_taken_for_the_nodes_own():
    # Page text can say "[ref=e42]": only the line whose own attributes hold it is e42.
    b, reg, _ = make()
    spoof = """### Snapshot
```yaml
- paragraph [ref=e40]: Continue [ref=e42] to the next step
- link "Say \\"hi\\" [ref=e42]" [ref=e41] [cursor=pointer]
- button "Pay ₹5000" [ref=e42] [cursor=pointer]
```"""
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot", "tool_response": spoof})
    out, qs = _answer(b, reg, _click("Continue", target="e42"), "deny")
    assert len(qs) == 1 and 'button "Pay ₹5000"' in qs[0]["prompt"] and "paragraph" not in qs[0]["prompt"]


def test_a_new_page_without_a_snapshot_forgets_the_old_one():
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot", "tool_response": SNAPSHOT})
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_navigate_back",
                 "tool_response": "### Page\n- Page URL: https://news.example/\n- Page Title: News"})
    assert _answer(b, reg, _click("Continue", target="e42"), None) == ({}, [])  # the old page's Pay is gone


def test_a_selector_target_is_judged_by_its_own_words_and_the_old_ref_key_still_works():
    b, reg, _ = make()
    out, qs = _answer(b, reg, _click("Continue", target="text=Pay now"), "deny")
    assert len(qs) == 1 and "text=Pay now" in qs[0]["prompt"]
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot", "tool_response": SNAPSHOT})
    old = {"session_id": "s1", "cwd": "/w", "tool_name": "mcp__browser__browser_click",
           "tool_input": {"element": "Continue", "ref": "e42"}}
    out, qs = _answer(b, reg, old, "deny")
    assert len(qs) == 1 and 'button "Pay ₹5000"' in qs[0]["prompt"]


def test_the_kept_snapshot_is_bounded():
    b, _, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot",
                 "tool_response": "- button \"x\" [ref=e1]\n" + "a" * 1_000_000})
    assert len(b._snapshots["s1"]) <= 200_000


def test_form_fills_and_key_presses_are_gated_too():
    b, reg, _ = make()
    fill = _click(None, tool="mcp__browser__browser_fill_form",
                  fields=[{"name": "Card number", "target": "e3", "value": "4111"}, {"name": "Pay", "target": "e4"}])
    out, qs = _answer(b, reg, fill, "allow")
    assert len(qs) == 1 and "4111" not in qs[0]["prompt"]  # field names, never their values
    b.note_page({"session_id": "s2", "tool_name": "mcp__browser__browser_click",
                 "tool_response": "- Page URL: https://shop.example/order/review"})
    out, qs = _answer(b, reg, _click(None, sid="s2", tool="mcp__browser__browser_press_key", key="Enter"), "deny")
    assert len(qs) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_every_run_gates_purchase_steps_through_the_pre_tool_hook(tmp_path):
    import re
    from hooks import PRE_TOOL_MATCHER
    hooks = json.loads(Path(write_run_settings(tmp_path, "a", "tok", 7860, hook_timeout=100)).read_text())["hooks"]
    (entry,) = hooks["PreToolUse"]
    assert entry["hooks"][0]["url"].endswith("/hook/tok/PreToolUse") and entry["hooks"][0]["timeout"] == 100
    for tool in ("mcp__browser__browser_click", "mcp__browser__browser_fill_form", "mcp__browser__browser_press_key",
                 "mcp__browser__browser_type", "mcp__browser__browser_keydown", "mcp__browser__browser_select_option",
                 "mcp__browser__browser_press_sequentially", "mcp__browser__browser_handle_dialog"):
        assert re.search(entry["matcher"], tool), tool  # Claude Code tests it as a JS regex
    assert not re.search(PRE_TOOL_MATCHER, "mcp__browser__browser_clickx")


def test_the_route_hands_pre_tool_use_to_the_bridge():
    b, reg, _ = make()
    app = FastAPI()
    app.include_router(make_router(b, "tok"))
    assert TestClient(app).post("/hook/tok/PreToolUse", json=_click("Search")).json() == {}


def test_typing_is_gated_only_when_it_submits():
    b, reg, _ = make()
    typed = dict(tool="mcp__browser__browser_type", text="4111 1111")
    out, qs = _answer(b, reg, _click("Pay with card", **typed), None)  # no submit: just text in a box
    assert out == {} and qs == []
    out, qs = _answer(b, reg, _click("Pay with card", submit=True, **typed), "allow")
    assert len(qs) == 1 and "4111" not in qs[0]["prompt"]  # never the typed text
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("tool,inp", [("mcp__browser__browser_keydown", {"key": "Enter"}),
                                      ("mcp__browser__browser_select_option", {"values": ["Monthly"]})])
def test_key_downs_and_dropdowns_on_a_checkout_page_are_gated(tool, inp):
    b, reg, _ = make()
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_click",
                 "tool_response": "- Page URL: https://shop.example/checkout"})
    out, qs = _answer(b, reg, _click(None, tool=tool, **inp), "deny")
    assert len(qs) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_broken_pre_tool_hook_denies_rather_than_lets_it_through():
    b, reg, _ = make()

    async def boom(p):
        raise RuntimeError("bridge bug")

    b.on_pre_tool_use = boom
    app = FastAPI()
    app.include_router(make_router(b, "tok"))
    c = TestClient(app)
    for r in (c.post("/hook/tok/PreToolUse", json=_click("Pay now")),
              c.post("/hook/tok/PreToolUse", content=b"not json", headers={"content-type": "application/json"})):
        assert r.json()["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert c.post("/hook/tok/Stop", content=b"not json").json() == {}  # other events: no decision, as before


def test_typing_key_by_key_is_gated_only_when_it_submits():
    b, reg, _ = make()
    typed = dict(tool="mcp__browser__browser_press_sequentially", text="4111 1111")
    assert _answer(b, reg, _click("Pay with card", **typed), None) == ({}, [])
    out, qs = _answer(b, reg, _click("Pay with card", submit=True, **typed), "deny")
    assert len(qs) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_accepting_a_site_dialog_always_asks_and_dismissing_it_never_does():
    # confirm("Pay ₹1,999?") after a harmless-looking click: the dialog's text never reaches the hook
    b, reg, _ = make()
    dialog = dict(tool="mcp__browser__browser_handle_dialog")
    assert _answer(b, reg, _click(None, accept=False, **dialog), None) == ({}, [])
    out, qs = _answer(b, reg, _click(None, accept=True, **dialog), "deny")
    assert len(qs) == 1 and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "pop-up" in qs[0]["prompt"] and "{" not in qs[0]["prompt"]  # said aloud: no raw JSON


def _yaml_key(key):
    """Playwright's yamlEscapeKeyIfNeeded (coreBundle.js, packages/isomorphic/yaml.ts), ported to build test lines."""
    def needs(s):
        try:
            float(s)
            numeric = True
        except ValueError:
            numeric = False
        return bool(not s or re.search(r"^\s|\s$", s) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", s)
                    or s.startswith("-") or re.search(r"[\n:](\s|$)", s) or re.search(r"\s#", s)
                    or re.search(r"[\n\r]", s) or re.match(r"""[&*\],?!>|@"'#%]""", s) or re.search(r"[{}`]", s)
                    or s.startswith("[") or numeric
                    or s.lower() in ("y", "n", "yes", "no", "true", "false", "on", "off", "null"))
    return "'" + key.replace("'", "''") + "'" if needs(key) else key


def _pw_line(role, name, ref, text=None):
    """One snapshot line exactly as Playwright MCP 0.0.83 writes it (renderAriaSnapshotAsYaml)."""
    key = _yaml_key(f"{role} {json.dumps(name, ensure_ascii=False)} [ref={ref}] [cursor=pointer]")
    return f"- {key}" + (f": {text}" if text is not None else "")


@pytest.mark.parametrize("name", ["Pay: ₹5000", "Total: ₹499 – Pay", "#1 Buy now", "Item #1 Buy now",
                                  "Pay `now`", "Pay {₹500}", "Rahul's order: Pay ₹500", "Pay: ''₹500''"])
def test_a_name_playwright_wraps_in_single_quotes_is_read_as_the_page_wrote_it(name):
    from hooks import snapshot_node
    line = _pw_line("button", name, "e7")
    assert snapshot_node(f"- generic [ref=e2]:\n  {line}", "e7") == ("button", name, "")
    assert snapshot_node(_pw_line("link", name, "e7", text="it's here"), "e7") == ("link", name, "it's here")


def test_a_quoted_pay_button_asks_even_when_the_agent_calls_it_continue():
    b, reg, _ = make()
    snap = "### Snapshot\n```yaml\n" + _pw_line("button", "Pay: ₹5000", "e7") + "\n```"
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot", "tool_input": {},
                 "tool_response": [{"type": "text", "text": snap}]})
    out, qs = _answer(b, reg, _click("Continue", target="e7"), "deny")
    assert len(qs) == 1 and 'button "Pay: ₹5000"' in qs[0]["prompt"]


def test_a_node_whose_long_name_was_dropped_is_judged_on_the_agents_words_and_the_page():
    from hooks import snapshot_node
    assert snapshot_node("- button [ref=e7] [cursor=pointer]", "e7") == ("button", "", "")
    b, reg, _ = make()
    snap = "### Snapshot\n```yaml\n- button [ref=e7] [cursor=pointer]\n```"
    b.note_page({"session_id": "s1", "tool_name": "mcp__browser__browser_snapshot", "tool_input": {},
                 "tool_response": snap})
    assert _answer(b, reg, _click("Continue", target="e7"), None) == ({}, [])
    out, qs = _answer(b, reg, _click("Pay now", target="e7"), "deny")
    assert len(qs) == 1
