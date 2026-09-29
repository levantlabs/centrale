"""Optional notification history for GET /api/orchestrator-wait (task-174).

These are past hook/harvest observations, never task state or merge authority.
Keep every notification for this process's lifetime so a caller can replay a
burst without gaps, even when no request was waiting. Restart clears history;
a per-project random stream id makes that loss explicit to returning callers.
No files, subprocesses, polling threads, or resources outside this process.
"""

import math
import re
import threading
import time
import uuid


DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 300.0
_condition = threading.Condition()
_streams = {}
_last_harvest_lines = {}
_CURSOR_RE = re.compile(r"([a-f0-9]{32}):([0-9]{1,20})\Z")


class WaitError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _stream(project):
    # Caller holds _condition. Nothing prunes this history: consuming an
    # event in one client must not consume it for another (or a retry).
    if project not in _streams:
        _streams[project] = (uuid.uuid4().hex, [])
    return _streams[project]


def publish(project, task_id, message):
    # Hook task ids are validated at ingress; harvest reasons can contain
    # multiline command output. The wire format must remain one plain line.
    line = " ".join(f"{str(task_id).upper()} {message}".split())
    with _condition:
        _, events = _stream(project)
        events.append(line)
        _condition.notify_all()


def publish_harvest(event):
    if event.get("merged"):
        message = "merged"
    elif event.get("alreadyMerged"):
        message = "merged (already merged)"
    else:
        message = "merge blocked: " + (event.get("error") or event.get("reason") or "unknown gate")
    key = (event["project"], str(event["taskId"]).upper())
    message = " ".join(message.split())
    always_publish = event.get("merged")
    # Auto-harvest evaluates every branch each cycle. Remember only the
    # last published harvest line for this task, so an unchanged gate,
    # error or already-merged outcome does not repeatedly wake the master
    # (task-174 review). Only an actual merge always publishes.
    # The condition's RLock makes comparison and publication atomic;
    # publish() re-enters it to append and notify the waiting clients.
    with _condition:
        if not always_publish and _last_harvest_lines.get(key) == message:
            return
        publish(*key, message)
        _last_harvest_lines[key] = message


def wait(project, after=None, timeout=DEFAULT_TIMEOUT):
    """Return '<cursor> <observation>\n', or '<cursor> nothing yet\n'.

    Omitted cursor starts at this process's first event for the project.
    Retrying a cursor replays the same event; advancing it gets exactly the
    next event. A timeout never advances it. One condition covers publication
    and checking/waiting, so arrival at the check/sleep boundary cannot be lost.
    """
    try:
        timeout = float(timeout)
    except (ValueError, TypeError):
        raise WaitError(f"timeout must be seconds between 0 and {MAX_TIMEOUT:g}") from None
    if not math.isfinite(timeout) or not 0 <= timeout <= MAX_TIMEOUT:
        raise WaitError(f"timeout must be seconds between 0 and {MAX_TIMEOUT:g}")
    parsed = None
    if after is not None:
        parsed = _CURSOR_RE.fullmatch(after)
        if parsed is None:
            raise WaitError("invalid cursor: use the position from the previous response")
    deadline = time.monotonic() + timeout
    with _condition:
        stream_id, events = _stream(project)
        position = 0
        if parsed is not None:
            position = int(parsed[2])
            if parsed[1] != stream_id or position > len(events):
                raise WaitError(
                    "cursor unavailable: server restarted, project changed, or position is ahead; "
                    "reconcile from the board and retry without after", status=409,
                )
        while position == len(events):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return f"{stream_id}:{position} nothing yet\n"
            _condition.wait(remaining)
        return f"{stream_id}:{position + 1} {events[position]}\n"
