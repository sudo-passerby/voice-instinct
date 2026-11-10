"""Where a delegated task works: its own private folder under the workspace root. Any other folder the user
names is a grant (folders.py), read by path; the shell always starts here."""

import re
from pathlib import Path


def _slug(task: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", task.lower())[:4]) or "task"


def private_workdir(task, session_id, workspace_root) -> Path:
    """Return (and create) the errand's own folder."""
    p = (Path(workspace_root) / f"{_slug(task)}-{session_id[:6]}").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p
