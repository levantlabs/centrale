#!/usr/bin/env python3
"""Agent lifecycle event helper for task-37's hook/notify injection.

Invoked two ways, both injected transiently at spawn/resume time (never
written into the user's home config or the target repo -- see
server.ensure_hooks_settings_file / spawn._inject_agent_hooks):

- by a generated Claude Code hooks settings file, as
  ``python3 centrale_notify.py working|waiting|finished`` (plus whatever
  extra hook-payload arguments Claude Code itself appends -- ignored);
- by codex's ``-c notify=[...]`` override, as
  ``python3 centrale_notify.py finished <json-payload>`` -- codex always
  appends one extra JSON argument describing the completed turn, also
  ignored; codex only ever reports "finished" this way, since notify has
  no equivalent of claude's working/waiting hook points.

POSTs ``{"state": "<state>"}`` to ``$CENTRALE_EVENT_URL`` (set on the
spawned session's own environment -- see spawn.event_url) and exits 0
either way. A hook must never be able to break or hang the agent it's
attached to, so every failure here -- a missing/malformed argv, an unset
CENTRALE_EVENT_URL, a network error, a timeout -- is swallowed silently:
no output, no non-zero exit, ever.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

TIMEOUT_SECONDS = 2


def _post_event(url, state):
    """POST {"state": state} to url. Raises on any failure -- callers
    (main) are the ones responsible for swallowing it."""
    body = json.dumps({"state": state}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS)


def main(argv):
    """argv is sys.argv (argv[0] the script path, argv[1] the state,
    anything past that ignored -- see module docstring). No-ops silently
    if the state argument or CENTRALE_EVENT_URL is missing, or if
    posting the event fails for any reason."""
    if len(argv) < 2:
        return
    state = argv[1]

    url = os.environ.get("CENTRALE_EVENT_URL")
    if not url:
        return

    try:
        _post_event(url, state)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:
        pass
    sys.exit(0)
