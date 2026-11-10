"""Per-run autonomy settings: a sandboxed shell that runs without prompts, secrets walled off.

Claude Code's own sandbox (docs: Sandboxing) confines Bash to the task folder and blocks the
network except approved domains. File tools, WebFetch and MCP run outside it, so the same secret
locations are also denied through permission rules. Deny rules hold in every permission mode.
"""

from pathlib import Path

_SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".config/gh", ".config/gcloud", ".kube", ".docker",
                ".mozilla", ".config/chromium", ".config/google-chrome", ".password-store",
                ".local/share/keyrings",  # logged-in browsers and keyrings
                ".config/BraveSoftware", ".config/vivaldi", ".config/microsoft-edge", ".config/opera",
                ".thunderbird", ".config/rclone", ".local/share/TelegramDesktop", ".var/app",  # flatpak apps' data
                ".config/Signal", ".config/discord", ".config/Slack",  # signed-in mail, chat and cloud drives
                ".claude")  # Claude's login and the user's own session transcripts (the CLI reads them itself)
_SECRET_FILES = (".netrc", ".git-credentials", ".npmrc", ".pypirc",
                 ".zsh_history", ".bash_history", ".python_history", ".local/share/fish/fish_history",
                 ".claude.json")  # other MCP servers' secrets
# Other projects' secrets, at any depth under home (gitignore-style ** in rules; on Linux the sandbox honours
# a wildcard after the first directory name, which ~/ always has).
_SECRET_GLOBS = ("**/.env", "**/.env.*")
# Code and settings that run outside the sandbox later (login, a shell, autostart, the next Claude session):
# readable, never writable. Plus, under data_dir, the programs install.sh put there.
_RUNS_LATER_DIRS = (".config/autostart", ".config/systemd", ".local/bin")
_RUNS_LATER_FILES = (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile",
                     ".gitconfig", ".config/git/config")  # git config: core.pager, aliases, hooksPath run code later
_DATA_CODE = ("node", "browser", "models", "qwen-asr")
# Any Claude settings or instructions under home, at any depth (Claude Code loads them; see consolidation_settings).
# Edit rules only: Linux's sandbox ignores these wildcards in denyWrite, but the sandbox protects .claude
# settings in the working directory itself.
# Git hooks and direnv's .envrc run code later too (the next git command, the next cd), granted folders included.
_RUNS_LATER_GLOBS = ("**/.claude/**", "**/CLAUDE*.md", "**/.git/hooks/**", "**/.envrc")
# Pages the errand browser may never request: this app's own server and anything else local. Playwright MCP
# 0.0.83's format (originOrHostGlob in playwright-core): "http://host:*" covers every explicit port but not
# the default one, so that is listed too; "http://[::1]:*" is not understood, the bare-host forms are (any
# scheme). Not a boundary on its own (its docs: redirects pass); guard.py is.
BLOCKED_ORIGINS = ";".join(("http://localhost:*", "http://localhost", "http://127.0.0.1:*", "http://127.0.0.1",
                            "[::1]:*", "[::1]"))
# The errand browser's tools that run code outside the sandbox or read local files into a page, and
# browser_evaluate: page JS could click "Pay" without the purchase guard (hooks.PRE_TOOL_MATCHER) seeing it.
_BROWSER_DENY = ("mcp__browser__browser_run_code_unsafe", "mcp__browser__browser_file_upload",
                 "mcp__browser__browser_evaluate")


def _fs(p: Path, home: Path) -> str:
    """Sandbox path: '~/x' under home, absolute otherwise."""
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return p.as_posix()


def _rule(p: Path, home: Path) -> str:
    """Permission-rule path: '~/x' under home; '//abs' for absolute paths (rule syntax)."""
    try:
        return "~/" + p.relative_to(home).as_posix()
    except ValueError:
        return "/" + p.as_posix()


# Shell commands that delete or move files away. Denied under a "write" grant, asked one by one under "delete".
# Claude Code checks every part of a compound command (&&, ;, |, $(...)) against them; another program doing
# the deleting (python -c, a script) is not caught: the docs say these rules are not a boundary around a program.
DELETE_COMMANDS = ("Bash(rm *)", "Bash(rmdir *)", "Bash(unlink *)", "Bash(shred *)", "Bash(mv *)",
                   "Bash(git clean *)", "Bash(find * -delete*)", "Bash(find *-delete*)")


def _grant_rules(folder: Path, access: str, home: Path) -> dict:
    """What a folder grant (folders.py) lets this run do, as settings. read: file tools may read it, nothing may
    write it. write: file tools may also change it and the shell may write there, but deletes are denied.
    delete: as write, with each delete asked. Every shell command that names the folder asks the user first."""
    rule, fs = _rule(folder, home), _fs(folder, home)
    names = {folder.as_posix(), fs}
    if fs.startswith("~/"):
        names.add("$HOME/" + fs[2:])
    out = {"additionalDirectories": [folder.as_posix()], "ask": [f"Bash(*{n}*)" for n in sorted(names)],
           "allow": [], "deny": [], "allowWrite": [], "denyWrite": []}
    if access == "read":
        out["deny"].append(f"Edit({rule}/**)")
        out["denyWrite"].append(fs)
    else:
        out["allow"].append(f"Edit({rule}/**)")
        out["allowWrite"].append(fs)
        # The shell writes there too, and the sandbox ignores the _RUNS_LATER_GLOBS: the folder's own ones.
        out["denyWrite"] += [f"{fs}/.git/hooks", f"{fs}/.envrc"]
        out["deny" if access == "write" else "ask"] += DELETE_COMMANDS
    return out


def sandbox_settings(home, app_dir, data_dir, hook_port=7860, errand=True, grant=None, cwd=None) -> dict:
    """grant: (folder, "read" | "write" | "delete") the user said yes to for this errand, or None. cwd: the run's
    folder, whose .playwright-mcp snapshots the purchase guard reads (hooks.py), so the agent may not change them;
    Playwright MCP writes them itself from outside the sandbox (an MCP server, not a Bash command)."""
    home, app_dir, data_dir = Path(home), Path(app_dir), Path(data_dir)
    # Memory (memory.py): errands grep it but never change it, and never read the raw journal of conversations.
    dirs = [home / d for d in _SECRET_DIRS] + [data_dir / "runs"] + ([data_dir / "memory/journal"] if errand else [])
    files = [home / f for f in _SECRET_FILES] + [
        app_dir / ".env", data_dir / "hook-token", data_dir / "harness.db", data_dir / "folders.json",
        data_dir / "harness.db-wal", data_dir / "harness.db-shm"]  # WAL mode keeps recent writes here
    deny = [f"Edit({_rule(data_dir / 'memory', home)}/**)"] if errand else []
    for p in dirs:
        deny += [f"Read({_rule(p, home)}/**)", f"Edit({_rule(p, home)}/**)"]
    for p in files:
        deny += [f"Read({_rule(p, home)})", f"Edit({_rule(p, home)})"]
    deny += [f"{t}(~/{g})" for g in _SECRET_GLOBS for t in ("Read", "Edit")]
    # Not writable: code that runs outside the sandbox later.
    no_write_dirs = [app_dir] + [data_dir / d for d in _DATA_CODE] + [home / d for d in _RUNS_LATER_DIRS] + (
        [Path(cwd) / ".playwright-mcp"] if cwd and errand else [])
    no_write_files = [home / f for f in _RUNS_LATER_FILES]
    deny += [f"Edit({_rule(p, home)}/**)" for p in no_write_dirs] + [f"Edit({_rule(p, home)})" for p in no_write_files]
    deny += [f"Edit(~/{g})" for g in _RUNS_LATER_GLOBS]
    gr = _grant_rules(Path(grant[0]), grant[1], home) if grant else {}
    s = {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,  # never fall back to running unsandboxed
            "autoAllowBashIfSandboxed": True,  # sandboxed shell commands need no prompt
            "allowUnsandboxedCommands": False,  # and cannot be retried outside the sandbox
            "filesystem": {"denyRead": [_fs(p, home) for p in dirs + files] + [f"~/{g}" for g in _SECRET_GLOBS],
                           # Edit denies are merged in too; listed for Bash all the same (plain paths: Linux)
                           "denyWrite": [_fs(p, home) for p in no_write_dirs + no_write_files]},
            # The sandbox proxy refuses loopback unless listed, so without this every hook POST to this
            # app got HTTP 403 and permission questions never reached the user.
            # strictAllowlist: in auto mode a command may otherwise name more hosts for the classifier.
            "network": {"allowedDomains": [f"127.0.0.1:{hook_port}"], "strictAllowlist": True},
        },
        "permissions": {"deny": deny + list(_BROWSER_DENY)},
        # Output past the inline limit is saved under ~/.claude, which the agent cannot read: keep it
        # inline up to the maximum. ponytail: past 128k chars it is still unreadable; give the agent a
        # readable overflow dir if real errands hit that.
        "bashOutputMaxChars": 128000,
        # The user's own instructions are for their own sessions, not for errands.
        "claudeMdExcludes": [str(home / "CLAUDE.md"), str(home / "CLAUDE.local.md")],
    }
    if gr:
        s["permissions"]["deny"] += gr["deny"]
        s["permissions"].update(allow=gr["allow"], ask=gr["ask"], additionalDirectories=gr["additionalDirectories"])
        fs = s["sandbox"]["filesystem"]
        fs["denyWrite"] += gr["denyWrite"]
        if gr["allowWrite"]:
            fs["allowWrite"] = gr["allowWrite"]
    return s


def consolidation_settings(home, app_dir, data_dir) -> dict:
    """The nightly memory run (consolidate.py): it writes memory, journal included, but the journal holds web
    text from errands, so it gets no network, no web tools and the same secret denials as errands."""
    s = sandbox_settings(home, app_dir, data_dir, errand=False)
    s["sandbox"]["network"] = {"allowedDomains": [], "strictAllowlist": True}
    # Nothing it writes may run or steer code later (git hooks, its next run's settings and instructions, at any
    # depth: Claude Code loads people/CLAUDE.md when it reads people/), and .gitignore keeps the raw journal out
    # of git history. The files our code owns are denied too. Edit denies cover Bash (merged into denyWrite).
    memory = _rule(Path(data_dir) / "memory", Path(home))
    # No shell: its prompt needs only Read/Edit/Write and git is done in Python, while a shell command could
    # delete memory files (or .git) around the Edit denies.
    s["permissions"]["deny"] += ["Bash", "WebFetch", "WebSearch", *(f"Edit({memory}/{p})" for p in (
        "**/.git/**", "**/.claude/**", "**/CLAUDE.md", "**/CLAUDE.local.md", ".gitignore", "notes.md",
        ".consolidated", "journal/**"))]
    return s


def browser_mcp(home, cdp_endpoint=None):
    """The errand browser for --mcp-config, or None until install.sh has set it up.

    Playwright MCP on the installer's own Node and its own Chromium, headless, with a fresh profile
    each run (--isolated): never signed in to the user's accounts. Page snapshots it saves stay in
    the errand's own folder. In debug mode (steps.py) it attaches to a browser we launched instead,
    so every step can be screenshotted.
    """
    vi = Path(home) / ".voice-instinct"
    node, cli = vi / "node/bin/node", vi / "browser/node_modules/@playwright/mcp/cli.js"
    browsers = vi / "browser/browsers"
    if not (node.exists() and cli.exists() and browsers.is_dir()):
        return None
    args = (["--cdp-endpoint", cdp_endpoint] if cdp_endpoint
            else ["--browser", "chromium", "--headless", "--isolated"]) + ["--blocked-origins", BLOCKED_ORIGINS]
    return {"mcpServers": {"browser": {"type": "stdio", "command": str(node), "args": [str(cli), *args],
                                       "env": {"PLAYWRIGHT_BROWSERS_PATH": str(browsers)}}}}


def with_debug_record(settings, cwd, home):
    """Debug runs: the step record in <cwd>/debug is the user's, not the agent's to change. Claude Code
    merges Edit deny rules into the sandbox's denyWrite, so this holds for Bash too."""
    rule = f"Edit({_rule(Path(cwd) / 'debug', Path(home))}/**)"
    return {**settings, "permissions": {**settings["permissions"],
                                        "deny": [*settings["permissions"]["deny"], rule]}}
