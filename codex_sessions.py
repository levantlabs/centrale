"""Codex's verified conversation lookup (task-195), in one stdlib-only file
so the same code answers on the hub and on a worker (task-207).

On the hub, spawn.codex_conversation_id imports conversation_id() from
here. For a project on a worker, remote.py sends this file's source to
the worker as `python3 - <worktree>` -- nothing is installed there -- and
the __main__ block prints the Codex home it read and the UUID it found,
so the resumed agent is bound to exactly that store.

Keep it importable by a bare python3 with nothing beside it: no imports
from this repo.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


def codex_home():
    """CODEX_HOME, else ~/.codex, absolute -- the store the CLI itself uses."""
    return os.path.abspath(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))


def conversation_id(home, wt_dir):
    """Latest interactive UUID belonging to this exact worktree (task-195).

    Read Codex's own records on each resume; never keep a task/session map.
    Metadata identifies the original cwd; turn contexts also exclude
    conversations moved elsewhere. Subagents share cwd but are not the
    task's interactive conversation. Unreadable, malformed or incomplete
    records cannot establish identity.
    """
    home = Path(home)
    target = os.path.realpath(wt_dir)
    candidates = []
    for folder, _, names in os.walk(home / "sessions"):
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            path = Path(folder) / name
            try:
                with path.open(encoding="utf-8") as stream:
                    meta = json.loads(stream.readline())
                payload = meta.get("payload", {})
                if (meta.get("type") != "session_meta"
                        or payload.get("source") != "cli"
                        or not isinstance(payload.get("cwd"), str)
                        or os.path.realpath(payload["cwd"]) != target):
                    continue
                identity = str(uuid.UUID(payload["id"]))
                # UUID lookup uses the rollout filename too; reject records
                # whose metadata and filename disagree.
                if not name.endswith(f"-{identity}.jsonl"):
                    continue
                candidates.append((path.stat().st_mtime_ns, identity, path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
    for _, identity, path in sorted(candidates, reverse=True):
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    event = json.loads(line)
                    if event.get("type") in {"session_meta", "turn_context"}:
                        cwd = event.get("payload", {}).get("cwd")
                        if not isinstance(cwd, str) or os.path.realpath(cwd) != target:
                            break
                else:
                    return identity
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


if __name__ == "__main__":
    # Worker side: `python3 - <worktree>`. Two lines: the home, the UUID
    # (empty when none is verified).
    home = codex_home()
    print(home)
    print(conversation_id(home, sys.argv[1]) or "")
