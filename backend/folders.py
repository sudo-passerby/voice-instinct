"""Folders an errand may use beyond its private workspace, only with the user's spoken yes.

A grant is "once" (that errand and its resumes, kept in the registry) or "always" (kept here, in
data_dir/folders.json, read on every errand so hand edits count). Access is "read", "write" (read and
change, never delete) or "delete" (once only; each delete still asks). autonomy.sandbox_settings
enforces a grant in the run's own settings.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from loguru import logger
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.services.llm_service import FunctionCallParams

LEVELS = {"read": 1, "write": 2, "delete": 3}  # each includes the ones below
STORED = ("read", "write")  # delete is never "always"
NOT_GRANTABLE = "That folder holds settings or my own files, so I can't use it, whatever you say."


def resolve_folder(directory, home) -> Path:
    """The folder the user named ('~/x', 'x' under home, or absolute), symlinks and '..' resolved."""
    home, d = Path(home).resolve(), directory.strip()
    if d == "~" or d.startswith("~/"):
        p = home / d[2:]
    else:
        p = Path(d)
        if not p.is_absolute():
            p = home / p
    return p.resolve()


def refusal(p: Path, home, data_dir, repo_root, workspace_root):
    """Why this folder can never be granted, or None. Checked on the resolved path, so a symlink into a
    refused place is refused too."""
    home, data, repo = Path(home).resolve(), Path(data_dir).expanduser().resolve(), Path(repo_root).resolve()
    ws = Path(workspace_root).expanduser().resolve()
    if not p.is_relative_to(home) or p == home:
        return "I can only use a folder inside your home folder, and not the whole home folder."
    if p.is_relative_to(ws):
        return None  # the private workspace needs no grant
    if any(part.startswith(".") for part in p.relative_to(home).parts):
        return "I can't use hidden folders, they hold settings."  # ~/.config, ~/.local, ~/.ssh, ...
    if p.is_relative_to(home / "bin"):  # on the PATH: a script written there runs outside the sandbox later
        return NOT_GRANTABLE
    for own in (repo, data):  # either way round: a parent of them would expose them too
        if p.is_relative_to(own) or own.is_relative_to(p):
            return NOT_GRANTABLE
    return None


class Folders:
    """The standing ("always") grants in data_dir/folders.json."""

    def __init__(self, path, home, data_dir, repo_root, workspace_root):
        self.path, self._where = Path(path), (home, data_dir, repo_root, workspace_root)

    def refusal(self, p: Path):
        return refusal(p, *self._where)

    def _raw(self) -> list:
        """The file's grants list as written, hand edits and all; [] if it is missing, unreadable or not a list."""
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.warning(f"folders.json unreadable, no standing grants: {type(e).__name__}")
            return []
        grants = data.get("grants", []) if isinstance(data, dict) else None
        if not isinstance(grants, list):
            logger.warning("folders.json: no grants list, no standing grants")
            return []
        return grants

    def _valid(self, g):
        """(grant, None) with its path resolved, or (None, why) for a bad entry. Never raises."""
        if not (isinstance(g, dict) and isinstance(g.get("path"), str) and isinstance(g.get("access"), str)
                and g["access"] in STORED and "\0" not in g["path"]):
            return None, "not a {path, access} entry"
        try:
            p = Path(g["path"]).expanduser().resolve()
        except (OSError, ValueError, RuntimeError) as e:  # RuntimeError: a symlink loop, or ~user unknown
            return None, type(e).__name__
        why = self.refusal(p)
        return (None, why) if why else ({**g, "path": str(p)}, None)

    def load(self) -> list[dict]:
        """Every valid standing grant, read fresh. A bad hand-edited entry is ignored and logged."""
        out = []
        for g in self._raw():
            ok, why = self._valid(g)
            if why:
                logger.warning(f"folders.json: ignoring {g!r}: {why}")
            else:
                out.append(ok)
        return out

    def access(self, folder: Path):
        """The access the most specific standing grant covering this folder gives, or None."""
        hits = [g for g in self.load() if Path(folder).is_relative_to(g["path"])]
        return max(hits, key=lambda g: len(g["path"]))["access"] if hits else None

    def _matching(self, g, test) -> bool:
        ok, _ = self._valid(g)
        return bool(ok) and test(Path(ok["path"]))

    def allow(self, folder: Path, access: str):
        """Save a standing grant, replacing this folder's own; every other entry is written back as it was,
        even one load() skips (the user may be mid-way through a hand edit)."""
        assert access in STORED, access  # delete is never standing
        grants = [g for g in self._raw() if not self._matching(g, lambda p: p == Path(folder))]
        grants.append({"path": str(folder), "access": access, "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        self._write(grants)

    def revoke(self, folder: Path) -> int:
        """Drop the grants for this folder and everything under it; how many went. Other entries stay as written."""
        grants = self._raw()
        keep = [g for g in grants if not self._matching(g, lambda p: p.is_relative_to(folder))]
        if len(keep) != len(grants):
            self._write(keep)
        return len(grants) - len(keep)

    def _write(self, grants):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".folders-")  # mkstemp: already 0600
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"grants": grants}, f, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def covers(granted, wanted) -> bool:
    return granted is not None and LEVELS[granted] >= LEVELS[wanted]


def tilde(p, home) -> str:
    """A folder as the user would say it: ~/Documents/trip."""
    try:
        return "~/" + Path(p).relative_to(Path(home).resolve()).as_posix()
    except ValueError:
        return str(p)


def question(folder, access, home, standing=False) -> str:
    """The consent question, saying exactly what is asked."""
    where = tilde(folder, home)
    if access == "delete":
        return f"Can I delete files in {where} for this errand? I'll ask you before each delete."
    what = (f"read files in {where}" if access == "read" else f"read and change files in {where}")
    promise = "I won't change or delete anything." if access == "read" else "I won't delete anything."
    if standing:
        return f"Can I always {what}, for every errand from now on? {promise}"
    return f"Can I {what}? {promise} Just this once, or always?"


NOVA_SID = "nova-folders"  # questions need a session row; this one is Nova's own (registry hides origin 'nova')


def make_folder_tools(folders, bridge, registry, home):
    home = Path(home)

    @tool_options(cancel_on_interruption=False, timeout_secs=bridge.timeout + 30)
    async def allow_folder(params: FunctionCallParams, path: str, access: str = "read"):
        """Give every errand standing access to a folder, only when the user asks for that in their own words (for example "you can always read my Documents folder"). The user is asked to confirm; nothing is granted until they say yes.

        Args:
            path: The folder, for example ~/Documents.
            access: "read" (read only) or "write" (read and change, never delete). Delete can never be standing.
        """
        if access not in STORED:
            await params.result_callback({"status": "refused", "result": "Standing access can only be read or "
                                          "write; deleting is only ever for one errand, and asked each time."})
            return
        folder = resolve_folder(path, home)
        why = folders.refusal(folder)
        if why:
            await params.result_callback({"status": "refused", "result": why})
            return
        registry.upsert_session(NOVA_SID, origin="nova", mode="nova", state="idle")
        answer = await bridge.ask(NOVA_SID, "folder", question(folder, access, home, standing=True),
                                  "allow_folder", {"folder": str(folder), "access": access})
        if answer == "once":  # "just this once" to a standing question saves nothing (relay: standing_answer)
            await params.result_callback({"status": "not_granted", "result": "You said just this once, so nothing "
                                          "was saved; each errand will ask about that folder."})
            return
        if answer != "always":
            await params.result_callback({"status": "not_granted", "result": "Nothing was saved; errands will ask."})
            return
        folders.allow(folder, access)
        await params.result_callback({"status": "granted", "folder": tilde(folder, home), "access": access})

    @tool_options(cancel_on_interruption=False)
    async def list_folders(params: FunctionCallParams):
        """List the folders errands may always use, and with what access. Everything else is asked per errand."""
        await params.result_callback({"folders": [
            {"folder": tilde(g["path"], home), "access": g["access"], "since": g.get("since")} for g in folders.load()]})

    @tool_options(cancel_on_interruption=False)
    async def revoke_folder(params: FunctionCallParams, path: str):
        """Stop errands using a folder (and its subfolders) without asking. Needs no confirmation: it only takes access away.

        Args:
            path: The folder, for example ~/Documents.
        """
        folder = resolve_folder(path, home)
        n = folders.revoke(folder)
        registry.revoke_grants(folder)  # errands that ran under it keep it otherwise, for their resumes
        still = folders.access(folder)
        await params.result_callback({"status": "revoked" if n else "none_found", "removed": n, "note":
            f"A grant on a parent folder still gives {still} access." if still else "No standing access is left."})

    return [allow_folder, list_folders, revoke_folder]
