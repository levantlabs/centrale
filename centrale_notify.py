#!/usr/bin/env python3
"""Agent lifecycle event helper for task-37's hook/notify injection.

Invoked two ways, both injected transiently at spawn/resume time (never
written into the user's home config or the target repo -- see
server.hooks_settings_payload / spawn._inject_agent_hooks):

- by Claude Code hooks passed inline via ``--settings '<json>'``, as
  ``python3 centrale_notify.py working|notification|finished`` (plus whatever
  extra hook-payload arguments Claude Code itself appends -- ignored).
  The notification mode reads the stdin JSON and only forwards requests
  for input as waiting; idle reminders and unrelated notifications are ignored;
- by codex's ``-c notify=[...]`` override, as
  ``python3 centrale_notify.py finished <json-payload>`` -- codex always
  appends one extra JSON argument describing the completed turn, also
  ignored; codex only ever reports "finished" this way, since notify has
  no equivalent of claude's working/waiting hook points.

POSTs ``{"state": "<state>", "firedAt": <epoch>}`` to
``$CENTRALE_EVENT_URL`` (set on the spawned session's own environment --
see spawn.event_url) and exits 0 either way. A hook must never be able to
break or hang the agent it's attached to, so every failure here -- a
missing/malformed argv, an unset CENTRALE_EVENT_URL, a network error, a
timeout -- is swallowed silently: no output, no non-zero exit, ever.

task-201: when the server cannot be reached (a restart, say), a detached
child keeps retrying for RETRY_SECONDS while this process exits at once,
so the agent is never held up and an event fired during a quick restart
still lands. firedAt lets the server drop a retried event that a later
one has already overtaken.

task-227: Claude working events also forward four optional provenance
identifiers: tool_name, agent_id, prompt_id and session_id. No tool input,
prompt text or transcript path is sent. The receiving server stores only
a short session-ID hash in the journal; retries preserve the provenance.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 2
STDIN_WAIT_SECONDS = 0.5
RETRY_SECONDS = 8
RETRY_INTERVAL_SECONDS = 0.25
INPUT_NOTIFICATIONS = frozenset({
    "permission_prompt", "elicitation_dialog", "elicitation_url_dialog",
    "agent_needs_input",
})


# task-227: provenance only, never tool arguments, prompt text or paths.
# Claude's tool names and correlation IDs are bounded plain identifiers.
PROVENANCE_FIELDS = ("tool_name", "agent_id", "prompt_id", "session_id")
PROVENANCE_VALUE_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,200}")


def run_tmux_note(pane, state, fired_at):
    """Best-effort local pane note through an injectable subprocess boundary.

    The hook's TMUX environment selects its own server. A short timeout keeps
    a stuck tmux from delaying the HTTP report (task-223).
    """
    subprocess.run(["tmux", "set-option", "-p", "-t", pane,
                    "@centrale_state", f"{state} {fired_at}"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=0.5, check=False)


def hook_provenance(payload):
    """Allowlist optional Claude hook identifiers; malformed fields are absent.
    Shared with the receiving server so direct POSTs cannot bypass the filter.
    The server hashes session_id before it reaches the fleet journal."""
    if not isinstance(payload, dict):
        return {}
    return {key: value for key in PROVENANCE_FIELDS
            if isinstance(value := payload.get(key), str)
            and PROVENANCE_VALUE_RE.fullmatch(value)}


def _post_event(url, state, fired_at, timeout=TIMEOUT_SECONDS, hook=None, provenance=None):
    """POST state, firedAt, optional hook and allowlisted provenance to url.
    Raises on any failure -- callers (main) are the ones responsible for
    swallowing it."""
    payload = {"state": state, "firedAt": fired_at}
    if hook:
        payload["hook"] = hook
    payload.update(hook_provenance(provenance))
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    urllib.request.urlopen(req, timeout=timeout)


def _retry_until_delivered(url, state, fired_at, hook=None, provenance=None):
    """Bounded retry; stops at the first answer, even an error status --
    a server that answered has heard the event and judged it."""
    deadline = time.monotonic() + RETRY_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(RETRY_INTERVAL_SECONDS, remaining))
        try:
            _post_event(url, state, fired_at,
                        timeout=max(0.1, min(TIMEOUT_SECONDS, deadline - time.monotonic())),
                        hook=hook, provenance=provenance)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            continue


def _retry_in_background(url, state, fired_at, hook=None, provenance=None):
    """Fork a detached retrier and return at once. The child leaves the
    hook's session and drops its stdio, so neither the agent's process
    group nor a runner waiting for the hook's output waits for it."""
    if os.fork():
        return
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        _retry_until_delivered(url, state, fired_at, hook, provenance)
    finally:
        os._exit(0)


def _stdin_payload():
    """The hook's stdin JSON, or {} -- never blocks: only a pipe that has
    data (or EOF) within a moment is read."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        try:
            ready = select.select([sys.stdin], [], [], STDIN_WAIT_SECONDS)[0]
        except (OSError, ValueError):  # not a real fd (a stand-in stream)
            ready = True
        if not ready:
            return {}
        payload = json.load(sys.stdin)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


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
        payload = _stdin_payload()
        if state == "notification":
            # Claude sends the notification type on stdin. An idle_prompt
            # is a delayed reminder about a previous stop, even when a new
            # turn is already working; it must never overwrite that turn.
            # Unknown/malformed notifications establish no need for input.
            if payload.get("notification_type") not in INPUT_NOTIFICATIONS:
                return
            state = "waiting"
        # task-226: name the hook point. Claude's stdin JSON says which
        # fired; the registered event (argv[2], claude hooks only -- codex
        # appends a JSON payload there) is the fallback.
        hook = payload.get("hook_event_name")
        if not isinstance(hook, str):
            hook = argv[2] if len(argv) > 2 and argv[2].isalpha() else None
        # Identity comes from the same launch URL the server uses; codex
        # hook payloads and non-working states do not need this diagnostic.
        kind = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("agentKind")
        details = hook_provenance(payload) if kind == ["claude"] and state == "working" else {}
        extra = {"provenance": details} if details else {}
        fired_at = time.time()
        pane = os.environ.get("TMUX_PANE")
        if pane and state in ("working", "waiting", "finished"):
            try:
                run_tmux_note(pane, state, fired_at)
            except Exception:
                pass  # a pane failure cannot prevent the HTTP event
        try:
            _post_event(url, state, fired_at, hook=hook, **extra)
        except urllib.error.HTTPError:
            return  # the server answered; retrying cannot change its verdict
        except Exception:
            _retry_in_background(url, state, fired_at, hook, **extra)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:
        pass
    sys.exit(0)
