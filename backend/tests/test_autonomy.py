import json
from pathlib import Path

from autonomy import browser_mcp, sandbox_settings, with_debug_record
from hooks import write_run_settings

HOME, APP, DATA = Path("/home/u"), Path("/home/u/voice-agent"), Path("/home/u/.voice-instinct")


def test_sandbox_is_on_fails_closed_and_cannot_be_escaped_by_retry():
    sb = sandbox_settings(HOME, APP, DATA)["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True  # no silent unsandboxed fallback
    assert sb["autoAllowBashIfSandboxed"] is True  # the autonomy: sandboxed shell runs without prompts
    assert sb["allowUnsandboxedCommands"] is False  # the agent cannot retry a failed command outside the sandbox


def test_secret_locations_are_unreadable_by_both_the_sandbox_and_the_file_tools():
    s = sandbox_settings(HOME, APP, DATA)
    deny_read, rules = s["sandbox"]["filesystem"]["denyRead"], s["permissions"]["deny"]
    for p in ("~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh", "~/.netrc", "~/voice-agent/.env",
              "~/.voice-instinct/hook-token", "~/.voice-instinct/harness.db", "~/.voice-instinct/runs",
              "~/.voice-instinct/harness.db-wal", "~/.voice-instinct/harness.db-shm"):  # WAL mode
        assert p in deny_read, p
    assert "Read(~/.ssh/**)" in rules and "Edit(~/.ssh/**)" in rules
    assert "Read(~/voice-agent/.env)" in rules and "Edit(~/.voice-instinct/hook-token)" in rules


def test_paths_outside_home_use_the_documented_double_slash_for_rules():
    s = sandbox_settings(Path("/home/u"), Path("/opt/app"), Path("/var/vi"))
    assert "/opt/app/.env" in s["sandbox"]["filesystem"]["denyRead"]
    assert "Read(//opt/app/.env)" in s["permissions"]["deny"]


def test_the_workspace_is_not_blocked_even_though_it_lives_under_the_data_dir():
    # ~/.voice-instinct/workspace must stay readable and writable: only the named secrets are denied.
    s = sandbox_settings(HOME, APP, DATA)
    assert "~/.voice-instinct" not in s["sandbox"]["filesystem"]["denyRead"]
    assert not any(r.startswith(("Read(~/.voice-instinct/**", "Edit(~/.voice-instinct/**")) for r in s["permissions"]["deny"])


def test_run_settings_carry_the_sandbox_alongside_the_hooks_and_stay_private(tmp_path):
    import os
    import stat

    path = write_run_settings(tmp_path, "s", "tok", 7860, extra=sandbox_settings(HOME, APP, DATA))
    cfg = json.loads(open(path).read())
    assert "PermissionRequest" in cfg["hooks"] and cfg["sandbox"]["enabled"] is True
    assert cfg["permissions"]["deny"] and stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_sandbox_lets_hooks_reach_this_app_and_nothing_else_local():
    # Claude Code's sandbox proxy refuses loopback unless it is listed: hooks got HTTP 403 without this.
    net = sandbox_settings(HOME, APP, DATA, hook_port=7860)["sandbox"]["network"]
    assert net["allowedDomains"] == ["127.0.0.1:7860"]


def test_errand_browser_is_private_and_only_offered_once_installed(tmp_path):
    from autonomy import browser_mcp

    assert browser_mcp(tmp_path) is None  # not installed: the agent gets no browser at all
    vi = tmp_path / ".voice-instinct"
    (vi / "node/bin").mkdir(parents=True)
    (vi / "node/bin/node").write_text("")
    cli = vi / "browser/node_modules/@playwright/mcp/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    (vi / "browser/browsers").mkdir()
    server = browser_mcp(tmp_path)["mcpServers"]["browser"]
    assert server["command"] == str(vi / "node/bin/node") and server["args"][0] == str(cli)
    # A fresh profile each run: never signed in to anything (page snapshots stay in the task folder).
    assert "--isolated" in server["args"] and "--headless" in server["args"]
    assert "--user-data-dir" not in server["args"]


def test_the_browsers_code_running_tools_are_denied_in_every_mode():
    # browser_run_code_unsafe runs model-written JS in the MCP server process, outside the sandbox:
    # an agent reading untrusted pages must never get it. Deny rules hold in every permission mode.
    deny = sandbox_settings(HOME, APP, DATA)["permissions"]["deny"]
    assert "mcp__browser__browser_run_code_unsafe" in deny and "mcp__browser__browser_file_upload" in deny
    # browser_evaluate runs model-written JS in the page: it could click "Pay" past the purchase guard (hooks.py).
    assert "mcp__browser__browser_evaluate" in deny
    # browser_drop with paths reads local files into the page from outside the sandbox, like file_upload.
    assert "mcp__browser__browser_drop" in deny


def test_the_shell_cannot_reach_any_host_beyond_the_hook_port():
    # In auto mode a command can name extra hosts for the classifier to approve; strict means it cannot.
    assert sandbox_settings(HOME, APP, DATA)["sandbox"]["network"]["strictAllowlist"] is True


def test_claudes_own_login_browser_profiles_and_keyrings_are_off_limits():
    s = sandbox_settings(HOME, APP, DATA)
    deny_read, rules = s["sandbox"]["filesystem"]["denyRead"], s["permissions"]["deny"]
    for p in ("~/.claude", "~/.claude.json", "~/.mozilla", "~/.config/chromium",
              "~/.config/google-chrome", "~/.password-store", "~/.local/share/keyrings"):
        assert p in deny_read, p
    assert "Read(~/.claude/**)" in rules and "Read(~/.mozilla/**)" in rules


def test_the_users_own_claude_md_is_not_loaded_into_errands():
    assert s_excludes(sandbox_settings(HOME, APP, DATA)) >= {str(HOME / "CLAUDE.md"), str(HOME / "CLAUDE.local.md")}


def s_excludes(s):
    return set(s["claudeMdExcludes"])


def test_errand_browser_is_offered_only_with_its_own_chromium(tmp_path):
    from autonomy import browser_mcp

    vi = tmp_path / ".voice-instinct"
    for f in ("node/bin/node", "browser/node_modules/@playwright/mcp/cli.js"):
        (vi / f).parent.mkdir(parents=True, exist_ok=True)
        (vi / f).write_text("")
    assert browser_mcp(tmp_path) is None  # Chromium missing: no browser rather than one that always fails
    (vi / "browser/browsers").mkdir()
    server = browser_mcp(tmp_path)["mcpServers"]["browser"]
    assert server["env"] == {"PLAYWRIGHT_BROWSERS_PATH": str(vi / "browser/browsers")}  # self-contained


def test_large_shell_output_stays_inline_because_its_overflow_file_is_unreadable():
    # Claude Code saves output past the inline limit under ~/.claude, which the agent cannot read.
    assert sandbox_settings(HOME, APP, DATA)["bashOutputMaxChars"] == 128000


def _install_browser(home):
    vi = home / ".voice-instinct"
    for f in ("node/bin/node", "browser/node_modules/@playwright/mcp/cli.js"):
        (vi / f).parent.mkdir(parents=True, exist_ok=True)
        (vi / f).touch()
    (vi / "browser/browsers").mkdir(parents=True, exist_ok=True)
    return vi / "browser/node_modules/@playwright/mcp/cli.js"


def test_browser_mcp_attaches_to_a_given_cdp_endpoint_instead_of_launching(tmp_path):
    cli = _install_browser(tmp_path)
    assert browser_mcp(tmp_path)["mcpServers"]["browser"]["args"][:5] == [
        str(cli), "--browser", "chromium", "--headless", "--isolated"]
    assert browser_mcp(tmp_path, cdp_endpoint="http://127.0.0.1:9")["mcpServers"]["browser"]["args"][:3] == [
        str(cli), "--cdp-endpoint", "http://127.0.0.1:9"]


def test_debug_runs_deny_edits_to_the_record_without_changing_the_shared_settings():
    base = sandbox_settings(HOME, APP, DATA)
    debug = with_debug_record(base, HOME / "trip", HOME)
    assert "Edit(~/trip/debug/**)" in debug["permissions"]["deny"]
    assert "Edit(~/trip/debug/**)" not in base["permissions"]["deny"]
    assert debug["sandbox"] == base["sandbox"]


def test_errands_may_read_memory_but_never_change_it_nor_read_the_raw_journal():
    s = sandbox_settings(HOME, APP, DATA)
    rules, deny_read = s["permissions"]["deny"], s["sandbox"]["filesystem"]["denyRead"]
    assert "Edit(~/.voice-instinct/memory/**)" in rules  # merged into the sandbox's denyWrite: Bash too
    assert "Read(~/.voice-instinct/memory/**)" not in rules  # grep finds facts there
    assert "Read(~/.voice-instinct/memory/journal/**)" in rules and "~/.voice-instinct/memory/journal" in deny_read


def test_the_nightly_run_writes_memory_but_has_no_network_or_web_and_no_secrets():
    from autonomy import consolidation_settings
    s = consolidation_settings(HOME, APP, DATA)
    rules = s["permissions"]["deny"]
    assert "Edit(~/.voice-instinct/memory/**)" not in rules  # its own folder, journal included
    assert not any(r.startswith("Read(") and "memory/journal" in r for r in rules)  # it reads the journal
    assert "Read(~/.ssh/**)" in rules and "Read(~/.voice-instinct/harness.db)" in rules
    assert "WebFetch" in rules and "WebSearch" in rules
    assert s["sandbox"]["network"] == {"allowedDomains": [], "strictAllowlist": True}
    assert s["sandbox"]["enabled"] and s["sandbox"]["failIfUnavailable"]
    assert "~/.voice-instinct/memory/journal" not in s["sandbox"]["filesystem"]["denyRead"]


def test_the_nightly_run_cannot_plant_code_that_runs_outside_its_sandbox():
    # Its input holds web text: git hooks, its own next run's settings or a CLAUDE.md would all run or steer
    # code later. .gitignore keeps the raw journal out of git history.
    from autonomy import consolidation_settings
    rules = consolidation_settings(HOME, APP, DATA)["permissions"]["deny"]
    # Nested too (review): Claude Code loads people/CLAUDE.md when it reads people/. The files it must leave
    # alone (notes.md, .consolidated, the journal) are denied as well, not only asked of it.
    for p in ("**/.git/**", "**/.claude/**", "**/CLAUDE.md", "**/CLAUDE.local.md", ".gitignore", "notes.md",
              ".consolidated", "journal/**"):
        assert f"Edit(~/.voice-instinct/memory/{p})" in rules, p


def test_the_errand_browser_may_not_request_this_app_or_any_local_page(tmp_path):
    # A page the agent visits must not reach the voice server; guard.py refuses it, this stops it earlier.
    _install_browser(tmp_path)
    for cdp in (None, "http://127.0.0.1:9"):  # debug runs too
        args = browser_mcp(tmp_path, cdp_endpoint=cdp)["mcpServers"]["browser"]["args"]
        blocked = args[args.index("--blocked-origins") + 1].split(";")
        assert {"http://localhost:*", "http://localhost", "http://127.0.0.1:*", "http://127.0.0.1",
                "[::1]:*", "[::1]"} <= set(blocked)


def test_more_signed_in_apps_and_shell_histories_are_off_limits():
    s = sandbox_settings(HOME, APP, DATA)
    deny_read, rules = s["sandbox"]["filesystem"]["denyRead"], s["permissions"]["deny"]
    for d in (".config/BraveSoftware", ".config/vivaldi", ".config/microsoft-edge", ".config/opera", ".thunderbird",
              ".config/rclone", ".local/share/TelegramDesktop", ".var/app", ".config/Signal", ".config/discord",
              ".config/Slack"):
        assert f"~/{d}" in deny_read and f"Read(~/{d}/**)" in rules and f"Edit(~/{d}/**)" in rules, d
    for f in (".zsh_history", ".bash_history", ".python_history", ".local/share/fish/fish_history"):
        assert f"~/{f}" in deny_read and f"Read(~/{f})" in rules, f


def test_other_projects_env_files_are_unreadable_at_any_depth():
    # Rule paths are gitignore-style (docs: Permissions > Read and Edit): ~/**/.env covers every project.
    s = sandbox_settings(HOME, APP, DATA)
    assert {"Read(~/**/.env)", "Read(~/**/.env.*)", "Edit(~/**/.env)"} <= set(s["permissions"]["deny"])
    assert "~/**/.env" in s["sandbox"]["filesystem"]["denyRead"]


def test_code_that_runs_outside_the_sandbox_later_cannot_be_changed():
    # This app's own code, the installer's programs, autostart and systemd units, ~/.local/bin, shell
    # startup files, and any Claude settings or instructions: changing one would run code unsandboxed later.
    s = sandbox_settings(HOME, APP, DATA)
    rules, deny_write = s["permissions"]["deny"], s["sandbox"]["filesystem"]["denyWrite"]
    for d in ("~/voice-agent", "~/.voice-instinct/node", "~/.voice-instinct/browser", "~/.voice-instinct/models",
              "~/.voice-instinct/qwen-asr", "~/.config/autostart", "~/.config/systemd", "~/.local/bin"):
        assert f"Edit({d}/**)" in rules and d in deny_write, d
        assert f"Read({d}/**)" not in rules  # still readable
    for f in ("~/.bashrc", "~/.zshrc", "~/.profile", "~/.bash_profile", "~/.zprofile", "~/.gitconfig", "~/.config/git/config"):
        assert f"Edit({f})" in rules and f in deny_write, f
    assert "Edit(~/**/.claude/**)" in rules and "Edit(~/**/CLAUDE*.md)" in rules
    # Git hooks and direnv files run code later, outside the sandbox: never writable, granted folder or not.
    assert "Edit(~/**/.git/hooks/**)" in rules and "Edit(~/**/.envrc)" in rules
    # Linux's sandbox ignores wildcards in denyWrite other than a trailing /**: only plain paths go there.
    assert not any("*" in p for p in deny_write)


def test_the_repo_outside_home_is_protected_with_an_absolute_rule():
    s = sandbox_settings(Path("/home/u"), Path("/opt/app"), Path("/var/vi"))
    assert "Edit(//opt/app/**)" in s["permissions"]["deny"] and "/opt/app" in s["sandbox"]["filesystem"]["denyWrite"]


def test_an_errand_cannot_rewrite_the_browsers_snapshot_files():
    # The purchase guard reads <cwd>/.playwright-mcp/page-*.yml (hooks.py): a hijacked agent must not rename
    # "Pay" to "Continue" there before a click. Playwright MCP writes them from outside the sandbox.
    cwd = HOME / ".voice-instinct/workspace/trip-abc123"
    s = sandbox_settings(HOME, APP, DATA, cwd=cwd)
    assert "Edit(~/.voice-instinct/workspace/trip-abc123/.playwright-mcp/**)" in s["permissions"]["deny"]
    assert "~/.voice-instinct/workspace/trip-abc123/.playwright-mcp" in s["sandbox"]["filesystem"]["denyWrite"]
    assert not any(".playwright-mcp" in r for r in sandbox_settings(HOME, APP, DATA)["permissions"]["deny"])
    # A folder delegate could not make it in (an old errand's, of the user's): the Edit deny only, since the
    # sandbox needs a real path to deny writes to.
    s = sandbox_settings(HOME, APP, DATA, cwd=cwd, snapshot_dir_made=False)
    assert "Edit(~/.voice-instinct/workspace/trip-abc123/.playwright-mcp/**)" in s["permissions"]["deny"]
    assert not any(".playwright-mcp" in p for p in s["sandbox"]["filesystem"]["denyWrite"])


def test_the_nightly_run_has_no_shell():
    # Its Edit denies hold for its own edits, but a shell could still delete memory files or .git (a failed run
    # that "deleted a file via the shell" would then be beyond the rollback). It needs none: git is consolidate.py's.
    from autonomy import consolidation_settings
    from consolidate import CONSOLIDATE_PROMPT
    s = consolidation_settings(HOME, APP, DATA)
    assert "Bash" in s["permissions"]["deny"]
    assert "Bash" not in s["permissions"].get("allow", [])
    assert "shell" not in CONSOLIDATE_PROMPT.lower() and "bash" not in CONSOLIDATE_PROMPT.lower()


def test_the_nightly_run_gets_the_same_protections():
    from autonomy import consolidation_settings
    rules = consolidation_settings(HOME, APP, DATA)["permissions"]["deny"]
    assert "Edit(~/voice-agent/**)" in rules and "Read(~/**/.env)" in rules and "Edit(~/.bashrc)" in rules


GATEWAYS = ("api.razorpay.com", "checkout.razorpay.com", "secure.payu.in", "www.payumoney.com", "securegw.paytm.in",
            "secure.ccavenue.com", "pgi.billdesk.com", "api.juspay.in", "sdk.cashfree.com", "mercury.phonepe.com",
            "pay.easebuzz.in", "www.instamojo.com", "checkout.stripe.com", "js.stripe.com", "api.stripe.com",
            "www.paypal.com", "checkoutshopper-live.adyen.com", "api.braintreegateway.com", "api.checkout.com",
            "secure.worldpay.com", "pay.google.com", "payments.amazon.in", "pay.amazon.com")


def _glob_matches(pattern, url):
    """Playwright's originOrHostGlob + globToRegexPattern (coreBundle.js), enough for host patterns."""
    import re
    m = re.match(r"^(https?://[^/:]+):\*$", pattern)
    glob = f"{m.group(1)}:*/**" if m else (pattern + "/**" if "://" in pattern else f"*://{pattern}/**")
    rx = "^" + "".join("(.*)" if t == "**" else "([^/]*)" if t == "*" else re.escape(t)
                       for t in re.findall(r"\*\*|\*|[^*]+", glob)) + "$"
    return re.match(rx, url) is not None


def test_the_errand_browser_can_never_reach_a_payment_gateway(tmp_path):
    # Nova never pays: the errand browser blocks the gateways' origins (Playwright MCP's own glob format).
    from autonomy import BLOCKED_ORIGINS, PAYMENT_HOSTS
    _install_browser(tmp_path)
    for server in (browser_mcp(tmp_path), browser_mcp(tmp_path, cdp_endpoint="http://127.0.0.1:9")):
        args = server["mcpServers"]["browser"]["args"]
        blocked = args[args.index("--blocked-origins") + 1].split(";")
        assert blocked == BLOCKED_ORIGINS.split(";") and "--no-webmcp" in args
        for host in GATEWAYS:
            assert any(_glob_matches(b, f"https://{host}/v1/pay") for b in blocked), host
        for host in ("www.amazon.in", "www.flipkart.com", "www.google.com", "maps.google.com", "razorpay.com.evil.in"):
            assert not any(_glob_matches(b, f"https://{host}/") for b in blocked), host
    assert all(h in BLOCKED_ORIGINS for h in PAYMENT_HOSTS)


def test_a_gateway_is_blocked_even_behind_a_redirect(tmp_path):
    # --blocked-origins does not see redirects (Playwright MCP's own docs; confirmed with 0.0.83): the browser's
    # resolver refuses the gateways' hosts too, through a config file the agent can't write (browser/ is denied).
    import json as _json
    from autonomy import resolver_rules
    _install_browser(tmp_path)
    args = browser_mcp(tmp_path)["mcpServers"]["browser"]["args"]
    cfg = Path(args[args.index("--config") + 1])
    assert cfg.parent == tmp_path / ".voice-instinct/browser"
    assert _json.loads(cfg.read_text())["browser"]["launchOptions"]["args"] == [f"--host-resolver-rules={resolver_rules()}"]
    rules = resolver_rules()
    assert "MAP *.razorpay.com ~NOTFOUND" in rules and "MAP razorpay.com ~NOTFOUND" in rules
    assert "MAP pay.google.com ~NOTFOUND" in rules and "MAP *.google.com" not in rules
