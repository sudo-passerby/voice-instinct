"""Claude Code hook receiver: records events and relays permission requests to the user."""

import asyncio
import hmac
import json
import os
import re
import secrets
import stat
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
SNAPSHOT_CAP = 200_000  # characters of the latest page snapshot kept per session (bytes, read from its file)
SNAPSHOT_READ_TIMEOUT = 2  # seconds to read a snapshot file; bot.py adds it to the hook's own timeout


_IS_REF = re.compile(r"^(f\d+)?e\d+$")  # Playwright MCP 0.0.83's own test (targetLocators): else it is a selector
# A snapshot node (Playwright's aria YAML, renderAriaSnapshotAsYaml): `- role "name" [attr] [attr]...`, then an
# optional `: text`. The name is JSON-quoted, so a quote inside it is escaped.
_NODE = re.compile(r'^\s*- ([^\s":\[]+)(?: ("(?:[^"\\]|\\.)*"))?((?: \[[^\]\s]*\])*)(?::\s?(.*))?$')
# yamlEscapeKeyIfNeeded wraps the whole key in single quotes (a ' inside doubled) when the name holds ": ", " #",
# {, }, a backtick and the like: `- 'button "Pay: ₹5000" [ref=e7]'`, then the same optional `: text`.
_QUOTED_KEY = re.compile(r"^(\s*- )'((?:[^']|'')*)'(:.*)?$")
_SNAPSHOT_LINK = re.compile(r"^- \[Snapshot\]\((.+)\)$", re.M)  # a page-changing step's snapshot, in a file


def _targets(inp):
    """What a browser step names as its element(s): `target` (0.0.83, which install.sh pins), or the older `ref`;
    its own and its form fields'."""
    items = [inp, *[f for f in inp.get("fields") or [] if isinstance(f, dict)]]
    return [str(t) for i in items if (t := i.get("target") or i.get("ref"))]


def _refs(inp):
    return [t for t in _targets(inp) if _IS_REF.match(t)]


def snapshot_node(snapshot, ref):
    """(role, name, text) of the node whose own attributes hold [ref=<ref>], or None. A "[ref=...]" inside a
    node's quoted name or its text is page content, never taken for the node's ref."""
    want = f"ref={ref}"
    for line in (snapshot or "").splitlines():
        if q := _QUOTED_KEY.match(line):
            line = q.group(1) + q.group(2).replace("''", "'") + (q.group(3) or "")
        if f"[{want}]" not in line or not (m := _NODE.match(line)):
            continue
        if want in re.findall(r"\[([^\]]*)\]", m.group(3) or ""):
            try:
                name = json.loads(m.group(2)) if m.group(2) else ""
            except ValueError:
                name = m.group(2)[1:-1]
            return m.group(1), str(name), (m.group(4) or "").strip()
    return None


def _read_snapshot(path):
    """A snapshot file's text, capped. Never follows a symlink and never waits on a FIFO or a device: only a
    regular file is read."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        data = b""
        while len(data) < SNAPSHOT_CAP and (chunk := os.read(fd, SNAPSHOT_CAP - len(data))):
            data += chunk
    finally:
        os.close(fd)
    return data.decode("utf-8", errors="replace")


def _real_name(node):
    """`button "Pay ₹5000"`: the role and accessible name (or its text), as said in the question."""
    role, name, text = node
    return f'{role} "{name}"' if name else f"{role}: {text[:80]}" if text else role


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
    selectors = [t for t in _targets(inp) if not _IS_REF.match(t)]  # a selector is the agent's own words too
    return " ".join(str(x) for x in (inp.get("element"), *fields, *selectors, inp.get("key")) if x)


def describe(tool_name, tool_input, page=None, real=None):
    inp = tool_input if isinstance(tool_input, dict) else {"input": tool_input}
    if tool_name == "mcp__browser__browser_handle_dialog":  # the dialog's own text never reaches the hook
        return "accept a pop-up the site opened" + (f" on {page}" if page else "") + (
            f", answering {str(inp['promptText'])[:80]!r}" if inp.get("promptText") else "") + " (it may confirm a payment)"
    if str(tool_name).startswith("mcp__browser__") and (inp.get("element") or inp.get("fields") or inp.get("key")
                                                       or _targets(inp)):
        # The element text is the agent's own description: give the page ref it really targets as well.
        own = inp.get("target") or inp.get("ref")
        notes = [f"ref {own}"] if own and _IS_REF.match(str(own)) else []
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
        self._snapshots: dict[str, str | Path] = {}  # session -> its latest snapshot: text (capped) or its file
        self._roots: dict[str, Path] = {}  # session -> the errand folder delegate made for its run (register_run)

    def register_run(self, session_id, workdir):
        """The folder this session's run works in, as delegate made it (None: the run ended). Only its
        .playwright-mcp snapshots are ever read; never a folder a hook payload names."""
        if workdir is None:
            self._roots.pop(session_id, None)
        else:
            self._roots[session_id] = Path(workdir)

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
        texts = list(_texts(p.get("tool_response")))
        url = page_url(texts)
        if url:
            self._pages[sid] = url
        inline = "\n".join(t for t in texts if "[ref=" in t)  # browser_snapshot: the snapshot itself
        link = next((m.group(1) for t in texts if (m := _SNAPSHOT_LINK.search(t))), None)
        if inline:
            self._snapshots[sid] = inline[:SNAPSHOT_CAP]
        elif link and (f := self._snapshot_file(link, self._roots.get(sid))):
            self._snapshots[sid] = f  # a page-changing step: a page-*.yml link, read when a step is judged
        elif link:
            self._snapshots.pop(sid, None)  # a file it may not read: never say an older snapshot's names
        elif url:
            self._snapshots.pop(sid, None)  # a new page and no snapshot of it: never say the old page's names

    @staticmethod
    def _snapshot_file(link, root):
        """The linked snapshot file, only a regular file in <root>/.playwright-mcp: Playwright MCP writes it
        there (its cwd is the run's folder) and the sandbox denies that folder to the agent (autonomy.py); any
        other .yml the agent could have written. ponytail: its tmpdir fallback (cwd not writable) is not followed;
        the click is then judged on the agent's words and the page URL."""
        if root is None:
            return None
        try:
            root = Path(root).resolve()
            f = (root / link).resolve()  # a symlink resolves to where it points, so one out is refused
            ok = f.suffix == ".yml" and f.is_relative_to(root / ".playwright-mcp") and f.is_file()
        except (OSError, ValueError, RuntimeError):
            return None
        return f if ok else None

    async def _snapshot_text(self, sid):
        """The latest snapshot's text, capped; a linked file is read now (the hook runs outside the sandbox),
        off the event loop. Raises if it can't be read in time."""
        snap = self._snapshots.get(sid)
        if isinstance(snap, Path):
            return await asyncio.wait_for(asyncio.to_thread(_read_snapshot, snap), SNAPSHOT_READ_TIMEOUT)
        return snap or ""

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
        try:
            snapshot = await self._snapshot_text(sid) if _refs(inp) else ""
        except (OSError, TimeoutError) as e:  # a guard that can't see the page can't judge the step: deny it
            logger.warning(f"hook PreToolUse: snapshot unreadable, step denied ({e!r})")
            return pre_tool_response(None)
        nodes = [n for r in _refs(inp) if (n := snapshot_node(snapshot, r))]
        if not (dialog or MONEY_WORDS.search(" ".join([_browser_target(inp), page or "",
                                                         *(" ".join(n) for n in nodes)]))):
            return {}
        if not sid:  # no session to ask in
            return pre_tool_response(None)
        logger.info(f"hook PreToolUse: a purchase-like step ({sid[:8]})")
        self.reg.upsert_session(sid, cwd=p.get("cwd"))
        self.reg.add_event(sid, "PreToolUse", p)
        real = [_real_name(n) for n in nodes]
        # The real target's name, the page's own when the snapshot knows it, for relay: only a target that cancels
        # or stops lets "yes, cancel it" approve (confirm.names_a_cancellation). Never sent back to Claude.
        name = (nodes[0][1] or nodes[0][2]) if nodes else str(inp.get("element") or "")
        return pre_tool_response(await self.ask(sid, "permission", describe(p.get("tool_name"), inp, page, real),
                                                p.get("tool_name"), {**inp, "_target_name": name}))

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
