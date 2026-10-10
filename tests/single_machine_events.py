"""The single-machine status-event script behind task-209's before/after
check (doc-3, "Single-machine guarantee"): a fixed sequence of hook POSTs
through the real POST /api/agent-event route, for a claude and a codex
agent, and what an orchestrator then hears plus every badge along the way.

Run as a script it prints that record as JSON; tests/test_remote_status.py
compares a fresh run with the record captured from the code BEFORE task-209
(fixtures/single_machine_events.json). Hermetic: an ephemeral loopback
port, an in-memory journal, manual codex timers and a faked pane capture.
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet
import orchestrator
import server

from lifecycle_harness import ManualTimer


# (task, agentKind, state, firedAt offset, fire pending codex timers after)
SCRIPT = [
    ("TASK-1", "claude", "working", 1, False),
    ("TASK-1", "claude", "working", 2, False),
    ("TASK-1", "claude", "waiting", 3, False),
    ("TASK-1", "claude", "working", 4, False),
    ("TASK-1", "claude", "finished", 6, False),
    ("TASK-1", "claude", "finished", 5, False),  # a retry overtaken: dropped
    ("TASK-2", "codex", "working", 1, False),
    ("TASK-2", "codex", "waiting", 2, True),
    ("TASK-2", "codex", "working", 3, False),
    ("TASK-2", "codex", "finished", 4, True),
    ("TASK-2", "codex", "finished", 5, True),
    ("TASK-3", None, "finished", 1, False),
]


def hooks():
    """The hook argv a local claude and codex agent are launched with."""
    import spawn
    with mock.patch.object(server, "probe_codex_hook_trust", return_value=True):
        record = {"claude": spawn._inject_agent_hooks(["claude"]),
                  "codex": spawn._inject_agent_hooks(["codex"])}
    return json.loads(json.dumps(record).replace(server.notify_script_path(), "<notify>"))


def run():
    # Orchestrator history lives for the process: a fresh project per run.
    PROJECT = "single-machine-events-" + uuid.uuid4().hex
    timers = []

    class Timer(ManualTimer):
        def start(self):
            timers.append(self)

    config = {"worktreeRoot": "/unused", "projects": [{"name": PROJECT, "path": "/unused"}]}
    httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, config)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    server._reset_agent_events()
    base = time.time() - 100
    record = {"hooks": hooks(), "badges": [], "orchestrator": []}
    try:
        with mock.patch.object(server, "fleet_history", fleet.Journal()), \
             mock.patch.object(server, "_agent_wait_timer", Timer), \
             mock.patch.object(server, "capture_session_pane",
                               return_value=["› Ask Codex to do anything"]):
            for task, kind, state, offset, fire in SCRIPT:
                params = {"project": PROJECT, "task": task}
                if kind:
                    params["agentKind"] = kind
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                try:
                    conn.request("POST", "/api/agent-event?" + urllib.parse.urlencode(params),
                                 body=json.dumps({"state": state, "firedAt": base + offset}),
                                 headers={"Content-Type": "application/json"})
                    status = conn.getresponse().status
                finally:
                    conn.close()
                if fire:
                    # One round: an unconfirmed codex wait re-arms itself.
                    pending, timers[:] = list(timers), []
                    for timer in pending:
                        timer.fire()
                record["badges"].append([task, state, status,
                                         server.get_agent_lifecycle(PROJECT, task)])
        cursor = None
        while True:
            line = orchestrator.wait(PROJECT, cursor, timeout=0)
            cursor, message = line.rstrip("\n").split(" ", 1)
            if message == "nothing yet":
                break
            record["orchestrator"].append(message)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        server._reset_agent_events()
    return record


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
