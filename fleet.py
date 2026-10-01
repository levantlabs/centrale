"""Durable observations for fleet views, never task or live-session authority.

Only main() opens the configured journal. Importing this module has no I/O;
embedded servers/tests can inject a Journal with an isolated path (or none).
"""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

RETENTION = 48 * 60 * 60
DEFAULT_WINDOW = 2 * 60 * 60
STATES = {'spawn', 'working', 'waiting', 'idle', 'finished', 'unknown',
          'merge blocked', 'merged', 'session ended'}


def history_path():
    override = os.environ.get('CENTRALE_FLEET_LOG')
    if override:
        return Path(override)
    home = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return Path(home) / 'centrale' / 'fleet.jsonl'


def parse_window(value):
    try:
        window = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'window must be seconds greater than 0 and at most {RETENTION}') from None
    if not math.isfinite(window) or not 0 < window <= RETENTION:
        raise ValueError(f'window must be seconds greater than 0 and at most {RETENTION}')
    return window


def _valid(row):
    return (isinstance(row, dict)
            and all(isinstance(row.get(k), str) and row[k]
                    for k in ('project', 'taskId', 'agent', 'state'))
            and row['state'] in STATES
            and type(row.get('timestamp')) in (int, float)
            and -1e15 < row['timestamp'] < 1e15)


class Journal:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.last_survey = 0.0
        self.rows = []
        self.skipped = 0
        self.error = None
        if self.path is not None:
            try:
                with self.path.open('rb') as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                            if not _valid(row):
                                raise ValueError('invalid history entry')
                            self.rows.append(row)
                        except (ValueError, UnicodeDecodeError):
                            self.skipped += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.error = str(exc)
        self.rows.sort(key=lambda row: row['timestamp'])
        self._prune()
        for row in self.rows:
            key = (row["project"], row["taskId"])
            if row["state"] == "session ended":
                self.sessions.pop(key, None)
            elif row["state"] not in {"merged", "merge blocked"}:
                self.sessions[key] = {"created": None, "observedAt": 0.0}
        if self.skipped:
            # Remove an incomplete final line before the next append, so
            # one crashed write cannot swallow the next valid observation.
            self._rewrite()

    def _rewrite(self):
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                for row in self.rows:
                    stream.write(json.dumps(row) + '\n')
            os.replace(temporary, self.path)
            self.error = None
        except OSError as exc:
            self.error = str(exc)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    def _prune(self):
        cutoff = self.clock() - RETENTION
        kept = [row for row in self.rows if row['timestamp'] >= cutoff]
        if len(kept) != len(self.rows):
            self.rows = kept
            self._rewrite()

    def latest(self, project, task_id):
        with self.lock:
            return next((dict(r) for r in reversed(self.rows)
                         if r['project'] == project and r['taskId'] == task_id.upper()), {})

    def agent(self, project, task_id):
        return self.latest(project, task_id).get('agent', 'unknown')

    def append(self, project, task_id, agent, state, timestamp=None, **details):
        row = dict(project=project, taskId=str(task_id).upper(), agent=agent or 'unknown',
                   state=state, timestamp=self.clock() if timestamp is None else timestamp)
        row.update(details)
        if not _valid(row):
            raise ValueError('invalid history entry')
        with self.lock:
            self._prune()
            self.rows.append(row)
            self.rows.sort(key=lambda entry: entry['timestamp'])
            if self.path is not None:
                if self.error:
                    self._rewrite()
                else:
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        with self.path.open('a', encoding='utf-8') as stream:
                            stream.write(json.dumps(row) + '\n')
                    except OSError as exc:
                        # Recording must not turn a successful spawn/merge
                        # into an HTTP failure. The fleet response exposes it.
                        self.error = str(exc)

    def snapshot(self, window=DEFAULT_WINDOW):
        with self.lock:
            self._prune()
            now = self.clock()
            return {'timestamp': now, 'window': window, 'retention': RETENTION,
                    'history': [dict(r) for r in self.rows
                                if now - window <= r['timestamp'] <= now],
                    'skippedLines': self.skipped, 'historyError': self.error}
