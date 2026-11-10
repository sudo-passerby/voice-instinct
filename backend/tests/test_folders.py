"""Folder grants (folders.py): an errand uses only its private workspace unless the user says yes, in their own words."""

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from autonomy import DELETE_COMMANDS, consolidation_settings, sandbox_settings
from claude_cli import BackendResult
from confirm import grant_answer
from delegate import make_tools
from folders import Folders, make_folder_tools, refusal
from hooks import HookBridge
from registry import Registry
from relay import Relay, make_bridge_tools


class FakeTask:
    def __init__(self, ctx):
        self.ctx = ctx

    async def queue_frames(self, frames):  # the real pipeline lands the question in the context
        for f in frames:
            self.ctx.msgs += getattr(f, "messages", [])


class Ctx:
    def __init__(self, *said):
        self.msgs = [{"role": "user", "content": t} for t in said]

    def get_messages(self):
        return self.msgs


class Backend:
    timeout = 300

    def __init__(self):
        self.calls = []

    async def run(self, task, session_id=None, resume=False, settings=None, cwd=None, mcp_config=None, **kw):
        self.calls.append({"task": task, "sid": session_id, "resume": resume, "settings": settings, "cwd": cwd})
        return BackendResult("done")


class World:
    """The real consent path: HookBridge + Relay + answer_question, and delegate's own tools."""

    def __init__(self, home, *said):
        self.home = Path(home)
        self.reg, self.ctx = Registry(), Ctx(*said)
        self.relay = Relay(self.reg)
        self.relay.attach(FakeTask(self.ctx), self.ctx)
        self.bridge = HookBridge(self.reg, self.relay, permission_timeout=5)
        self.folders = self.fresh_folders()
        self.backend, self.grants = Backend(), []

        def settings_for(sid, debug_cwd=None, grant=None):
            self.grants.append(grant)
            return None

        (self.delegate,) = make_tools(self.backend, self.reg, settings_for, workspace_root=self.ws, home=self.home,
                                      bridge=self.bridge, folders=self.folders)
        self.answer = make_bridge_tools(self.bridge, self.relay, self.reg, None)[0]
        self.allow_folder, self.list_folders, self.revoke_folder = make_folder_tools(
            self.folders, self.bridge, self.reg, self.home)

    @property
    def ws(self):
        return self.home / ".voice-instinct" / "workspace"

    def fresh_folders(self):  # what a new delegate call sees: the file, read again
        data = self.home / ".voice-instinct"
        return Folders(data / "folders.json", self.home, data, self.home / "voice-instinct", data / "workspace")

    def params(self):
        out = []

        async def cb(r, **kw):
            out.append(r)

        return SimpleNamespace(result_callback=cb, context=self.ctx), out

    async def call(self, tool, *args, reply=None, **kw):
        """Run a tool; if it asks a question, the user answers `reply` (None: the tool must not ask)."""
        p, out = self.params()
        ran_before = len(self.backend.calls)
        t = asyncio.create_task(tool(p, *args, **kw))
        for _ in range(20):
            await asyncio.sleep(0)
        asked = self.reg.open_questions()
        if reply is not None:
            assert asked and len(self.backend.calls) == ran_before, "the run must not start before the answer"
            self.ctx.msgs.append({"role": "assistant", "content": "May I?"})
            self.ctx.msgs.append({"role": "user", "content": reply})
            await self.answer(self.params()[0], asked[0]["id"])
        await asyncio.wait_for(t, 2)
        return out, asked

    def go(self, *a, **kw):
        return asyncio.run(self.call(*a, **kw))


@pytest.fixture
def w(tmp_path):
    (tmp_path / "Documents" / "trip").mkdir(parents=True)
    return World(tmp_path, "plan my trip from the notes in my Documents trip folder")


def test_no_folder_means_the_private_workspace_and_no_question(w):
    out, asked = w.go(w.delegate, "find flights")
    assert asked == [] and w.grants == [None] and out[-1]["status"] == "done"
    assert Path(w.backend.calls[0]["cwd"]).is_relative_to(w.ws.resolve())


def test_an_outside_folder_asks_first_and_runs_only_after_a_yes(w):
    out, asked = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="yes")
    (q,) = asked
    assert q["prompt"] == "Can I read files in ~/Documents/trip? I won't change or delete anything. Just this once, or always?"
    folder = (w.home / "Documents" / "trip").resolve()
    assert w.grants == [(folder, "read")] and out[-1]["status"] == "done"
    call = w.backend.calls[0]
    assert Path(call["cwd"]).is_relative_to(w.ws.resolve())  # the shell still starts in the private workspace
    assert str(folder) in call["task"] and "read only" in call["task"]  # the agent is told where and how


def test_write_access_says_it_will_not_delete(w):
    _, (q,) = w.go(w.delegate, "tidy the notes", "~/Documents/trip", access="write", reply="sure")
    assert q["prompt"].startswith("Can I read and change files in ~/Documents/trip? I won't delete anything.")
    assert w.grants[-1][1] == "write"


@pytest.mark.parametrize("reply", ["no", "no thanks", "don't"])
def test_a_no_refuses_and_nothing_runs(w, reply):
    out, asked = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply=reply)
    assert w.backend.calls == [] and out[-1]["status"] == "refused" and "~/Documents/trip" in out[-1]["result"]


def test_once_lasts_for_that_errand_and_its_resumes_only(w):
    out, _ = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="yes, just this once")
    errand = out[-1]["errand"]
    _, asked = w.go(w.delegate, "add the hotel", errand=errand)  # resumed: its grant, no new question
    assert asked == [] and w.grants[-1] == w.grants[0] and w.backend.calls[-1]["resume"] is True
    assert w.fresh_folders().load() == []  # never saved
    _, asked = w.go(w.delegate, "fix a typo in them", errand=errand, access="write", reply="no")  # higher: asks
    assert len(asked) == 1 and w.grants[-1] == w.grants[0]
    _, asked = w.go(w.delegate, "another errand", "~/Documents/trip", reply="no")  # a new errand asks again
    assert len(asked) == 1


def test_always_covers_later_errands_and_survives_a_reload(w):
    w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="yes, always")
    f = w.home / ".voice-instinct" / "folders.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    (g,) = json.loads(f.read_text())["grants"]
    assert (g["path"], g["access"]) == (str((w.home / "Documents/trip").resolve()), "read") and g["since"]
    w2 = World(w.home)  # a restart: a fresh file read
    _, asked = w2.go(w2.delegate, "check the notes", "~/Documents/trip/day1")  # subfolders are covered
    assert asked == [] and w2.grants == [((w.home / "Documents/trip/day1").resolve(), "read")]
    _, asked = w2.go(w2.delegate, "edit them", "~/Documents/trip", access="write", reply="no")  # higher: asks
    assert len(asked) == 1


def test_a_resumed_errand_keeps_an_always_grant(w):
    out, _ = w.go(w.delegate, "plan the trip", "~/Documents/trip", access="write", reply="yes, always")
    folder = (w.home / "Documents/trip").resolve()
    _, asked = w.go(w.delegate, "add the hotel", errand=out[-1]["errand"])  # no directory: its grant, no question
    assert asked == [] and w.grants[-1] == (folder, "write") and w.backend.calls[-1]["resume"] is True


def test_a_resumed_errand_keeps_a_grant_a_standing_one_covered(w):
    w.folders.allow((w.home / "Documents").resolve(), "read")
    out, asked = w.go(w.delegate, "plan the trip", "~/Documents/trip")  # covered: no question
    folder = (w.home / "Documents/trip").resolve()
    assert asked == [] and w.grants[-1] == (folder, "read")
    assert w.reg.get_grant(w.backend.calls[-1]["sid"]) == {"folder": str(folder), "access": "read"}
    _, asked = w.go(w.delegate, "add the hotel", errand=out[-1]["errand"])
    assert asked == [] and w.grants[-1] == (folder, "read")


def test_a_resume_gets_the_higher_of_its_own_and_a_standing_grant(w):
    out, _ = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="yes, just this once")  # once: read
    w.folders.allow((w.home / "Documents").resolve(), "write")  # later, a standing write on the parent
    _, asked = w.go(w.delegate, "fix the notes", errand=out[-1]["errand"])
    assert asked == [] and w.grants[-1] == ((w.home / "Documents/trip").resolve(), "write")


def test_a_resume_that_repeats_the_folder_keeps_its_higher_access(w):
    out, _ = w.go(w.delegate, "tidy the notes", "~/Documents/trip", access="write", reply="yes")
    _, asked = w.go(w.delegate, "and the rest", "~/Documents/trip", errand=out[-1]["errand"])  # access: read
    assert asked == [] and w.grants[-1] == ((w.home / "Documents/trip").resolve(), "write")


def test_a_refused_resume_leaves_the_errand_idle_not_running(w):
    out, _ = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="yes")
    sid = w.backend.calls[-1]["sid"]
    w.go(w.delegate, "fix a typo", errand=out[-1]["errand"], access="write", reply="no")
    assert w.reg.get_session(sid)["state"] == "idle"


def test_a_cancelled_folder_question_leaves_the_errand_idle(w):
    async def go():
        p, _ = w.params()
        t = asyncio.create_task(w.delegate(p, "plan the trip", "~/Documents/trip"))
        for _ in range(20):
            await asyncio.sleep(0)
        (q,) = w.reg.open_questions()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return q["session_id"]

    sid = asyncio.run(go())
    assert w.backend.calls == [] and w.reg.get_session(sid)["state"] == "idle"


def test_a_hand_edit_to_the_file_counts_at_once(w):
    f = w.home / ".voice-instinct" / "folders.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"grants": [{"path": str(w.home / "Documents"), "access": "write"}]}))
    _, asked = w.go(w.delegate, "fix the notes", "~/Documents/trip", access="write")
    assert asked == [] and w.grants[-1][1] == "write"  # write includes read too


def test_the_most_specific_grant_wins(w):
    w.folders._write([{"path": str(w.home / "Documents"), "access": "write"},
                      {"path": str(w.home / "Documents/trip"), "access": "read"}])
    assert w.folders.access(w.home / "Documents/trip/x") == "read"
    assert w.folders.access(w.home / "Documents/other") == "write"
    assert w.folders.access(w.home / "Music") is None


def test_bad_hand_edited_entries_are_ignored(w):
    f = w.home / ".voice-instinct" / "folders.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    good = {"path": str(w.home / "Documents"), "access": "read"}
    f.write_text(json.dumps({"grants": [
        {"path": str(w.home / ".ssh"), "access": "read"}, {"path": str(w.home / "voice-instinct"), "access": "write"},
        {"path": str(w.home / ".voice-instinct"), "access": "read"}, {"path": "/etc", "access": "read"},
        {"path": str(w.home / ".config/x"), "access": "read"}, {"path": str(w.home / "Documents"), "access": "delete"},
        {"path": str(w.home), "access": "read"}, "junk", good]}))
    assert w.folders.load() == [good]
    f.write_text("{not json")
    assert w.folders.load() == []  # unreadable: no standing grants, never a crash


@pytest.mark.parametrize("bad", ["/etc", "~", "~/.config", "~/.local/share/x", "~/.ssh", "~/Documents/.git",
                                 "~/voice-instinct", "~/voice-instinct/backend", "~/.voice-instinct",
                                 "~/.voice-instinct/memory", "~/../elsewhere"])
def test_never_grantable_folders_are_refused_without_even_asking(w, bad):
    out, asked = w.go(w.delegate, "look", bad)
    assert asked == [] and w.backend.calls == [] and out[-1]["status"] == "refused"


def test_a_symlink_into_a_refused_folder_is_refused(w):
    (w.home / ".ssh").mkdir()
    (w.home / "Documents" / "keys").symlink_to(w.home / ".ssh")
    out, asked = w.go(w.delegate, "look", "~/Documents/keys")
    assert asked == [] and out[-1]["status"] == "refused"


def test_a_parent_of_this_apps_own_files_is_refused(tmp_path):
    repo = tmp_path / "Documents" / "voice-instinct"
    repo.mkdir(parents=True)
    data = tmp_path / ".voice-instinct"
    assert refusal((tmp_path / "Documents").resolve(), tmp_path, data, repo, data / "workspace")


def test_delete_is_asked_for_only_when_the_user_said_delete(w):
    out, asked = w.go(w.delegate, "clean up", "~/Documents/trip", access="delete")  # the user never said delete
    assert asked == [] and out[-1]["status"] == "refused"
    w.ctx.msgs.append({"role": "user", "content": "delete the old drafts in my trip folder"})
    out, (q,) = w.go(w.delegate, "delete old drafts", "~/Documents/trip", access="delete", reply="yes always")
    assert q["prompt"] == "Can I delete files in ~/Documents/trip for this errand? I'll ask you before each delete."
    assert w.grants[-1][1] == "delete" and out[-1]["status"] == "done"
    assert w.fresh_folders().load() == []  # "always" to a delete question counts as once


def test_a_grant_up_front_is_saved_only_after_the_users_own_yes(w):
    out, (q,) = w.go(w.allow_folder, "~/Documents", "read", reply="yes")
    assert q["prompt"] == "Can I always read files in ~/Documents, for every errand from now on? I won't change or delete anything."
    assert out[-1]["status"] == "granted" and w.fresh_folders().access(w.home / "Documents") == "read"
    out, _ = w.go(w.allow_folder, "~/Desktop", "write", reply="no")
    assert out[-1]["status"] == "not_granted" and w.fresh_folders().access(w.home / "Desktop") is None
    out, asked = w.go(w.allow_folder, "~/Documents", "delete")  # never standing
    assert asked == [] and out[-1]["status"] == "refused"
    out, _ = w.go(w.list_folders)
    assert [(f["folder"], f["access"]) for f in out[-1]["folders"]] == [("~/Documents", "read")]


@pytest.mark.parametrize("reply", ["just this once", "once", "sure, just this once"])
def test_just_this_once_to_a_standing_question_saves_nothing_and_says_so(w, reply):
    out, _ = w.go(w.allow_folder, "~/Documents", "read", reply=reply)
    assert out[-1]["status"] == "not_granted" and "once" in out[-1]["result"]
    assert w.fresh_folders().load() == []


@pytest.mark.parametrize("reply", ["yes", "sure", "yes, always", "always"])
def test_a_plain_yes_to_a_standing_question_saves_it(w, reply):
    out, _ = w.go(w.allow_folder, "~/Documents", "read", reply=reply)
    assert out[-1]["status"] == "granted" and w.fresh_folders().access(w.home / "Documents") == "read"


def test_revoke_removes_the_grant_without_asking(w):
    w.folders.allow((w.home / "Documents").resolve(), "write")
    out, asked = w.go(w.revoke_folder, "~/Documents")
    assert asked == [] and out[-1]["removed"] == 1 and w.fresh_folders().load() == []
    _, asked = w.go(w.delegate, "edit", "~/Documents/trip", access="write", reply="no")
    assert len(asked) == 1  # it asks again


def test_revoke_also_takes_the_folder_away_from_errands_that_ran_under_it(w):
    # Resumes keep the grant they ran under (registry): "stop accessing Documents" must still stop them.
    out, _ = w.go(w.delegate, "plan the trip", "~/Documents/trip", reply="always")
    w.go(w.revoke_folder, "~/Documents")
    _, asked = w.go(w.delegate, "add the hotel", errand=out[-1]["errand"])
    assert asked == [] and w.grants[-1] is None  # its private workspace only
    _, asked = w.go(w.delegate, "add the hotel", "~/Documents/trip", errand=out[-1]["errand"], reply="no")
    assert len(asked) == 1  # naming the folder again asks again


def test_nova_s_own_questions_never_show_as_errands(w):
    w.go(w.allow_folder, "~/Documents", "read", reply="yes")
    assert w.reg.list_sessions() == []


@pytest.mark.parametrize("said,want", [("yes", "once"), ("sure, just this once", "once"), ("this time only", "once"),
                                       ("always", "always"), ("yes always", "always"), ("from now on, yes", "always"),
                                       ("yeah anytime", "always"), ("not always, just this time", "once"),
                                       ("no", "no"), ("never", "no"), ("always? what does that mean", "unclear"),
                                       ("hmm", "unclear"),
                                       # A negated "once" or "this time" is a refusal, not a one-off yes.
                                       ("Not this time.", "no"), ("not once", "no"), ("Not always.", "no"),
                                       ("not now", "no"), ("not yet", "no"), ("no, not this time", "no"),
                                       ("once", "once"), ("just this once", "once"),
                                       ("ok only this time", "once"), ("haan once", "once"), ("sure always", "always"),
                                       ("sure, for now", "once"), ("go ahead", "once")])
def test_once_or_always_comes_from_the_users_own_words(said, want):
    assert grant_answer(said) == want


# Re-review 2026-11-10: each of these is a refusal and once granted access ("once" or "this time" counted as a yes).
REFUSALS = ["not even once", "not even this time", "not at this time", "Not at this time, thanks",
            "let's not do it this time", "skip it this time", "leave it this time", "deny it this time",
            "I won't allow it this time", "can't allow even once", "refuse this once", "once is not ok",
            "nah, not even once", "nahi, once bhi nahi", "na, this time nahi", "absolutely not", "rather not"]


@pytest.mark.parametrize("said", REFUSALS)
def test_a_refusal_never_grants_whatever_option_word_it_contains(said):
    from confirm import standing_answer
    assert grant_answer(said) not in ("once", "always") and standing_answer(said) not in ("once", "always")


@pytest.mark.parametrize("said", ["pass on it this time", "eh, once I guess", "the other folder, once"])
def test_an_unknown_phrasing_re_asks_rather_than_grants(said):
    assert grant_answer(said) == "unclear"


@pytest.mark.parametrize("said,want", [("sure, for now", "once"), ("yes for today only", "once"), ("yes", "always"),
                                       ("always", "always"), ("just this once", "once")])
def test_a_one_off_yes_to_a_standing_question_saves_nothing(said, want):
    from confirm import standing_answer
    assert standing_answer(said) == want


HOME, APP, DATA = Path("/home/u"), Path("/home/u/voice-instinct"), Path("/home/u/.voice-instinct")
TRIP = HOME / "Documents/trip"


def _perm(access):
    return sandbox_settings(HOME, APP, DATA, grant=(TRIP, access))


def test_read_access_lets_file_tools_read_but_nothing_write():
    s = _perm("read")
    assert s["permissions"]["additionalDirectories"] == [str(TRIP)]
    assert "Edit(~/Documents/trip/**)" in s["permissions"]["deny"]  # Edit rules cover Write too (docs)
    assert "~/Documents/trip" in s["sandbox"]["filesystem"]["denyWrite"]
    assert "allowWrite" not in s["sandbox"]["filesystem"] and s["permissions"]["allow"] == []


def test_write_access_lets_it_change_files_but_never_delete_or_move_them():
    s = _perm("write")
    assert "Edit(~/Documents/trip/**)" in s["permissions"]["allow"]
    assert s["sandbox"]["filesystem"]["allowWrite"] == ["~/Documents/trip"]
    for rule in ("Bash(rm *)", "Bash(rmdir *)", "Bash(unlink *)", "Bash(shred *)", "Bash(mv *)",
                 "Bash(git clean *)", "Bash(find * -delete*)"):
        assert rule in s["permissions"]["deny"], rule


@pytest.mark.parametrize("access", ["write", "delete"])
def test_a_granted_folder_s_git_hooks_and_envrc_are_never_writable(access):
    s = _perm(access)
    assert "Edit(~/**/.git/hooks/**)" in s["permissions"]["deny"] and "Edit(~/**/.envrc)" in s["permissions"]["deny"]
    deny_write = s["sandbox"]["filesystem"]["denyWrite"]  # the shell too: plain paths at the folder's top
    assert "~/Documents/trip/.git/hooks" in deny_write and "~/Documents/trip/.envrc" in deny_write


def test_delete_access_asks_before_every_delete():
    s = _perm("delete")
    assert set(DELETE_COMMANDS) <= set(s["permissions"]["ask"])
    assert not set(DELETE_COMMANDS) & set(s["permissions"]["deny"])


@pytest.mark.parametrize("access", ["read", "write", "delete"])
def test_every_shell_command_naming_the_folder_asks(access):
    ask = _perm(access)["permissions"]["ask"]
    for form in ("/home/u/Documents/trip", "~/Documents/trip", "$HOME/Documents/trip"):
        assert f"Bash(*{form}*)" in ask, form


def test_no_grant_changes_nothing():
    s = sandbox_settings(HOME, APP, DATA)
    assert "ask" not in s["permissions"] and "additionalDirectories" not in s["permissions"]


def test_errands_and_the_nightly_run_can_never_touch_the_grants_file():
    for s in (sandbox_settings(HOME, APP, DATA), consolidation_settings(HOME, APP, DATA)):
        rules = s["permissions"]["deny"]
        assert "Read(~/.voice-instinct/folders.json)" in rules and "Edit(~/.voice-instinct/folders.json)" in rules
        assert "~/.voice-instinct/folders.json" in s["sandbox"]["filesystem"]["denyRead"]
