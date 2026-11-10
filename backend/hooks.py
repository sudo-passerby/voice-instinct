"""Claude Code hook receiver: records events and relays permission requests to the user."""

import asyncio
import hmac
import json
import os
import re
import secrets
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from debugbrowser import _texts, page_url

_EVENTS = ("PermissionRequest", "Stop", "SessionEnd", "SessionStart")
# A browser step that may spend money or commit the user always goes to the user first (PreToolUse), whatever
# auto mode's classifier would say: matched on the element's text, the form's field names and the page's URL.
# browser_type and browser_press_sequentially only when they submit (press Enter after); accepting a site's own
# dialog (confirm("Pay ₹…?")) always asks, since its text is not in the tool input; browser_evaluate is denied
# outright (autonomy.py).
PRE_TOOL_MATCHER = ("^mcp__browser__browser_(click|fill_form|press_key|type|press_sequentially|keydown"
                    "|select_option|handle_dialog)$")
_SUBMITS_ONLY = ("mcp__browser__browser_type", "mcp__browser__browser_press_sequentially")
# Whole words: "add to cart", "reorder" and "bookmark" stay unmatched. Keep README.md's list in step with this one.
MONEY_WORDS = re.compile(r"\b(pay|payment|buy|purchase|complete purchase|order|checkout|check out|subscribe"
                         r"|subscription|trial|upgrade|renew|confirm|confirm booking|book|booking|book now"
                         r"|complete booking|reserve|reservation|request ride|get tickets|donate|bid|place bid)\b",
                         re.I)
# Debug mode's step hook (steps.py): every tool the agent uses. steps.SCREENSHOT_TOOLS picks the ones
# that change the page and get a screenshot.
STEP_MATCHER = "*"
SNAPSHOT_CAP = 200_000  # characters of the latest page snapshot kept per session


def _refs(inp):
    """Every page ref a browser step acts on: its own and its form fields'."""
    return [str(r) for r in (inp.get("ref"), *[f.get("ref") for f in inp.get("fields") or [] if isinstance(f, dict)])
            if r]


def snapshot_line(snapshot, ref):
    """The snapshot's line for [ref=<ref>] (Playwright MCP's aria YAML: `- button "Pay ₹5000" [ref=e42] ...`),
    without its leading dash, or None."""
    m = re.search(rf"^\s*- (.*\[ref={re.escape(ref)}\].*)$", snapshot or "", re.M)
    return m.group(1).strip() if m else None


def _real_name(line):
    """`button "Pay ₹5000"` from a snapshot line: the role and accessible name, no [attributes] or children."""
    head = line.split(" [ref=")[0]
    return re.sub(r"( \[[\w-]+(=[^\]]*)?\])+$", "", head)


def hook_response(decision):
    if decision == "allow":
        d = {"behavior": "allow"}
    elif decision == "deny":
        d = {"behavior": "deny", "message": "The user declined this action by voice."}
    else:
        return {}  # no decision: Claude falls back to its normal behavior
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


def pre_tool_response(decision):
    if decision == "allow":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "The user did not approve this step by voice."}}


def _browser_target(inp):
    """What a browser step acts on, as its tool input names it. Never typed text or form values."""
    fields = [str(f.get("name")) for f in inp.get("fields") or [] if isinstance(f, dict) and f.get("name")]
    return " ".join(str(x) for x in (inp.get("element"), *fields, inp.get("key")) if x)


def describe(tool_name, tool_input, page=None, real=None):
    inp = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
    if tool_name == "mcp__browser__browser_handle_dialog":  # the dialog's own text never reaches the hook
        return "accept a pop-up the site opened" + (f" on {page}" if page else "") + (
            f", answering {str(inp['promptText'])[:80]!r}" if inp.get("promptText") else "") + " (it may confirm a payment)"
    if str(tool_name).startswith("mcp__browser__") and (inp.get("element") or inp.get("fields") or inp.get("key")):
        # The element text is the agent's own description: give the page ref it really targets as well.
        notes = [f"ref {inp['ref']}"] if inp.get("ref") else []
        if real:  # the page's own name for what it targets, from the latest snapshot
            notes.append(f"on the page: {'; '.join(real)}")
        ref = f" ({', '.join(notes)})" if notes else ""
        detail = f"{_browser_target(inp)}{ref}" + (f" on {page}" if page else "")
        return f"{str(tool_name).removeprefix('mcp__browser__')}: {detail}"
    detail = (inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("url")
              or inp.get("query") or json.dumps(inp, default=str)[:200])
    return f"{tool_name}: {detail}"


class HookBridge:
    def __init__(self, registry, relay, permission_timeout=90):
        self.reg, self.relay, self.timeout = registry, relay, permission_timeout
        self._waiters: dict[int, asyncio.Future] = {}
        self._pages: dict[str, str] = {}  # session -> the page its browser last reported (PostToolUse)
        self._snapshots: dict[str, str] = {}  # session -> its latest page snapshot text, capped

    def resolve(self, qid, decision):
        fut = self._waiters.get(qid)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        return True

    def expire_session(self, session_id):
        """Release hooks still waiting on this session (its run ended or was stopped)."""
        self._pages.pop(session_id, None)
        self._snapshots.pop(session_id, None)
        for qid, fut in list(self._waiters.items()):
            q = self.reg.get_question(qid)
            if q and q["session_id"] == session_id and not fut.done():
                fut.set_result(None)

    async def on_permission_request(self, p):
        sid = p["session_id"]
        logger.info(f"hook PermissionRequest ({sid[:8]})")
        self.reg.upsert_session(sid, cwd=p.get("cwd"))
        self.reg.add_event(sid, "PermissionRequest", p)
        return hook_response(await self.ask(sid, "permission", describe(p.get("tool_name"), p.get("tool_input")),
                                            p.get("tool_name"), p.get("tool_input")))

    def note_page(self, p):
        """PostToolUse: remember the page the errand browser is on and its latest snapshot, for on_pre_tool_use."""
        sid = p.get("session_id")
        if not sid or not str(p.get("tool_name", "")).startswith("mcp__browser__"):
            return
        if url := page_url(p.get("tool_response")):
            self._pages[sid] = url
        snap = "\n".join(t for t in _texts(p.get("tool_response")) if "[ref=" in t)
        if snap:  # Playwright MCP sends the full snapshot after each page-changing step (--snapshot-mode full)
            self._snapshots[sid] = snap[:SNAPSHOT_CAP]

    async def on_pre_tool_use(self, p):
        """A click, form fill or key press that may pay, buy, order or book asks the user first. Otherwise no
        decision: Claude Code goes on as usual."""
        sid, inp = p.get("session_id"), p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        tool = p.get("tool_name")
        if tool in _SUBMITS_ONLY and not inp.get("submit"):
            return {}  # text in a box commits nothing; submitting it is a key press
        if tool == "mcp__browser__browser_handle_dialog" and not inp.get("accept"):
            return {}  # dismissing a dialog commits nothing
        page = self._pages.get(sid)
        dialog = tool == "mcp__browser__browser_handle_dialog"
        # `element` is written by the agent: also judge the line the page itself has for each ref.
        lines = [ln for r in _refs(inp) if (ln := snapshot_line(self._snapshots.get(sid), r))]
        if not sid or not (dialog or MONEY_WORDS.search(" ".join([_browser_target(inp), page or "", *lines]))):
            return {}
        logger.info(f"hook PreToolUse: a purchase-like step ({sid[:8]})")
        self.reg.upsert_session(sid, cwd=p.get("cwd"))
        self.reg.add_event(sid, "PreToolUse", p)
        real = [_real_name(ln) for ln in lines]
        return pre_tool_response(await self.ask(sid, "permission", describe(p.get("tool_name"), inp, page, real),
                                                p.get("tool_name"), inp))

    async def ask(self, sid, kind, prompt, tool_name=None, tool_input=None):
        """Put a question to the user through the relay and wait for its answer (what answer_question resolved
        it with), or None if it timed out or was released. The same question delivered twice is asked once."""
        qid = self.reg.add_question(sid, kind, prompt, tool_name, tool_input)
        fut = self._waiters.get(qid)
        first = fut is None
        decision = None
        timed_out = False
        try:
            if first:
                fut = self._waiters[qid] = asyncio.get_running_loop().create_future()
                self.reg.set_state(sid, "waiting_input")
                await self.relay.raise_question(qid)
            decision = await asyncio.wait_for(asyncio.shield(fut), self.timeout)
        except asyncio.TimeoutError:
            decision, timed_out = None, True
        finally:  # also runs on cancellation or a relay failure: never leave a zombie question
            if first:  # only the first delivery closes the question
                self._waiters.pop(qid, None)
                self.reg.resolve_question(qid, "answered" if decision else "expired", decision)
                s = self.reg.get_session(sid)
                if s and s["state"] == "waiting_input":  # do not resurrect an ended session
                    # A folder question comes before its run starts (delegate.py), so no run is going yet.
                    self.reg.set_state(sid, "idle" if kind == "folder" else "running")
                self.relay.question_closed(qid, expired=timed_out)
                if not fut.done():
                    fut.set_result(decision)
        return decision

    async def on_event(self, event, p):
        sid = p.get("session_id")
        if not sid:
            return {}
        self.reg.upsert_session(sid, cwd=p.get("cwd"))
        self.reg.add_event(sid, event, p)
        if event == "Stop":
            self.reg.set_state(sid, "idle")
        elif event == "SessionEnd":
            self.reg.set_state(sid, "ended", end_reason=p.get("reason"))
        elif event == "SessionStart":
            self.reg.set_state(sid, "running")
        return {}


def make_router(bridge, token, steps=None, budget=None):
    router = APIRouter()

    @router.post("/hook/{tok}/{event}")
    async def hook(tok: str, event: str, request: Request):
        if not hmac.compare_digest(tok.encode(), token.encode()):  # str form raises on non-ASCII
            raise HTTPException(status_code=404)
        # The purchase guard fails closed: a step it could not judge is denied, never let through.
        failed = pre_tool_response(None) if event == "PreToolUse" else {}
        try:
            payload = await request.json()
        except Exception:
            return failed
        try:
            if event == "PreToolUse":
                return await bridge.on_pre_tool_use(payload)
            if event == "PostToolUse":  # debug mode's step record (steps.py), then the time budget (budget.py)
                bridge.note_page(payload)
                if steps:
                    await steps.on_step(payload)
                return budget.on_step(payload) if budget else {}
            if event == "PermissionRequest" and payload.get("session_id"):
                return await bridge.on_permission_request(payload)
            return await bridge.on_event(event, payload)
        except Exception as e:  # never let a bridge bug break the user's Claude session
            logger.error(f"hook {event} failed: {e!r}")
            return failed

    return router


_TOKEN = re.compile(r"/hook/[^/?\s]+")  # with or without an event after it


def hide_hook_tokens(record):
    """Logging filter for uvicorn's access log: keep hook calls (a refused one must be visible) but
    blank out the secret token in their URL."""
    if isinstance(record.args, tuple):
        record.args = tuple(_TOKEN.sub("/hook/…", a) if isinstance(a, str) else a for a in record.args)
    elif "/hook/" in str(record.msg):
        record.msg = _TOKEN.sub("/hook/…", str(record.msg))
    return True


def load_token(path):
    p = Path(path)
    if p.exists():
        p.chmod(0o600)  # someone may have loosened it
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


def write_run_settings(directory, session_id, token, port, hook_timeout=100, extra=None):
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}/hook/{token}"
    hooks = {e: [{"hooks": [{"type": "http", "timeout": hook_timeout, "url": f"{url}/{e}"}]}] for e in _EVENTS}
    # Every run: the time budget answers through it, and in debug mode it also drives the step record.
    # 15 s: a debug screenshot is capped at 10 (steps.SHOT_TIMEOUT).
    hooks["PostToolUse"] = [{"matcher": STEP_MATCHER,
                             "hooks": [{"type": "http", "timeout": 15, "url": f"{url}/PostToolUse"}]}]
    hooks["PreToolUse"] = [{"matcher": PRE_TOOL_MATCHER,  # waits for the user, like PermissionRequest
                            "hooks": [{"type": "http", "timeout": hook_timeout, "url": f"{url}/PreToolUse"}]}]
    path = d / f"{session_id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # the URL holds the token
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"hooks": hooks, **(extra or {})}))  # extra: sandbox + deny rules
    return str(path)
