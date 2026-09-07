"""Centrale backend: config loading, board aggregation, and a small stdlib
HTTP server exposing the JSON API described in docs/api.md.

Python 3.12, standard library only. Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import http.server
import json
import mimetypes
import os
import re
import shlex
import shutil
import signal
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid

import version  # the one place the version number is written down

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "projects.json")

DEFAULT_STATUSES = ["To Do", "In Progress", "Done"]
TASK_ID_RE = re.compile(r"^[A-Za-z]+-[0-9]+(\.[0-9]+)*$")
# task-121: the two destructive throwaway routes echo back the exact
# uncommitted paths GET /api/discard-preview listed, so their body scales
# with the worktree the way that response already does. The ordinary 64K
# cap would make a genuinely huge dirty worktree the one thing that could
# not be thrown away, which is backwards.
DISCARD_BODY_MAX_BYTES = 1024 * 1024
# task-121: what a destructive confirm may name as the tip it reviewed.
# Full 40-char SHAs only -- an abbreviation would have to be resolved to be
# compared, and "the state you reviewed" is not a thing to resolve loosely.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# The prefix every session name carries (see spawn.session_name), and the
# only thing the session-name readers below match on. task-65's rename
# left a second, pre-rename prefix here so a session already running
# under the old name stayed listable/attachable/endable/mergeable;
# task-146 retired it once none were left alive.
SESSION_PREFIX = "centrale-"
_STATUSES_LINE_RE = re.compile(r'^\s*statuses:\s*(\[.*\])\s*$', re.MULTILINE)
# task-91: one line of `backlog milestone list --plain --show-completed`,
# e.g. "  m-0: post-0.2.0 (0/7 done)". The section headings ("Active
# milestones (1):") sit at column 0 and the placeholder rows ("(none)")
# carry no "<id>:" prefix, so neither can match. The count is matched
# greedily off the END of the line, so a title containing parentheses
# stays intact.
_MILESTONE_LINE_RE = re.compile(r'^\s+([^\s:]+):\s+(.*)\s+\(\d+/\d+ done\)\s*$')

CACHE_TTL_SECONDS = 5.0

DEFAULT_BROWSER_PORT_BASE = 6421
DEFAULT_AGENTS = {"claude": ["claude"], "codex": ["codex"]}
DEFAULT_AGENT_NAME = "claude"

# Agent lifecycle events (task-37): the raw states a spawned agent can
# self-report via CENTRALE_EVENT_URL -- see record_agent_event / the
# generated hooks settings file below. ``idle`` is deliberately not an
# input event: it is the honest display state derived for a codex
# ``finished`` event, because codex cannot distinguish a completed task
# from a turn that ended waiting for a chat reply (task-62).
AGENT_STATES = ("working", "waiting", "finished")
AGENT_KINDS = ("claude", "codex")
HOOKS_SETTINGS_FILENAME = "hooks-settings.json"


class BacklogError(Exception):
    """Raised when a CLI boundary call (backlog, tmux, git) fails, times out,
    or returns data that can't be parsed as expected."""


class ConfigError(Exception):
    """Raised for a malformed projects.json that the user needs to fix —
    as opposed to a BacklogError from a runtime subprocess boundary. The
    message always names the offending key, so it can be surfaced to the
    user as-is."""


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def normalize_agents_map(raw_map, source="agents"):
    """Normalize a projects.json `agents` map to a single canonical shape:
    ``{name: {"cmd": [...], "promptSuffix": str | None, "resumeCmd":
    [...] | None}}``, so every other consumer (spawn.py) deals in exactly
    one representation.

    Each entry may be given in either of two forms:

    - a plain argv list, e.g. ``"codex": ["codex"]``
    - an object, e.g. ``{"cmd": ["claude", "--flag"], "promptSuffix":
      "...", "resumeCmd": ["claude", "--continue"]}``, where
      "promptSuffix" is optional and, when present and non-blank, gets
      appended (after a blank line) to the standard workflow prompt
      before it's passed to the agent; "resumeCmd" is also optional and,
      when present, is the argv spawn.py's resume() runs (no prompt
      argument) instead of "cmd" to continue an interrupted session for
      this agent -- see spawn.resume. Only the object form has anywhere
      to put either field; the plain argv-list form is exactly "cmd".

    Already-normalized entries pass through unchanged, so this is safe to
    call again on its own output.

    A missing/`None` map normalizes to `{}` (callers fall back to
    DEFAULT_AGENTS for that — an absent `agents` key is not an error). But
    once a map is present, every entry in it must be well-formed: this
    raises ConfigError, naming the offending agent key, for anything that
    isn't one of the two shapes above, or whose `cmd` is missing, empty, or
    not a list of strings, whose `promptSuffix` is present but not a
    string, or whose `resumeCmd` is present but not a non-empty list of
    strings.
    """
    if raw_map is None:
        return {}
    if not isinstance(raw_map, dict):
        raise ConfigError(f"projects.json: '{source}' must be an object mapping agent name to command")

    normalized = {}
    for key, value in raw_map.items():
        if not isinstance(key, str) or not key:
            raise ConfigError(f"projects.json: '{source}' has a non-string or empty agent name: {key!r}")

        if isinstance(value, list):
            cmd, suffix_raw, resume_cmd_raw = value, None, None
        elif isinstance(value, dict):
            cmd, suffix_raw = value.get("cmd"), value.get("promptSuffix")
            resume_cmd_raw = value.get("resumeCmd")
        else:
            raise ConfigError(
                f"projects.json: {source}.{key} must be a list of strings, or an object with a "
                f"'cmd' field, got {type(value).__name__}"
            )

        if not isinstance(cmd, list) or not cmd:
            raise ConfigError(f"projects.json: {source}.{key}.cmd must be a non-empty list of strings")
        if not all(isinstance(v, str) for v in cmd):
            raise ConfigError(f"projects.json: {source}.{key}.cmd must contain only strings")

        suffix = None
        if suffix_raw is not None:
            if not isinstance(suffix_raw, str):
                raise ConfigError(f"projects.json: {source}.{key}.promptSuffix must be a string")
            if suffix_raw.strip():
                suffix = suffix_raw

        resume_cmd = None
        if resume_cmd_raw is not None:
            if not isinstance(resume_cmd_raw, list) or not resume_cmd_raw:
                raise ConfigError(f"projects.json: {source}.{key}.resumeCmd must be a non-empty list of strings")
            if not all(isinstance(v, str) for v in resume_cmd_raw):
                raise ConfigError(f"projects.json: {source}.{key}.resumeCmd must contain only strings")
            resume_cmd = list(resume_cmd_raw)

        normalized[key] = {"cmd": list(cmd), "promptSuffix": suffix, "resumeCmd": resume_cmd}

    return normalized


HARVEST_MODES = ("click", "auto")


def normalize_harvest_config(raw):
    """Normalize the projects.json 'harvest' key to {"mode": "click" |
    "auto"}. Missing/None defaults to {"mode": "click"} (harvesting stays
    click-only unless explicitly opted into "auto") -- but once present,
    it must be well-formed: raises ConfigError for a non-object value or
    a 'mode' that isn't one of HARVEST_MODES, the same style as
    normalize_agents_map (a malformed *present* value is a config the
    user needs to fix, not something to silently paper over)."""
    if raw is None:
        return {"mode": "click"}
    if not isinstance(raw, dict):
        raise ConfigError("projects.json: 'harvest' must be an object, e.g. {\"mode\": \"click\"}")
    mode = raw.get("mode", "click")
    if not isinstance(mode, str) or mode not in HARVEST_MODES:
        raise ConfigError(f"projects.json: harvest.mode must be one of {HARVEST_MODES!r}, got {mode!r}")
    return {"mode": mode}


# task-60/61: the in-drawer live session pane. A tiered mode on ONE key
# rather than a bag of booleans, so the reply box (task-61) can be switched
# off independently of the read-only pane without a second, easily-
# forgotten setting:
#   "off"      -- no drawer section; GET /api/session-pane and
#                 POST /api/session-input both refuse (403).
#   "view"     -- read-only pane only; POST /api/session-input refuses.
#   "interact" -- pane plus the reply row (text box and the two keys).
# Each tier disables the feature end to end -- the endpoint refuses, not
# just the UI hiding -- so a disabled feature can't be driven by hand.
SESSION_PREVIEW_MODES = ("off", "view", "interact")
DEFAULT_SESSION_PREVIEW_MODE = "interact"
DEFAULT_SESSION_PANE_LINES = 40
MAX_SESSION_PANE_LINES = 200

# task-61: replying into a live session. A reply is only ever meaningful
# against a pane the user has just SEEN, so the server refuses (409) unless
# it captured that session's pane within this many seconds -- the drawer
# polls every ~2s while open, so a fresh drawer is always well inside it,
# and nothing else can drive the endpoint without first previewing. This
# is a mitigation, not atomicity: the prompt can still change between the
# capture and the keystroke landing (the UI copy says so too).
SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS = 10
# Single-line replies only (multi-line input is explicitly out of scope --
# that road ends at an embedded terminal). Anything longer than this is not
# a short reply.
MAX_SESSION_INPUT_TEXT_CHARS = 1000
# task-135: the two session keys, as tmux key NAMES (send-keys without -l).
# Restored after task-77 removed all three quick keys, with the evidence its
# removal note asked for: a claude session can be stopped dead by a startup
# modal ("Teach auto mode about your environment?"), and the text path
# cannot answer one -- a reply is pasted where there is no prompt and the
# trailing Enter confirms whatever option happens to be focused. Escape
# dismisses the modal; Enter is here because a dialog whose default the user
# CAN see in the pane above still needs a way to say yes.
#
# "y" stays gone: task-77's reason for it holds -- bare "y" answers a prompt
# style the current TUIs barely use -- and nothing since has argued otherwise.
# Arrow keys and Ctrl-C are out of scope for the same discipline.
#
# Allowlisted rather than open-ended on purpose: tmux silently sends an
# UNKNOWN key name as literal text (verified empirically), so a free-form
# "key" field would be a second text channel with none of the text
# validation.
SESSION_INPUT_KEYS = ("Escape", "Enter")


def normalize_session_preview_config(raw):
    """Normalize the projects.json 'sessionPreview' key to {"mode": "off" |
    "view" | "interact"}. Missing/None defaults to {"mode": "interact"}
    (pane and reply row are on unless explicitly dialed down) -- but once
    present it must be well-formed: raises ConfigError for a non-object
    value or a 'mode' not in SESSION_PREVIEW_MODES, exactly like
    normalize_harvest_config."""
    if raw is None:
        return {"mode": DEFAULT_SESSION_PREVIEW_MODE}
    if not isinstance(raw, dict):
        raise ConfigError(
            "projects.json: 'sessionPreview' must be an object, e.g. {\"mode\": \"view\"}"
        )
    mode = raw.get("mode", DEFAULT_SESSION_PREVIEW_MODE)
    if not isinstance(mode, str) or mode not in SESSION_PREVIEW_MODES:
        raise ConfigError(
            f"projects.json: sessionPreview.mode must be one of {SESSION_PREVIEW_MODES!r}, got {mode!r}"
        )
    return {"mode": mode}


def session_preview_mode(config):
    """The effective sessionPreview mode for a loaded (or test-built)
    config -- tolerant of a config dict that never went through
    load_config, the same way _handle_board reads harvest.mode."""
    return (config.get("sessionPreview") or {}).get("mode", DEFAULT_SESSION_PREVIEW_MODE)


DEFAULT_REFRESH_INTERVAL_SECONDS = 10
MIN_REFRESH_INTERVAL_SECONDS = 5


def normalize_refresh_interval(raw):
    """Normalize the projects.json 'refreshIntervalSeconds' key. Missing/
    None defaults to DEFAULT_REFRESH_INTERVAL_SECONDS -- but once present,
    it must be a whole number of seconds that's at least
    MIN_REFRESH_INTERVAL_SECONDS, the same "malformed present value is an
    error" style as harvest/agents."""
    if raw is None:
        return DEFAULT_REFRESH_INTERVAL_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(
            f"projects.json: refreshIntervalSeconds must be a whole number of seconds, got {raw!r}"
        )
    if raw < MIN_REFRESH_INTERVAL_SECONDS:
        raise ConfigError(
            f"projects.json: refreshIntervalSeconds must be at least {MIN_REFRESH_INTERVAL_SECONDS}, got {raw!r}"
        )
    return raw


SPAWN_PROMPT_PLACEHOLDER = "task_id"


def normalize_spawn_prompt(raw):
    """Normalize the projects.json top-level 'spawnPrompt' key (task-69):
    a user-supplied replacement for spawn.PROMPT_TEMPLATE. Missing/None
    -> None, meaning "use the built-in default". Once present it must be
    a non-blank string that is a valid str.format template whose ONLY
    field is {task_id} -- a template with no {task_id} would spawn every
    agent onto no task at all, and one with a stray {other} field (or an
    unbalanced brace) would blow up with a KeyError/ValueError at spawn
    time instead of here. All of those raise ConfigError naming the key,
    the same "malformed present value is an error" strictness as
    harvest/agents/refreshIntervalSeconds. The value is returned verbatim
    (no stripping) so what the agent gets is exactly what was written."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(
            "projects.json: spawnPrompt must be a non-empty string containing the "
            f"{{{SPAWN_PROMPT_PLACEHOLDER}}} placeholder, got {raw!r}"
        )
    try:
        fields = {
            name for _literal, name, _spec, _conv in string.Formatter().parse(raw)
            if name is not None
        }
    except ValueError as exc:
        raise ConfigError(f"projects.json: spawnPrompt is not a valid template: {exc}") from exc
    if SPAWN_PROMPT_PLACEHOLDER not in fields:
        raise ConfigError(
            f"projects.json: spawnPrompt must contain the {{{SPAWN_PROMPT_PLACEHOLDER}}} "
            "placeholder (it is replaced with the task id at spawn time)"
        )
    unknown = sorted(fields - {SPAWN_PROMPT_PLACEHOLDER})
    if unknown:
        raise ConfigError(
            f"projects.json: spawnPrompt may only use the {{{SPAWN_PROMPT_PLACEHOLDER}}} "
            f"placeholder; unknown placeholder(s) {unknown!r} (write a literal brace as {{{{ or }}}})"
        )
    return raw


# Every subprocess boundary (run_backlog, run_backlog_raw, run_git,
# run_tmux) shares this one configurable timeout, so a hung external CLI
# can never freeze board rendering, a spawn, or a merge -- see
# configure_subprocess_timeout. checkCommand gets its own, much longer
# default (harvest.py's gate 5 can legitimately take minutes) via a
# separate, per-project 'checkTimeoutSeconds'.
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 30
DEFAULT_CHECK_TIMEOUT_SECONDS = 600


def normalize_subprocess_timeout(raw):
    """Normalize the projects.json top-level 'subprocessTimeoutSeconds'
    key. Missing/None defaults to DEFAULT_SUBPROCESS_TIMEOUT_SECONDS; a
    present value must be a positive number of seconds -- same
    "malformed present value is an error" style as harvest/agents/
    refreshIntervalSeconds, since this is a top-level key."""
    if raw is None:
        return DEFAULT_SUBPROCESS_TIMEOUT_SECONDS
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(
            f"projects.json: subprocessTimeoutSeconds must be a positive number of seconds, got {raw!r}"
        )
    if raw <= 0:
        raise ConfigError(
            f"projects.json: subprocessTimeoutSeconds must be a positive number of seconds, got {raw!r}"
        )
    return raw


def _normalize_check_timeout(raw):
    """Normalize one project's 'checkTimeoutSeconds'. Missing/None, or
    any malformed value, quietly falls back to
    DEFAULT_CHECK_TIMEOUT_SECONDS -- the same lenient, silently-corrected
    style already used for a project's other per-project fields
    (browserPort, checkCommand), rather than the top-level keys' "raise
    ConfigError" style: one project's mistyped override shouldn't refuse
    to start the whole server."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return DEFAULT_CHECK_TIMEOUT_SECONDS
    return raw


# The '@repo' sentinel for 'worktreeRoot' (global, or a per-project
# override): "this project's own worktrees, not a shared sibling
# directory" -- spawn.worktree_dir resolves it to
# <project path>/.centrale-worktrees per project, since there's no one
# real path to expand it to here (a different one for every project).
# See `worktreeRoot` in docs/configuration.md for why: codex's own sandboxing
# trusts a spawned worktree only if it's a filesystem descendant of an
# already-trusted repo, which a sibling directory under the OLD default
# (a hardcoded directory under the original author's home, task-53
# replaced this as the fallback below with '@repo' itself) never is.
WORKTREE_ROOT_REPO_TOKEN = "@repo"


def normalize_worktree_root(raw):
    """Normalize the projects.json top-level 'worktreeRoot' key.
    Missing/falsy defaults to the '@repo' sentinel (task-53): the
    previous fallback -- a hardcoded directory under the original
    author's home -- was a one-machine path baked into the code, not a
    real default a stranger's clone has any reason to share, and it was
    already strictly worse than '@repo' on its own merits (see
    WORKTREE_ROOT_REPO_TOKEN's own comment above: codex's sandboxing
    trusts an in-repo worktree but not a shared-sibling one). Anyone who
    was actually relying on the old hardcoded path via an omitted key
    was already broken on any machine but the original author's; the
    shipped projects.json and projects.example.json both always set
    'worktreeRoot' explicitly regardless, so this only changes behavior
    for a config that both omits the key entirely AND happens to run on
    a machine with that exact directory layout. The literal string
    '@repo' is preserved as-is rather than expanded -- it's a sentinel
    resolved per-project (see WORKTREE_ROOT_REPO_TOKEN) -- and any other
    string is ~-expanded as before."""
    if not raw:
        return WORKTREE_ROOT_REPO_TOKEN
    if raw == WORKTREE_ROOT_REPO_TOKEN:
        return raw
    return os.path.expanduser(raw)


def _normalize_project_worktree_root(raw):
    """Normalize one project's optional 'worktreeRoot' override (same
    lenient, silently-corrected style as checkTimeoutSeconds/browserPort
    -- a malformed override just means "no override", never a startup
    failure). None means "use the global worktreeRoot" -- see
    normalize_worktree_root for what a real value means."""
    if not raw or not isinstance(raw, str):
        return None
    if raw == WORKTREE_ROOT_REPO_TOKEN:
        return raw
    return os.path.expanduser(raw)


def load_config(path=None):
    """Load projects.json (next to this file by default), expanding ``~`` in
    worktreeRoot and each project path. Missing file / missing keys fall back
    to sensible defaults (see normalize_worktree_root for the 'worktreeRoot'
    one specifically) -- a missing FILE (task-53's "zero-config" first run)
    additionally sets the returned config's "zeroConfig" flag, used by
    main()'s startup line and run_doctor_check()'s --check guidance.

    Raises ConfigError if the `agents` map, the `harvest` key, or the
    `spawnPrompt` template is present but malformed (see
    normalize_agents_map / normalize_harvest_config / normalize_spawn_prompt) --
    that's a config the user needs to fix, so it's surfaced loudly rather
    than silently degraded. This only ever happens for a file that DOES
    exist and DOES parse as JSON but has a bad shape -- zero-config mode
    (the file doesn't exist at all) never raises, by construction: there's
    no user-authored content to be malformed."""
    if path is None:
        path = DEFAULT_CONFIG_PATH

    raw = {}
    zero_config = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
        zero_config = True

    if not isinstance(raw, dict):
        raw = {}

    port = raw.get("port", 7420)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 7420

    worktree_root = normalize_worktree_root(raw.get("worktreeRoot"))

    browser_port_base = raw.get("browserPortBase", DEFAULT_BROWSER_PORT_BASE)
    try:
        browser_port_base = int(browser_port_base)
    except (TypeError, ValueError):
        browser_port_base = DEFAULT_BROWSER_PORT_BASE

    agents = normalize_agents_map(raw.get("agents"))
    if not agents:
        agents = normalize_agents_map(DEFAULT_AGENTS)

    default_agent = raw.get("defaultAgent")
    if not isinstance(default_agent, str) or not default_agent:
        default_agent = DEFAULT_AGENT_NAME

    harvest_config = normalize_harvest_config(raw.get("harvest"))
    session_preview_config = normalize_session_preview_config(raw.get("sessionPreview"))
    refresh_interval_seconds = normalize_refresh_interval(raw.get("refreshIntervalSeconds"))
    subprocess_timeout_seconds = normalize_subprocess_timeout(raw.get("subprocessTimeoutSeconds"))
    spawn_prompt = normalize_spawn_prompt(raw.get("spawnPrompt"))

    projects = []
    for entry in raw.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        proj_path = entry.get("path", "")
        if not name:
            continue
        browser_port = entry.get("browserPort")
        try:
            browser_port = int(browser_port) if browser_port is not None else None
        except (TypeError, ValueError):
            browser_port = None
        check_command = entry.get("checkCommand")
        if not isinstance(check_command, str) or not check_command.strip():
            check_command = None
        check_timeout_seconds = _normalize_check_timeout(entry.get("checkTimeoutSeconds"))
        project_worktree_root = _normalize_project_worktree_root(entry.get("worktreeRoot"))
        projects.append({
            "name": name,
            "path": os.path.expanduser(proj_path or ""),
            "browserPort": browser_port,
            "checkCommand": check_command,
            "checkTimeoutSeconds": check_timeout_seconds,
            "worktreeRoot": project_worktree_root,
        })

    return {
        "port": port,
        "worktreeRoot": worktree_root,
        "projects": projects,
        "agents": agents,
        "defaultAgent": default_agent,
        "browserPortBase": browser_port_base,
        "harvest": harvest_config,
        "sessionPreview": session_preview_config,
        "refreshIntervalSeconds": refresh_interval_seconds,
        "subprocessTimeoutSeconds": subprocess_timeout_seconds,
        "spawnPrompt": spawn_prompt,
        "zeroConfig": zero_config,
    }


# ---------------------------------------------------------------------------
# Subprocess boundary functions (kept tiny + patchable for tests)
# ---------------------------------------------------------------------------

# Shared by run_backlog/run_backlog_raw/run_git/run_tmux -- see
# configure_subprocess_timeout, called once from main() with the loaded
# config's subprocessTimeoutSeconds. Deliberately a plain module global
# (matching browser.py's _atexit_registered / this module's own
# _board_cache) rather than a side effect of load_config() itself, so
# calling load_config() in a test never quietly changes this process's
# shared timeout for unrelated tests.
_subprocess_timeout_seconds = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS


def configure_subprocess_timeout(seconds):
    """Sets the timeout run_backlog/run_backlog_raw/run_git/run_tmux all
    use for every call they make, from here on. One shared knob (see
    DEFAULT_SUBPROCESS_TIMEOUT_SECONDS / normalize_subprocess_timeout),
    not four separate ones -- a hung external CLI on any of these must
    never freeze board rendering, a spawn, or a merge."""
    global _subprocess_timeout_seconds
    _subprocess_timeout_seconds = seconds


def _run(cmd, cwd=None, timeout=15, input=None):
    """Run a command, never raising for a non-zero exit. Returns a
    subprocess.CompletedProcess, synthesizing one for a missing binary or a
    timeout so callers always get the same shape back. `input`, when not
    None, is fed to the child's stdin (text) -- how send_session_input
    hands reply text to `tmux load-buffer -` (task-72)."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", str(exc))


def run_backlog(args, cwd):
    """Run `backlog <args>` in cwd and return the parsed JSON dict.

    Raises BacklogError on a non-zero exit, invalid JSON, a missing
    `backlog` binary, or a timeout (subprocess.TimeoutExpired surfaces as
    the same synthesized returncode=124 _run() gives any other failure,
    so this raises the same BacklogError style either way).
    """
    proc = _run(["backlog", *args], cwd=cwd, timeout=_subprocess_timeout_seconds)
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "backlog command failed").strip()
        raise BacklogError(message or f"backlog {' '.join(args)} failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BacklogError(f"invalid JSON from backlog CLI: {exc}") from exc


def run_backlog_raw(args, cwd):
    """Run `backlog <args>` in cwd without treating stdout as JSON.

    Returns a subprocess.CompletedProcess; does not raise on a non-zero
    exit (a timeout included -- see _run). For write commands (e.g.
    `task edit`) where the caller only cares about success/failure, not a
    parsed payload — unlike run_backlog, which is for the read commands
    (`task list`, `task view`) whose `--json` output this app depends on.
    """
    return _run(["backlog", *args], cwd=cwd, timeout=_subprocess_timeout_seconds)


def run_git(args, cwd=None):
    """Run `git <args>` in cwd. Returns a subprocess.CompletedProcess; does
    not raise on a non-zero exit (a timeout included -- see _run)."""
    return _run(["git", *args], cwd=cwd, timeout=_subprocess_timeout_seconds)


def run_tmux(args, input=None):
    """Run `tmux <args>`. Returns a subprocess.CompletedProcess; does not
    raise on a non-zero exit (e.g. "no server running", or a timeout --
    see _run). `input` (text) goes to tmux's stdin -- only `load-buffer -`
    reads it (task-72); every other call leaves it None."""
    return _run(["tmux", *args], timeout=_subprocess_timeout_seconds, input=input)


def run_check_command(command, cwd, timeout=DEFAULT_CHECK_TIMEOUT_SECONDS):
    """Run a project's optional `checkCommand` (a shell-style string,
    shlex-split into argv -- never shell=True) in cwd. Returns a
    subprocess.CompletedProcess; does not raise on a non-zero exit, since
    a failing check is a harvest gate failing, not a crash -- a timeout
    is exactly that too (_run synthesizes the same returncode=124 shape
    for it as for any other failure, so harvest.py's gate 5 doesn't need
    to special-case it: a timeout just reads as "checkCommand failed").
    An empty/whitespace-only command is treated as a trivial pass. The
    default timeout is deliberately much longer than the other
    subprocess boundaries' -- a real test suite can legitimately take
    minutes -- and callers (harvest.py) pass the project's own
    checkTimeoutSeconds override explicitly rather than relying on it."""
    args = shlex.split(command or "")
    if not args:
        return subprocess.CompletedProcess(args, 0, "", "")
    return _run(args, cwd=cwd, timeout=timeout)


def which(name):
    """Thin wrapper around shutil.which, kept as its own injectable
    boundary function (like run_git/run_tmux) so tests can control
    external-tool-availability detection without depending on the real
    PATH of the machine running the suite."""
    return shutil.which(name)


# task-42: real feature-detection for codex's --dangerously-bypass-hook-
# trust flag, cached per binary path. Writing <worktree>/.codex/hooks.json
# is just a file write -- it succeeds unconditionally on any codex
# version, so it feature-detects nothing about the binary itself. An
# older codex (< 0.150.0, no hooks engine) doesn't recognize
# --dangerously-bypass-hook-trust at all and rejects the whole argv at
# startup, killing the spawned session outright -- so spawn.py must know
# whether this codex binary supports the flag *before* ever appending it,
# not find out by trying.
_codex_hook_trust_probe_cache = {}
_codex_hook_trust_probe_lock = threading.Lock()


def probe_codex_hook_trust(codex_argv0):
    """True iff `<codex_argv0> --help` advertises
    --dangerously-bypass-hook-trust in its output -- the only reliable
    signal that this codex binary has the hooks engine (and therefore the
    flag) at all. Runs through the same _run() boundary as every other
    subprocess call here (short timeout, never raises: a missing binary,
    a non-zero exit, or a timeout all just read as "no"). Checked against
    combined stdout+stderr, not gated on returncode==0, since --help
    conventions vary across CLI versions.

    Cached per `codex_argv0` (module-level, thread-safe) so a given
    binary is only ever probed once, not on every single spawn/resume --
    see _reset_codex_hook_trust_probe_cache for the test hook that clears
    it. See spawn._inject_codex_hooks for the caller that gates the
    entire hooks.json/trust-flag path on this."""
    with _codex_hook_trust_probe_lock:
        if codex_argv0 in _codex_hook_trust_probe_cache:
            return _codex_hook_trust_probe_cache[codex_argv0]
    proc = _run([codex_argv0, "--help"], timeout=5)
    output = (proc.stdout or "") + (proc.stderr or "")
    result = "dangerously-bypass-hook-trust" in output
    with _codex_hook_trust_probe_lock:
        _codex_hook_trust_probe_cache[codex_argv0] = result
    return result


def _reset_codex_hook_trust_probe_cache():
    """Test helper: clears probe_codex_hook_trust's cache (mirrors
    _reset_agent_events) so each test starts from a clean, unprobed
    state instead of leaking a cached result across tests."""
    with _codex_hook_trust_probe_lock:
        _codex_hook_trust_probe_cache.clear()


def run_bwrap_probe():
    """Runs a minimal `bwrap` invocation that requires an unprivileged
    user namespace -- the same primitive codex's own sandboxing needs --
    purely to check whether it works, never to actually sandbox
    anything here. Returns a subprocess.CompletedProcess; never raises.
    A separate small injectable boundary (like run_git/run_backlog_raw)
    so tests never execute a real bwrap. Only meaningful on Linux;
    callers gate on `which("bwrap")` first."""
    return _run(["bwrap", "--unshare-net", "--dev-bind", "/", "/", "true"], timeout=5)


def apparmor_userns_restricted():
    """Reads /proc/sys/kernel/apparmor_restrict_unprivileged_userns --
    Ubuntu 24.04+'s AppArmor control that, when set to 1, blocks
    unprivileged user-namespace creation (exactly what bwrap, and so
    codex's own sandboxing, needs) for any process without a permissive
    AppArmor profile. Returns True/False, or None if the file doesn't
    exist (not that kind of kernel/distro, so this doesn't apply) --
    kept as its own tiny injectable boundary, same reasoning as
    which()/run_bwrap_probe()."""
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def detect_capabilities():
    """Best-effort feature detection for optional external tools, meant
    to run once at server startup. Currently just tmux, which spawning
    depends on; more capabilities may be added here later."""
    return {"tmux": which("tmux") is not None}


def tmux_capability(config):
    """Whether spawning is available for this config. Defaults to True
    when a config has no 'capabilities' key at all (e.g. one built
    directly by a test, or never passed through detect_capabilities) --
    fail open, consistent with this module's other missing-key
    defaults."""
    return bool((config.get("capabilities") or {}).get("tmux", True))


def detect_version():
    """The build string this process is running, resolved once at server
    startup the same way detect_capabilities() resolves tmux (task-107).

    Two layers, in this order:

      1. `version.__version__` -- always available, ships as tracked
         content, needs no parsing. This is the floor, and it is the
         whole answer in a downloaded zip with no `.git` in it.
      2. `git describe --tags --always --dirty`, layered on when this
         checkout actually has a `.git`. It answers "which build" and
         "how far past the release" at once: in a released snapshot,
         whose release commit release.sh tagged, it reads a clean
         "v0.1.0"; in a development checkout it reads
         "v0.1.0-14-g4570911", whose suffix moves with every commit.

    A missing `git` binary, or a git that fails, times out, or prints
    nothing (a `.git` that is not readable, a repository with no commits
    at all), falls back to layer 1 rather than reporting anything
    uncertain -- the constant is never wrong about what shipped, it is
    only less specific. And when git answers but no tag is reachable
    yet, the two are shown together
    ("v0.1.0 (e773bdb-dirty)"): a bare hash names the build without
    naming the version the changelog is keyed to.

    Goes through the run_git boundary like every other subprocess call
    here, so a test drives it without a repository.
    """
    fallback = f"v{version.__version__}"
    # os.path.exists, not isdir: in a git WORKTREE (which is how every
    # agent in this repo runs) `.git` is a FILE pointing at the real
    # directory, and git describes it exactly as well.
    if which("git") is None or not os.path.exists(os.path.join(BASE_DIR, ".git")):
        return fallback
    proc = run_git(["describe", "--tags", "--always", "--dirty"], cwd=BASE_DIR)
    described = (proc.stdout or "").strip()
    if proc.returncode != 0 or not described:
        return fallback
    if described == fallback or described.startswith(fallback + "-"):
        # The normal case once a release has been tagged: describe
        # already begins with this very version ("v0.1.0",
        # "v0.1.0-14-g4570911-dirty"), so it says everything the
        # constant does and more. Show it alone.
        return described
    # No tag is reachable yet (before the first release, or in a clone
    # fetched without tags), so `--always` fell back to a bare commit
    # hash -- which names the build but not the version. Neither half
    # answers the question on its own here, so show both rather than
    # dropping the number the changelog and the tag are keyed to.
    return f"{fallback} ({described})"


def served_version(config):
    """The version string to report for this config -- captured at
    startup by main(), read back here.

    Falls back to the bare constant when a config never went through
    main() (one built directly by a test, say), the same fail-open
    default as tmux_capability(). It is deliberately NOT a fresh
    detect_version() call: re-resolving per request would happily report
    code the running process has never executed, which is exactly the
    failure decision-2 records -- task-91's fix sat invisible for twenty
    minutes behind a server that predated it. What this reports is what
    THIS PROCESS booted from, or nothing at all.
    """
    return config.get("version") or f"v{version.__version__}"


# ---------------------------------------------------------------------------
# task-128: is the running process behind the code on disk?
#
# Every .py file is loaded once at process start, so a merge that touches
# one has no effect until a restart -- and nothing on screen said so.
# Decision-2 records the discipline that stood in for a fix ("restart
# after any merge touching Python") and the day it was written the gap
# recurred anyway: a process serving v0.1.0 (055ff59) sat 23 commits
# behind its checkout, answering a DNS-rebinding probe with 200 while the
# code on disk refused it with 403. The evidence was already there --
# served_version() names the commit the process loaded -- nobody compared
# it with HEAD. This is the comparison.
#
# Derived, never remembered: the loaded side is the version string
# main() already captured (nothing else is stored), the current side is
# one `git rev-parse HEAD` in this checkout, run when asked. Any doubt
# degrades to silence -- a published snapshot has no .git, a machine may
# have no git, a dirty tree is not staleness -- because a false "restart
# me" would teach the reader to ignore the true one.
# ---------------------------------------------------------------------------

#: The two shapes detect_version() puts a commit hash into: the
#: `-g<hash>` suffix git describe appends past a tag
#: ("v0.1.0-14-g4570911", "-dirty" or not), and the bare hash `--always`
#: falls back to when no tag is reachable, which detect_version() shows
#: in parentheses after the constant ("v0.1.0 (e773bdb-dirty)"). An
#: exact tag ("v0.1.0") carries no hash at all -- see loaded_commit().
_DESCRIBE_SUFFIX_HASH_RE = re.compile(r"^.+-g([0-9a-f]{4,64})(?:-dirty)?$")
_BARE_HASH_RE = re.compile(r"^\S+ \(([0-9a-f]{4,64})(?:-dirty)?\)$")
_FULL_HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")


def loaded_commit(served):
    """The commit a served version string names, as (hash, is_tag).

    `hash` is the abbreviated hash git describe wrote into the string
    ("4570911" out of "v0.1.0-14-g4570911-dirty"), or, for a version that
    is an exact tag with no hash in it ("v0.1.0", "v0.1.0-dirty"), the
    tag name itself with `is_tag` True so the caller knows it still has
    to be resolved. None for anything else: a string this function does
    not recognise is not evidence of staleness, so code_drift() stays
    quiet on it rather than guessing.
    """
    if not isinstance(served, str) or not served.strip():
        return None
    s = served.strip()
    m = _DESCRIBE_SUFFIX_HASH_RE.match(s) or _BARE_HASH_RE.match(s)
    if m:
        return m.group(1), False
    tag = s[: -len("-dirty")] if s.endswith("-dirty") else s
    if not tag or " " in tag:
        return None
    return tag, True


def code_drift(config):
    """Whether THIS process is behind the checkout it runs from, derived
    on request from git and never stored (task-128).

    None means "no signal": either the two agree, or one side cannot be
    established -- a config that never went through main() and so
    captured no version, no `.git` next to this file (a published
    snapshot), no `git` on PATH, a git that fails or answers with
    something that is not a commit, or a version string with no
    recognisable commit in it. Every one of those is silence, never an
    error and never a signal.

    Otherwise a dict: `loaded` names the commit the process started from
    (the abbreviated hash, or the tag when the version was an exact
    tag), `current` names HEAD to the same width, and `commitsBehind` is
    `git rev-list --count loaded..HEAD` -- how many commits the process
    has not loaded -- or None when git cannot count (the loaded commit
    rewritten away, say). The count deliberately says nothing about
    WHICH files those commits touched: one merge of a stylesheet and
    eleven merges of server.py read the same here, and the message is
    honest about that -- code has changed, restart to load it.

    A `-dirty` suffix on the loaded side is not staleness: the process
    loaded that commit plus uncommitted edits, and if HEAD is still that
    commit there is nothing a restart would change that this can see.
    """
    parsed = loaded_commit(config.get("version"))
    if parsed is None:
        return None
    if which("git") is None or not os.path.exists(os.path.join(BASE_DIR, ".git")):
        return None
    loaded, is_tag = parsed
    if is_tag:
        proc = run_git(["rev-parse", "--verify", "--quiet", f"{loaded}^{{commit}}"], cwd=BASE_DIR)
        loaded_full = (proc.stdout or "").strip()
        if proc.returncode != 0 or not _FULL_HASH_RE.match(loaded_full):
            return None
    else:
        loaded_full = loaded
    proc = run_git(["rev-parse", "HEAD"], cwd=BASE_DIR)
    current = (proc.stdout or "").strip()
    if proc.returncode != 0 or not _FULL_HASH_RE.match(current):
        return None
    if current.startswith(loaded_full):
        return None
    proc = run_git(["rev-list", "--count", f"{loaded_full}..HEAD"], cwd=BASE_DIR)
    count = (proc.stdout or "").strip()
    commits_behind = int(count) if proc.returncode == 0 and count.isdigit() else None
    width = 7 if is_tag else len(loaded)
    return {
        "loaded": loaded,
        "current": current[:width],
        "commitsBehind": commits_behind,
    }


DOCTOR_PROBE_TIMEOUT_SECONDS = 5


def probe_running_server(port):
    """`GET /api/board` from a Centrale already listening on
    127.0.0.1:port, as the parsed dict -- or None when nothing answers
    like one (connection refused, a timeout, a non-JSON or non-board
    answer from whatever else holds the port). The one network call
    --check makes (task-128): it never binds, it only asks a process
    that is already up what IT loaded. Sends nothing but the request --
    no Origin, no Referer -- and the Host urllib sets is the loopback
    name at that port, exactly what _refuse_untrusted_request() admits.
    Its own tiny injectable boundary, like which()/run_bwrap_probe(), so
    the doctor is testable without a server."""
    import urllib.request  # local: the only use in this module
    url = f"http://127.0.0.1:{port}/api/board"
    try:
        with urllib.request.urlopen(url, timeout=DOCTOR_PROBE_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        # URLError (refused, unreachable, timed out) and HTTPError are
        # both OSError; a JSON or decoding failure is a ValueError.
        return None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        return None
    return data


# ---------------------------------------------------------------------------
# task-157: is a `backlog browser` Centrale launched behind the backlog
# on PATH? Same shape of question as code_drift() above, one layer down:
# upgrading a package on disk does not touch a process already running
# it, and after a 1.50.1 -> 1.51.0 upgrade three boards Centrale had
# launched kept answering 1.50.1 while the terminal said 1.51.0. The
# difference from Centrale's own staleness is that a board's version is
# trivially queryable -- it serves /api/version over HTTP on the port
# the registry already knows. Both halves below are boundaries, so the
# comparison in browser.version_drift() is testable without a board and
# without a `backlog` on PATH.
# ---------------------------------------------------------------------------

BROWSER_VERSION_PROBE_TIMEOUT_SECONDS = 2


def probe_browser_version(port):
    """The version a `backlog browser` listening on 127.0.0.1:port
    reports on its own /api/version, or None when nothing answers like
    one (refused, timed out, non-JSON, no `version` string -- whatever
    else holds the port).

    Sends nothing but the request, exactly like probe_running_server()
    above, and like it never binds: it only asks a process that is
    already up what IT loaded. None is silence, never an error -- a
    board that cannot be asked is not evidence of anything.
    """
    import urllib.request  # local: same as probe_running_server above
    url = f"http://127.0.0.1:{port}/api/version"
    try:
        with urllib.request.urlopen(url, timeout=BROWSER_VERSION_PROBE_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reported = data.get("version")
    return _version_token(reported)


def backlog_cli_version():
    """The version `backlog --version` prints on PATH, or None when it
    cannot be asked (missing, failing, silent, timing out -- _run turns
    all of those into a non-zero CompletedProcess) or printed nothing of
    that shape. Goes through backlog_version() (task-162) so this and
    the doctor's reading of the same CLI never disagree on what counts
    as a version."""
    proc = run_backlog_raw(["--version"], cwd=BASE_DIR)
    return backlog_version(proc)


def _version_token(raw):
    """The bare X.Y.Z out of either side's answer ("1.51.0" out of
    "1.51.0" and out of a hypothetical "backlog 1.51.0"), or None when
    `raw` is not a string or carries nothing of that exact shape
    (task-162: this is the same bounded search backlog_version() uses,
    so both sides of the drift comparison accept and reject the same
    inputs).

    The two sides are the same package asked two ways, so they normally
    agree verbatim; this only keeps a difference in how one of them
    chooses to *print* the number from reading as a version difference.
    A false "your board is stale" would teach the reader to ignore the
    true one -- the same reasoning code_drift() degrades to silence on.
    """
    if not isinstance(raw, str):
        return None
    # Not \b: a bare \b would refuse the "v1.51.0" half of a "backlog
    # v1.51.0" banner, since `v` and `1` are both word characters. The
    # bounds that matter are digits and dots -- "1.51" is not a version
    # and "1.51.0.2" is not this one, so both read as None.
    match = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", raw)
    return match.group(1) if match else None


def launch_browser_process(cmd, cwd):
    """Start a detached, long-running process (the `backlog browser` web
    UI) and return the Popen handle, without waiting for it to exit.

    Unlike `_run`, this doesn't capture output or wait for completion:
    callers need the live process object itself to poll liveness later.
    Raises OSError (e.g. FileNotFoundError) if the binary can't be started.
    """
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def port_is_free(port):
    """Best-effort, TOCTOU-prone-by-nature check: True if a socket can
    bind to 127.0.0.1:<port> right now. Used by browser.py both before
    launching (a port some *other*, untracked process already holds
    should never be silently handed out or reused) and immediately after
    (to confirm the new child actually came up and is listening, rather
    than trusting a live Popen handle alone -- a process can be alive
    without ever having bound anything)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


_SS_LISTENER_PID_RE = re.compile(r"pid=(\d+)")


def _listener_pid_from_ss(port):
    """Linux half of resolve_listener_pid: `ss -tlnp` (ships with
    iproute2 on effectively every Linux distro Centrale targets).
    Returns None -- never raises -- if `ss` isn't on PATH, nothing is
    listening on `port`, or the output carries no "pid=" detail (a
    locked-down `ss` omits it without root). Kept as its own function so
    tests can exercise the fallback below without a real `ss`."""
    proc = _run(["ss", "-tlnp", f"sport = :{port}"], timeout=5)
    if proc.returncode != 0:
        return None
    match = _SS_LISTENER_PID_RE.search(proc.stdout or "")
    return int(match.group(1)) if match else None


def _listener_pid_from_lsof(port):
    """Portable half of resolve_listener_pid (task-105): `lsof -nP
    -iTCP:<port> -sTCP:LISTEN`, which is how you ask this question on
    macOS and the BSDs, where there is no `ss` at all. Returns None --
    never raises -- if `lsof` isn't on PATH, nothing is listening, or no
    line can be parsed.

    The parse is deliberately tight rather than "second column of the
    first data row": it only accepts a row whose NAME column ends in the
    `(LISTEN)` marker and whose PID column is a positive integer, so
    output from some *other* tool (or an lsof that printed a warning
    banner first) can never yield a bogus pid that a caller would then
    try to kill."""
    proc = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], timeout=5)
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        if "(LISTEN)" not in line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[1])
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def resolve_listener_pid(port):
    """Best-effort: the pid of whatever process is actually listening on
    127.0.0.1:<port> right now. This exists because `backlog browser` is
    a node wrapper that immediately forks the real listening server,
    which then reparents away -- the pid launch_browser_process's Popen
    handle tracks is the wrapper, not the process actually holding the
    port, so killing only that pid can leave the real server running
    forever (see browser.py's registry).

    Tries `ss` first (_listener_pid_from_ss) and falls back to `lsof`
    (_listener_pid_from_lsof) only when that produced nothing: on Linux
    the fast path is unchanged and costs no extra process, on macOS/BSD
    -- where `ss` doesn't exist and _run synthesizes returncode 127 --
    the fallback is what actually answers, and on a locked-down Linux
    whose `ss` omits "pid=" without root, `lsof` gets a second try.

    Returns None -- never raises -- when neither method can name a pid:
    callers degrade to tracking only the wrapper pid, exactly as before
    this existed. Both halves are separate small injectable boundaries
    (like run_git/run_backlog_raw), so tests never execute a real `ss`
    or `lsof`."""
    return _listener_pid_from_ss(port) or _listener_pid_from_lsof(port)


def _cmdline_from_procfs(pid):
    """Linux half of process_cmdline: /proc/<pid>/cmdline, space-joined.
    None if there's no procfs (macOS, the BSDs), the process doesn't
    exist, or the file isn't readable -- never raises."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    return " ".join(part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part)


def _cmdline_from_ps(pid):
    """Portable half of process_cmdline (task-105): `ps -ww -o command=
    -p <pid>`, the procfs-free way to ask what a pid is on macOS and the
    BSDs. `-ww` disables the width truncation BSD `ps` otherwise applies
    -- a truncated cmdline would silently drop the very markers
    browser.py matches on, turning a live orphan into an unrecognized
    one. Returns None -- never raises -- if `ps` isn't on PATH, the pid
    is gone (non-zero exit), or the output is blank."""
    proc = _run(["ps", "-ww", "-o", "command=", "-p", str(pid)], timeout=5)
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def process_cmdline(pid):
    """Best-effort: the cmdline of a running process, or None if it
    doesn't exist, isn't readable, or isn't running. Used by browser.py's
    boot sweep to confirm a registry entry's pid is *still* the `backlog
    browser` process it recorded -- never kill on pid alone, since pids
    get reused.

    Reads /proc first (_cmdline_from_procfs) and falls back to `ps`
    (_cmdline_from_ps) only when that produced nothing, so Linux keeps
    its plain-file-read fast path while macOS/BSD -- where there is no
    /proc at all, and this used to return None for *every* pid, quietly
    disabling browser.sweep_orphaned_browsers() -- gets a real answer.
    The extra `ps` only ever runs on a call that was already about to
    return None.

    Returning None when neither method can identify the pid is what
    keeps the sweep fail-closed: browser._kill_if_still_matches refuses
    to signal a pid it cannot confirm."""
    return _cmdline_from_procfs(pid) or _cmdline_from_ps(pid)


def kill_process(pid, sig=signal.SIGTERM):
    """Best-effort: sends `sig` to `pid`. Returns True if the signal was
    delivered, False if the process was already gone or couldn't be
    signaled (ProcessLookupError / PermissionError) -- either way,
    there's nothing more this can do about it. Never raises."""
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Agent lifecycle events (task-37): hook/notify injection + in-memory
# state, keyed by (project, taskId) -- never by tmux session name, so it
# survives a session being killed and re-spawned/resumed. See
# spawn._inject_agent_hooks for how a claude-family or codex agent gets
# pointed at this, and centrale_notify.py for the helper both of those
# ultimately shell out to.
# ---------------------------------------------------------------------------

def notify_script_path():
    """Absolute path to centrale_notify.py, the small hook/notify helper
    shipped at the repo root -- referenced by absolute path from the
    generated hooks settings file and the codex notify override, never
    copied anywhere else. Every spawn/resume gets this path."""
    return os.path.join(BASE_DIR, "centrale_notify.py")


def hooks_settings_path():
    """~/.cache/centrale/hooks-settings.json, or
    $XDG_CACHE_HOME/centrale/... if that's set -- the same XDG-aware
    lookup browser.py's registry_path() uses for browsers.json, so the
    generated Claude Code hooks settings file (see
    ensure_hooks_settings_file) lives under the centrale cache dir too,
    never the user's ~/.claude config or the target repo. A function,
    not a module-level constant, so tests can monkeypatch it to a
    throwaway path without ever touching a real one."""
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache_home, "centrale", HOOKS_SETTINGS_FILENAME)


def _hooks_settings_hook(state):
    command = shlex.join(["python3", notify_script_path(), state])
    return {"hooks": [{"type": "command", "command": command}]}


def hooks_settings_payload():
    """The Claude Code hooks settings JSON: UserPromptSubmit/PreToolUse
    -> working, Notification -> waiting, Stop -> finished. Agent-generic
    and identical for every claude-family spawn -- identity travels via
    CENTRALE_EVENT_URL in the session's own environment (see
    spawn.event_url), not anything encoded here, so this one generated
    file is shared across every spawn/resume rather than written fresh
    per task."""
    return {
        "hooks": {
            "UserPromptSubmit": [_hooks_settings_hook("working")],
            "PreToolUse": [_hooks_settings_hook("working")],
            "Notification": [_hooks_settings_hook("waiting")],
            "Stop": [_hooks_settings_hook("finished")],
        }
    }


# task-42's original codex counterpart to hooks_settings_payload() above
# wrote this same shape into <worktree>/.codex/hooks.json as a file.
# task-44 replaced that: codex resolves its *project* config layer
# through a linked worktree's `.git` FILE to the MAIN repo root, so a
# worktree-local hooks.json is never discovered in exactly the
# environment centrale spawns into (see spawn._inject_codex_hooks for the
# full empirical writeup and "Agent lifecycle events" in docs/agents.md). The fix -- proven
# live -- is to pass the same four hook definitions as inline -c config
# overrides on the codex argv itself instead, which never touch the
# filesystem at all, so there is no config layer left for codex to
# resolve wrong. PermissionRequest is codex's waiting-state equivalent
# of claude's Notification point; codex's hooks engine has no direct
# "Notification".
def _codex_hooks_override(point, state):
    """One -c override argv pair, e.g. ["-c",
    'hooks.UserPromptSubmit=[{hooks=[{type="command",command="python3
    <abs>/centrale_notify.py working"}]}]'] -- a TOML inline-array-of-
    inline-table value matching codex's hooks.json schema one level
    deep. Reuses _hooks_settings_hook's own shlex.join'd command string
    (the exact same command every claude-family and the old codex-file
    path have always used) rather than rebuilding it, so a spaced path
    is still handled correctly. The command is TOML-quoted via
    json.dumps: JSON's basic-string escaping (\\, ", control chars) is a
    valid subset of TOML's basic-string escaping, the same trick the
    notify override's own json.dumps(notify_argv) value already relies
    on for its array."""
    command = _hooks_settings_hook(state)["hooks"][0]["command"]
    quoted_command = json.dumps(command)
    return ["-c", f'hooks.{point}=[{{hooks=[{{type="command",command={quoted_command}}}]}}]']


def codex_hooks_overrides():
    """The four -c inline config overrides that give codex full
    UserPromptSubmit/PreToolUse -> working, PermissionRequest -> waiting,
    Stop -> finished lifecycle fidelity (task-44), in place of a
    hooks.json file. Flat argv list (four ["-c", "hooks.<Point>=..."]
    pairs, 8 elements total) meant to be spliced directly into a codex
    spawn's argv -- see spawn._inject_codex_hooks for the gating (only
    once server.probe_codex_hook_trust says this binary supports
    --dangerously-bypass-hook-trust) and the full linked-worktree
    config-layer finding behind why these replaced a file write."""
    overrides = []
    for point, state in (
        ("UserPromptSubmit", "working"),
        ("PreToolUse", "working"),
        ("PermissionRequest", "waiting"),
        ("Stop", "finished"),
    ):
        overrides += _codex_hooks_override(point, state)
    return overrides


def ensure_hooks_settings_file():
    """Idempotently (re)writes the generated hooks settings file (see
    hooks_settings_path/hooks_settings_payload) and returns its path.
    Atomic write (temp file + os.replace), the same style as settings.py's
    projects.json rewrite -- regenerated on every claude-family spawn/
    resume (see spawn._inject_agent_hooks), which is cheap and also
    self-healing if the cache dir was ever cleared or the file hand-
    edited. Raises OSError if the cache dir can't be created or written;
    callers treat that as best-effort and never let it block a spawn."""
    path = hooks_settings_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".hooks-settings-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(hooks_settings_payload(), f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


# In-memory agent-event store: ephemeral runtime cache, consistent with
# the thin-veneer rule -- after a server restart this is empty again and
# every task reads back "unknown" until its agent's hooks/notify fire
# again. Keyed on the uppercased task id (see _agent_event_key) so a
# lookup from /api/board (task["id"], whatever case backlog stores it as)
# and one from /api/sessions (parsed lowercase out of a tmux session
# name -- see _parse_session_project_and_task) always agree.
_agent_events = {}
_agent_events_lock = threading.Lock()


def _agent_event_key(project, task_id):
    return (project, str(task_id).upper())


def record_agent_event(project, task_id, state, agent_kind=None):
    """Records an agent lifecycle event for (project, taskId). Raises
    ValueError for a state not in AGENT_STATES or a non-null agent kind
    not in AGENT_KINDS -- Handler._handle_agent_event turns that into a
    4xx JSON error, never a traceback.

    Agent kind is optional so existing custom agents that POST to the
    original project/task URL keep working unchanged. When omitted, a
    previously known kind is retained; every built-in spawn/resume puts
    it in CENTRALE_EVENT_URL, so any subsequent event can restore both
    state and kind after a server restart without persistent storage.
    """
    if state not in AGENT_STATES:
        raise ValueError(f"invalid state: {state!r}")
    if agent_kind is not None and agent_kind not in AGENT_KINDS:
        raise ValueError(f"invalid agent kind: {agent_kind!r}")
    with _agent_events_lock:
        key = _agent_event_key(project, task_id)
        previous = _agent_events.get(key) or {}
        _agent_events[key] = {
            "state": state,
            "agentKind": agent_kind or previous.get("agentKind"),
            "lastEventAt": time.time(),
        }


def get_agent_lifecycle(project, task_id):
    """Return public ``agentState``/``agentKind`` for a task.

    A raw codex ``finished`` event becomes ``idle``: TASK-58 proved the
    same event stream occurs when codex is genuinely done and when it
    ends a turn on a plain chat/design-approval question. Claude retains
    its trustworthy ``finished`` state because its Notification hook can
    separately report waiting. Unknown/custom kind preserves the legacy
    raw-state behavior for backward compatibility.
    """
    with _agent_events_lock:
        entry = _agent_events.get(_agent_event_key(project, task_id))
    if not entry:
        return {"agentState": "unknown", "agentKind": "unknown"}
    agent_kind = entry.get("agentKind") or "unknown"
    state = entry["state"]
    if agent_kind == "codex" and state == "finished":
        state = "idle"
    return {"agentState": state, "agentKind": agent_kind}


def get_agent_state(project, task_id):
    """The public last-known agentState; see get_agent_lifecycle."""
    return get_agent_lifecycle(project, task_id)["agentState"]


def get_agent_kind(project, task_id):
    """The last-known built-in agent kind, or ``unknown``."""
    return get_agent_lifecycle(project, task_id)["agentKind"]


def clear_agent_event(project, task_id):
    """Forget the last event produced by an earlier session for this
    task. Spawn/resume call this immediately before creating the next tmux
    session, so events emitted by the new process can only arrive after the
    old entry is gone and are never cleared out from under it."""
    with _agent_events_lock:
        _agent_events.pop(_agent_event_key(project, task_id), None)


def _reset_agent_events():
    """Test helper: clear the in-memory agent-event store (mirrors
    _reset_board_cache)."""
    with _agent_events_lock:
        _agent_events.clear()


# ---------------------------------------------------------------------------
# Lifecycle serialization (task-121)
# ---------------------------------------------------------------------------
#
# Centrale is a ThreadingHTTPServer, so two lifecycle requests for the
# SAME project+task can otherwise interleave their git operations: a
# discard removing a worktree and force-deleting a branch while a merge
# is running its gates against that same branch, or a re-spawn recreating
# the worktree a discard is halfway through taking away. Every route that
# creates or destroys a task's worktree or branch -- /api/spawn,
# /api/resume, /api/cleanup-branch, /api/discard-attempt,
# /api/abandon-worktree and harvest_branch -- runs under the one lock
# this returns for its (project, task).
#
# One lock PER task, not one global one: two tasks share no worktree, no
# branch and no tag, so a slow merge of one must not stall a discard of
# another. The only cross-task serialization is harvest's own global
# _harvest_lock, and the lock ORDER is fixed to keep that safe:
# harvest_branch takes _harvest_lock and then this one; nothing that
# holds this one ever asks for _harvest_lock. So there is no cycle.
#
# Read-only routes (GET /api/board, GET /api/discard-preview) take
# nothing: they mutate nothing, and a preview that blocked behind a
# running merge would be a worse answer than a fresh unlocked one -- the
# destructive POST re-measures under the lock anyway and refuses if the
# repository moved (see _refuse_stale_state_or_none).
#
# The registry only ever grows, by one small entry per (project, task)
# a lifecycle route has touched -- bounded by the board, and dropping
# entries on release would race with a thread about to wait on one.

_lifecycle_locks = {}
_lifecycle_locks_guard = threading.Lock()


def task_lifecycle_lock(project_name, task_id):
    """The lock serializing lifecycle operations on one project+task.

    Keyed case-insensitively on the task id for the same reason
    spawn.branch_name lowercases it: TASK-9 and task-9 name one branch
    and one worktree, so they must not get two locks. Identity is
    stringified rather than validated -- this is called before some
    handlers have finished validating, and an unhashable body value
    should not be able to crash the lookup."""
    key = (str(project_name), str(task_id).lower())
    with _lifecycle_locks_guard:
        lock = _lifecycle_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _lifecycle_locks[key] = lock
    return lock


# ---------------------------------------------------------------------------
# Board aggregation
# ---------------------------------------------------------------------------

_board_cache = {"time": 0.0, "data": None}
_board_lock = threading.Lock()


def _reset_board_cache():
    """Test helper: clear the in-memory board cache."""
    with _board_lock:
        _board_cache["time"] = 0.0
        _board_cache["data"] = None


def _read_statuses(repo_path):
    """Best-effort parse of `statuses: [...]` from backlog/config.yml.
    Returns None if the file or line can't be found/parsed."""
    config_path = os.path.join(repo_path, "backlog", "config.yml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None

    match = _STATUSES_LINE_RE.search(content)
    if not match:
        return None
    try:
        statuses = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if isinstance(statuses, list) and all(isinstance(s, str) for s in statuses):
        return statuses
    return None


def _load_project_milestones(repo_path):
    """Best-effort ``{milestone id: title}`` for one repo, read through the
    backlog CLI.

    task-91: backlog's task JSON carries a milestone's ID ("m-0") and never
    its title, so the board could only ever label a milestone "m-0". The
    title lives in ``backlog milestone list`` -- which, alone among the
    read commands this app depends on, has no ``--json``. Parsing its
    ``--plain`` output (see _MILESTONE_LINE_RE) is the one place Centrale
    reads a plain CLI rendering rather than JSON; it is still the CLI, so
    the veneer rule -- Backlog.md state is only ever read through backlog
    itself, never by reading backlog/*.md directly -- holds.

    ``--show-completed`` is passed so a task still sitting under a finished
    milestone gets a title too; without it that section collapses to a
    "(collapsed, ...)" placeholder.

    Never raises. A repo with no milestones, an older/newer CLI whose plain
    output this doesn't recognise, or a failing call all yield ``{}`` and
    the board falls back to labelling by id, exactly as it did before.
    """
    proc = run_backlog_raw(
        ["milestone", "list", "--plain", "--show-completed"], cwd=repo_path
    )
    if proc.returncode != 0:
        return {}
    titles = {}
    for line in (proc.stdout or "").splitlines():
        match = _MILESTONE_LINE_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        if title:
            titles[match.group(1)] = title
    return titles


def _worktree_has_uncommitted_changes(config, project_name, task_id):
    """True if task_id's worktree exists and `git status --porcelain`
    reports anything in it -- the "interrupted" signal (see GET
    /api/resume): an agent's session died mid-work, leaving committed-
    but-unmerged progress plus uncommitted changes behind. A missing
    worktree, or any git failure, is treated as "not dirty" (false),
    same fail-open spirit as the rest of board aggregation -- this is a
    display hint, not a safety gate (harvest.py's own gate 3 is the
    actual safety check at merge time)."""
    import spawn  # local import: avoids a circular import at module load

    wt_dir = spawn.worktree_dir(config, project_name, task_id)
    if not os.path.isdir(wt_dir):
        return False
    proc = run_git(["status", "--porcelain"], cwd=wt_dir)
    if proc.returncode != 0:
        return False
    return bool((proc.stdout or "").strip())


def _branch_is_fully_merged(project_path, task_id):
    """True iff task_id's task/<id> branch tip is an ancestor of the
    project's current base branch (spawn.current_branch). Ancestry ALONE
    cannot tell "was merged" apart from "never produced anything" -- a
    freshly spawned branch's only commit is the claim commit, which
    already exists on main too, so it's trivially an ancestor the
    instant it's created (the TASK-2/TASK-3 finding in another repo,
    task-45).
    Callers that need the real "already merged" decision must use
    _branch_already_merged, not this function directly -- this is kept
    as the pure ancestry primitive it wraps.

    Checked via `git merge-base --is-ancestor branch base_branch`
    (returncode 0 = ancestor). Fail-safe like
    _worktree_has_uncommitted_changes: any git failure (missing
    branch/ref, detached HEAD, ...) reads as "not an ancestor"
    (returncode 1 or anything else both read as False)."""
    import spawn  # local import: avoids a circular import at module load

    branch = spawn.branch_name(task_id)
    base_branch = spawn.current_branch(project_path)
    proc = run_git(["merge-base", "--is-ancestor", branch, base_branch], cwd=project_path)
    return proc.returncode == 0


def _branch_already_merged(project_path, task_id, task_status):
    """The real "already merged" decision (task-45): the branch tip must
    be an ancestor of the base branch AND the main-side task status must
    be Done. A genuine out-of-band merge (the TASK-1 incident in another
    repo that this whole flag exists for) propagates Done to main; an
    interrupted or still-being-worked branch -- including one with zero
    commits of its own, just the claim commit (that same repo's
    TASK-2/TASK-3) -- leaves main at
    To Do/In Progress and keeps its normal Merge/interrupted-Resume
    treatment instead. An out-of-band merge that forgot to set Done
    falls back to the old Merge button, whose click just hits the
    already-merged no-op -- acceptable, per task-45's own spec.

    task_status is checked first (free -- callers already have it from
    backlog data) so the git ancestry call is skipped entirely for any
    task that isn't Done, same cost-discipline spirit as
    worktreeDirty/alreadyMerged only running for branch-bearing tasks.
    Compared case-insensitively (str(...).strip().lower() == "done"),
    matching spawn._check_not_done's own Done comparison -- a repo with
    custom-cased statuses must not make the two features disagree about
    what "Done" means.

    Reused by both board aggregation (the alreadyMerged flag) and POST
    /api/cleanup-branch's own server-side re-verification, so the two
    can never disagree about what "already merged" means."""
    if str(task_status or "").strip().lower() != "done":
        return False
    return _branch_is_fully_merged(project_path, task_id)


def _read_main_task_status(repo_path, task_id):
    """Best-effort read of task_id's status from the project's main
    checkout (repo_path -- never a worktree) via `backlog task view
    --json`, for POST /api/cleanup-branch's server-side re-verification
    (task-45). Returns None on any failure (backlog CLI error, missing
    task, malformed response) -- _branch_already_merged treats anything
    other than the literal string "Done" as not-Done, so a failed read
    here fails safe into a 409 refusal, never a false "already merged"."""
    try:
        data = run_backlog(["task", "view", task_id, "--json"], cwd=repo_path)
    except BacklogError:
        return None
    task = data.get("task") if isinstance(data, dict) else None
    if not isinstance(task, dict):
        return None
    return task.get("status")


def _task_branch_exists(repo_path, branch):
    """Cheap single-ref existence check for a task/<id> branch, used by
    POST /api/cleanup-branch to tell "nothing to delete, already gone"
    apart from a genuine not-yet-merged refusal. Mirrors harvest.py's
    private _branch_exists of the same shape, kept separate rather than
    shared per this codebase's module-internal-helper convention (see
    harvest._branch_exists' own docstring)."""
    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path)
    return proc.returncode == 0


def _task_view_or_none(cwd, task_id):
    """Best-effort `backlog task view --json` from `cwd`. Returns the
    raw schemaVersion-1 envelope, or None for any failure at all (a
    backlog CLI error, a missing task, an unexpected shape). Every
    branch-side read below is decoration on top of an already-valid
    main-checkout response, so None simply means "no branchTask" --
    never a failed request."""
    try:
        data = run_backlog(["task", "view", task_id, "--json"], cwd=cwd)
    except BacklogError:
        return None
    if isinstance(data, dict) and data.get("schemaVersion") == 1:
        return data
    return None


def _branch_task_view(config, project, task_id):
    """The task as its own `task/<id>` branch has it -- GET /api/task's
    "branchTask" -- read from wherever that branch actually lives
    (task-79), or None when there's nothing to read.

    An agent's Done status, checked ACs and implementation notes are
    committed only on that branch until it's merged, so the main
    checkout's copy (the response's top-level "task") is stale for the
    entire life of a spawn. Where the fresh copy lives depends on
    spawn.checkout_state's kind:

      centrale  -- the Centrale-managed worktree: read it directly,
                   uncommitted edits included (its live progress).
      external  -- the branch was adopted into a foreign worktree
                   (task-70): read that checkout the same way. The path
                   comes from `git worktree list --porcelain`, never
                   from anything user-supplied.
      none      -- a parked branch, checked out nowhere: read its
                   committed state through a detached snapshot, the
                   same one harvest's gate 2 uses (spawn.detached_snapshot).

    Cost discipline, since this runs on every drawer open: an existing
    Centrale worktree short-circuits before any git call at all, and a
    task with no branch costs exactly one `git rev-parse --verify` and
    nothing else -- no worktree listing, no snapshot, no second backlog
    call. A "centrale" kind whose directory is gone (a hand-deleted
    worktree git still lists) reads as nothing, exactly as before."""
    import spawn  # local import: avoids a circular import at module load

    wt_dir = spawn.worktree_dir(config, project["name"], task_id)
    if os.path.isdir(wt_dir):
        return _task_view_or_none(wt_dir, task_id)

    repo_path = project["path"]
    branch = spawn.branch_name(task_id)
    if not _task_branch_exists(repo_path, branch):
        return None

    state = spawn.checkout_state(config, project, task_id)
    kind = state.get("kind")
    if kind == "external" and state.get("path"):
        return _task_view_or_none(state["path"], task_id)
    if kind == "none":
        with spawn.detached_snapshot(repo_path, branch) as (snapshot, error):
            if error is not None:
                return None
            return _task_view_or_none(snapshot, task_id)
    return None


def _dirty_paths_from_status(porcelain_output):
    """All paths named in `git status --porcelain` output, dequoted (see
    dequote_git_path) and deduped in first-seen order. Used by POST
    /api/cleanup-branch to report exactly which uncommitted/untracked
    files a forced worktree removal is about to discard -- unlike
    harvest._parse_dirty_paths, this doesn't need the staged/other
    split (that split is about whether a merge would conflict; a forced
    removal discards everything uncommitted regardless)."""
    paths = []
    seen = set()
    for line in porcelain_output.splitlines():
        if not line or len(line) < 4:
            continue
        raw = line[3:]
        if " -> " in raw:
            old_path, _, new_path = raw.partition(" -> ")
            candidates = [old_path, new_path]
        else:
            candidates = [raw]
        for candidate in candidates:
            path = dequote_git_path(candidate)
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _branch_tip_sha(repo_path, branch):
    """The full 40-char tip SHA of `branch`, or None when git can't say.
    The one fact a discarded attempt has to be recoverable from
    (task-119) -- see recovery_tag_name for why a SHA alone is not
    enough on its own."""
    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo_path)
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _branch_commit_count(repo_path, branch, base_branch):
    """How many commits `branch` carries that `base_branch` does not --
    `git rev-list --count <base>..<branch>` -- or None when git can't
    say. This is the number the discard confirm names ("3 commits and 4
    uncommitted files"), so None is deliberately NOT coerced to 0: a
    count Centrale could not measure must stop the confirm from arming
    rather than understate what is about to be destroyed (task-119).

    Counted against the base rather than as the branch's whole history
    for the same reason task-45 exists: a spawn branch's first commit is
    the claim commit, which is already on the base, and "1 commit" for a
    branch that produced nothing would be a lie in the other direction."""
    proc = run_git(["rev-list", "--count", f"{base_branch}..{branch}"], cwd=repo_path)
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def recovery_tag_name(task_id, now=None):
    """The lightweight tag a discard leaves behind at the branch tip:
    `abandoned/task-<id>-<YYYYMMDD-HHMMSS UTC>`.

    Deleting an unmerged branch is the only irreversible act in the
    product, so task-119 asked whether a tag was worth its clutter. It
    is, and the reason is measured rather than aesthetic: after
    `git worktree remove --force` followed by `git branch -D`, NO reflog
    anywhere still references the branch tip -- the worktree's own
    reflog (`.git/worktrees/<n>/logs/HEAD`) is deleted with the
    worktree, and the branch's (`.git/logs/refs/heads/task/<id>`) with
    the branch. The commits are unreachable immediately, and a single
    `git gc --prune=now` (or any gc once the default grace elapses)
    destroys them. Without a tag, the "recover with `git branch ...
    <sha>`" line the response hands the user is a promise git does not
    keep.

    So the tag is created BEFORE either removal, and a failure to
    create it aborts the discard with nothing removed: no anchor, no
    delete. The timestamp (UTC, second resolution) keeps repeated
    discards of the same task from colliding, and the shared
    `abandoned/` prefix keeps them together in `git tag` and trivial to
    sweep (`git tag -d abandoned/task-119-...`) once the user is sure."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    return f"abandoned/{task_id.lower()}-{stamp}"


def recovery_command(branch, sha):
    """The exact copy-pasteable line that puts a discarded branch back."""
    return f"git branch {branch} {sha}"


# The inverse of recovery_tag_name(): the tag NAME is the record of when
# a discard happened. Deliberately not `%(creatordate)` -- these are
# lightweight tags, so git would report the tagged COMMIT's date, which
# is when the discarded work was last committed and not when the user
# threw it away.
_RECOVERY_TAG_RE = re.compile(
    r"^abandoned/([a-z]+)-([0-9]+(?:\.[0-9]+)*)-([0-9]{8})-([0-9]{6})$")


def parse_recovery_tag(tag):
    """`("TASK-9", "2026-09-04T16:30:12Z")` from
    `abandoned/task-9-20260904-163012`, or None for anything that isn't
    one of our recovery tags -- a hand-made `abandoned/wip`, a tag whose
    stamp has been edited, anything under the prefix we didn't write."""
    m = _RECOVERY_TAG_RE.match((tag or "").strip())
    if not m:
        return None
    prefix, number, day, clock = m.groups()
    when = (f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
            f"T{clock[0:2]}:{clock[2:4]}:{clock[4:6]}Z")
    return f"{prefix.upper()}-{number}", when


def latest_discard_times(repo_path):
    """`{task id: ISO 8601 UTC}` for the NEWEST discard of each task in
    this repo, from one `git for-each-ref` over `refs/tags/abandoned/`
    (never one call per task -- the same discipline, and the same shape,
    as harvest.list_task_branches feeding `hasSpawnBranch`).

    Derived, never stored: the tags ARE the record, so deleting one
    (`git tag -d abandoned/task-9-...`, which the docs tell the user to
    do once they're sure) takes the fact away with it. Never raises; a
    repo git can't read, or one with no such tags, just yields nothing,
    which reads downstream as "no discard here" -- exactly what a
    repository that has never had one looks like."""
    proc = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/tags/abandoned"],
        cwd=repo_path)
    if proc.returncode != 0:
        return {}
    newest = {}
    for line in (proc.stdout or "").splitlines():
        parsed = parse_recovery_tag(line)
        if parsed is None:
            continue
        task_id, when = parsed
        # Fixed-width ISO 8601 UTC: lexicographic order IS chronological
        # order, so repeated discards of one task resolve to the last.
        if when > newest.get(task_id, ""):
            newest[task_id] = when
    return newest


def _load_project_board(config, project):
    """Fetch and merge task list + ready list for a single project. Never
    raises: failures are captured as an "error" field on the result."""
    import harvest  # local import: avoids a circular import at module load
    import spawn  # local import: avoids a circular import at module load

    name = project.get("name")
    path = project.get("path") or ""
    result = {
        "name": name,
        "path": path,
        "error": None,
        "statuses": list(DEFAULT_STATUSES),
        "tasks": [],
    }

    if not path or not os.path.isdir(path):
        result["error"] = f"project path not found: {path or '(empty)'}"
        return result

    try:
        list_data = run_backlog(["task", "list", "--json"], cwd=path)
        ready_data = run_backlog(["task", "list", "--ready", "--json"], cwd=path)
    except BacklogError as exc:
        result["error"] = str(exc)
        return result

    if not isinstance(list_data, dict) or list_data.get("schemaVersion") != 1:
        result["error"] = "unsupported or missing schemaVersion from `task list --json`"
        return result
    if not isinstance(ready_data, dict) or ready_data.get("schemaVersion") != 1:
        result["error"] = "unsupported or missing schemaVersion from `task list --ready --json`"
        return result

    tasks = list_data.get("tasks") or []
    ready_ids = {t.get("id") for t in (ready_data.get("tasks") or [])}

    # One `git for-each-ref` call for the whole project (never one per
    # task) to flag which tasks have an unmerged task/<id> branch. main's
    # status/AC/notes for such a task can be stale (see harvest.py's gate
    # 2 and GET /api/task's "branchTask") -- the frontend uses this flag
    # to show the Merge button and an "unmerged branch" indicator without
    # waiting on main to agree the task is Done.
    spawn_branch_ids = {
        harvest.task_id_from_branch(b) for b in harvest.list_task_branches(path)
    }
    spawn_branch_ids.discard(None)

    # task-70: one `git worktree list --porcelain` per project (only when
    # there's a branch to classify at all) tells, for each task/<id>
    # branch, whether it's checked out in Centrale's own worktree, in a
    # foreign one (.worktrees/, /tmp, ... -- the branch was adopted and
    # work may be continuing outside Centrale's view), or nowhere (a
    # "parked" branch). The frontend renders the three differently; see
    # spawn.checkout_state for the shape and the per-kind meaning.
    checkouts = spawn.branch_checkouts(path) if spawn_branch_ids else {}

    # task-134: one `git for-each-ref refs/tags/abandoned/` per project
    # (never one per task) says when each task's most recent attempt was
    # discarded. Same derive-don't-remember rule as everything else on
    # the board: the recovery tags a discard leaves behind are the whole
    # record, read fresh on every load. The frontend needs it because a
    # task that is In Progress with no branch looks exactly like one
    # claimed by a worker Centrale can't see -- unless a tag says the
    # user discarded it themselves, which is what the spawn confirm then
    # names instead.
    discard_times = latest_discard_times(path)

    # task-91: milestone ids are assigned per repo and sequentially, so
    # every repo has an "m-0" and the id alone says nothing about which
    # milestone -- or whose. Resolve id -> title ONCE per project here
    # (never per task: the lookup below is a dict hit) and skip the call
    # entirely for a project where no task carries a milestone, which is
    # every project on a board that doesn't use them.
    milestone_titles = (
        _load_project_milestones(path)
        if any(str(t.get("milestone") or "").strip() for t in tasks)
        else {}
    )

    merged_tasks = []
    observed_statuses = []
    for task in tasks:
        merged = dict(task)
        task_id = merged.get("id")
        merged["ready"] = task_id in ready_ids
        has_branch = task_id in spawn_branch_ids
        merged["hasSpawnBranch"] = has_branch
        merged.update(get_agent_lifecycle(name, task_id))
        # Only checked for tasks that actually have a branch (one git
        # call per such task, not per task overall): distinguishes a
        # clean branch awaiting merge from an "interrupted" one -- an
        # agent's session died mid-work, leaving uncommitted changes
        # behind in its worktree. See GET /api/resume and the drawer's
        # "Resume agent" action.
        merged["worktreeDirty"] = has_branch and _worktree_has_uncommitted_changes(config, name, task_id)
        # task-70: null for a task without a branch; otherwise where its
        # branch is checked out (see `checkouts` above). Only the external
        # kind costs an extra git call (the branch's last-commit age).
        merged["branchCheckout"] = (
            spawn.checkout_state(config, project, task_id, checkouts=checkouts) if has_branch else None
        )
        # task-43/task-45: a branch fully merged into the base branch AND
        # whose main-side task is Done -- whether by Centrale's own
        # harvest or entirely out-of-band (the real TASK-1 incident in
        # another repo this flag exists for) -- makes Merge the wrong offer;
        # the frontend swaps it for "Merged -- clean up" instead. The
        # Done check (task-45) rules out a freshly spawned, still-empty
        # branch (just the claim commit, already an ancestor of main the
        # instant it's created -- that repo's TASK-2/TASK-3 finding) from
        # ever reading as merged while it's still In Progress. The
        # status is free (already in `merged` from the backlog list),
        # so this stays the same one-call-per-branch-bearing-task cost
        # discipline as worktreeDirty just above -- the git ancestry
        # call itself is skipped entirely unless status is Done.
        merged["alreadyMerged"] = has_branch and _branch_already_merged(path, task_id, merged.get("status"))
        # task-91: the id in "milestone" stays exactly as backlog gave it
        # -- it is what the frontend filters on, and it survives a
        # rename. "milestoneTitle" is display only, and is None whenever
        # the title can't be resolved (no milestone, or an unreadable
        # milestone list), which the frontend renders as the bare id.
        milestone_id = str(merged.get("milestone") or "").strip()
        merged["milestoneTitle"] = milestone_titles.get(milestone_id) or None
        # task-134: when this task's newest `abandoned/task-<id>-<stamp>`
        # recovery tag says its last attempt was discarded, or None for
        # a task that has never had one (and for a repo whose tag
        # listing failed -- see latest_discard_times).
        merged["lastDiscardedAt"] = discard_times.get(task_id)
        merged_tasks.append(merged)
        status = merged.get("status")
        if status and status not in observed_statuses:
            observed_statuses.append(status)

    result["tasks"] = merged_tasks

    statuses = _read_statuses(path)
    if statuses is None:
        statuses = list(DEFAULT_STATUSES)
    else:
        statuses = list(statuses)
    for status in observed_statuses:
        if status not in statuses:
            statuses.append(status)
    result["statuses"] = statuses

    return result


def get_board(config, force=False):
    """Aggregate the board across all configured projects, concurrently.
    Results are cached in-memory for CACHE_TTL_SECONDS unless force=True."""
    now = time.time()
    if not force:
        with _board_lock:
            cached = _board_cache["data"]
            if cached is not None and (now - _board_cache["time"]) < CACHE_TTL_SECONDS:
                return cached

    projects = config.get("projects") or []
    results_by_name = {}
    if projects:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(projects)) as pool:
            future_to_name = {
                pool.submit(_load_project_board, config, project): project["name"]
                for project in projects
            }
            for future in future_to_name:
                name = future_to_name[future]
                try:
                    results_by_name[name] = future.result()
                except Exception as exc:  # pragma: no cover - defensive isolation
                    results_by_name[name] = {
                        "name": name,
                        "path": "",
                        "error": str(exc),
                        "statuses": list(DEFAULT_STATUSES),
                        "tasks": [],
                    }

    ordered = [results_by_name[p["name"]] for p in projects]
    board = {"projects": ordered}

    with _board_lock:
        _board_cache["data"] = board
        _board_cache["time"] = now

    return board


# ---------------------------------------------------------------------------
# tmux sessions
# ---------------------------------------------------------------------------

def list_sessions():
    """Return centrale-* tmux sessions (see SESSION_PREFIX) as a list of
    {"name", "created", "attached"} dicts. A tmux server that isn't
    running yields an empty list rather than an error."""
    proc = run_tmux([
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_created}\t#{session_attached}",
    ])
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "no server running" in stderr or "no such file or directory" in stderr:
            return []
        if not (proc.stdout or "").strip():
            # Treat any other empty-output non-zero exit as "no sessions"
            # too, so a missing/unavailable tmux never surfaces as a 500.
            return []
        raise BacklogError(f"tmux list-sessions failed: {(proc.stderr or '').strip()}")

    sessions = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        name, created, attached = parts[0], parts[1], parts[2]
        if not name.startswith(SESSION_PREFIX):
            continue
        sessions.append({
            "name": name,
            "created": created,
            "attached": attached == "1",
        })
    return sessions


TOUCHED_FILES_CAP = 20


def _parse_session_project_and_task(name, config):
    """Best-effort reverse-map a `centrale-<project>-<encoded-taskid>`
    session name back to (project_dict, lowercased_dotted_task_id), by
    matching against configured project
    names (longest name wins, in case one project name is itself a
    prefix of another) — mirrors the frontend's own parseSessionTask().
    The task-id portion is decoded via spawn.decode_session_task_id
    (task-59: session names embed a tmux-safe "_"-for-"." encoding of a
    dotted subtask id like "task-11.2" -> "task-11_2", see
    spawn.encode_task_id_for_session) so this returns the real dotted
    id, matching what every other caller (worktree_dir, get_agent_state,
    ...) expects — not the raw underscored session-name substring.
    Returns (None, None) if it can't be resolved.

    This also recognizes sessions a pre-task-59 spawn left behind: tmux
    itself silently mangled the "." in those requested names to "_"
    before task-59's fix ever did so on purpose (verified empirically),
    so their actual session names already have the exact shape this
    decodes — no migration needed."""
    import spawn  # local import: avoids a circular import at module load

    if not name.startswith(SESSION_PREFIX):
        return None, None
    rest = name[len(SESSION_PREFIX):]
    candidates = [
        p for p in config.get("projects", [])
        if p.get("name") and rest.startswith(f"{p['name']}-")
    ]
    candidates.sort(key=lambda p: len(p["name"]), reverse=True)
    for project in candidates:
        remainder = rest[len(project["name"]) + 1:]
        if spawn.is_encoded_task_id(remainder):
            return project, spawn.decode_session_task_id(remainder)
    return None, None


def live_sessions_for_project(project_name, config):
    """The names of the live `centrale-*` tmux sessions that belong to
    `project_name`, resolved through the same reverse mapping
    everything else uses (_parse_session_project_and_task, so a project
    name that is a prefix of another one still resolves to the right
    owner). Empty when tmux has no server running at all.

    Public because settings.py's remove-project guard (task-167) asks
    exactly this question -- "is an agent of ours still running in this
    repo" -- and a cross-module caller here goes through a public
    function rather than reaching for the private mapping.
    """
    owned = []
    for session in list_sessions():
        name = session.get("name", "")
        project, _ = _parse_session_project_and_task(name, config)
        if project is not None and project.get("name") == project_name:
            owned.append(name)
    return owned


class PaneCaptureError(Exception):
    """capture_session_pane failed; `.status` is the HTTP status GET
    /api/session-pane should answer with (404 when the session simply
    isn't there, 500 for any other tmux failure)."""

    def __init__(self, message, status=500):
        super().__init__(message)
        self.status = status


def _normalize_pane_lines(raw):
    """Clamp the optional ?lines= query value into 1..MAX_SESSION_PANE_LINES,
    falling back to DEFAULT_SESSION_PANE_LINES for anything unparseable --
    a lenient display knob, not a validation gate."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_PANE_LINES
    return max(1, min(MAX_SESSION_PANE_LINES, n))


def capture_session_pane(name, max_lines=DEFAULT_SESSION_PANE_LINES):
    """Return the last `max_lines` rendered lines of a live tmux session's
    active pane as clean text (task-60). Exactly ONE tmux call --
    `capture-pane -p -t =<name>: -S -<max_lines>` -- with no list-sessions
    preflight, so the drawer's ~2s poll costs one subprocess per tick
    regardless of how many sessions exist. `-p` prints the rendered
    screen grid (tmux has already resolved every escape sequence, so no
    ANSI parsing is needed; trailing spaces are trimmed by tmux itself);
    `-S -N` prepends N lines of scrollback to the visible pane so a short
    pane still fills the requested window. The trailing ":" in the target
    is load-bearing: it makes "=" an exact SESSION-name match (verified
    empirically -- a bare "=name" is read as an exact PANE name and
    fails with "can't find pane"), so "centrale-app-task-1" can never
    resolve to "centrale-app-task-10" the way a prefix match could.

    Raises PaneCaptureError(status=404) when the session doesn't exist or
    no tmux server is running, PaneCaptureError(status=500) otherwise.
    """
    proc = run_tmux(["capture-pane", "-p", "-t", f"={name}:", "-S", f"-{max_lines}"])
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        lowered = stderr.lower()
        if (
            "can't find" in lowered
            or "no server running" in lowered
            or "no such file or directory" in lowered
            or "error connecting" in lowered
        ):
            raise PaneCaptureError(f"no live session {name}", status=404)
        raise PaneCaptureError(f"tmux capture-pane failed: {stderr or 'unknown error'}", status=500)
    lines = (proc.stdout or "").split("\n")
    # tmux pads the visible pane to its full height with blank rows; drop
    # those so the client sees what's actually on screen, then keep the
    # tail. Interior blank lines are real output and stay.
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-max_lines:]


# task-61: when did this process last successfully capture each session's
# pane? Process-local and deliberately NOT persisted (a restart forgets,
# and the reply endpoint then refuses until the drawer has previewed
# again -- the safe direction). Keyed by exact session name; values are
# time.monotonic() stamps. Guarded because the HTTP server is threaded.
_pane_capture_times = {}
_pane_capture_lock = threading.Lock()


def record_pane_capture(name, now=None):
    """Remember that `name`'s pane was just captured (called by GET
    /api/session-pane on success). `now` is injectable for tests."""
    with _pane_capture_lock:
        _pane_capture_times[name] = time.monotonic() if now is None else now


def pane_capture_age(name, now=None):
    """Seconds since this process last captured `name`'s pane, or None if
    it never has (or forgot -- see _reset_pane_capture_times)."""
    with _pane_capture_lock:
        stamp = _pane_capture_times.get(name)
    if stamp is None:
        return None
    return max(0.0, (time.monotonic() if now is None else now) - stamp)


def _reset_pane_capture_times():
    """Test hook: forget every capture (mirrors _reset_agent_events)."""
    with _pane_capture_lock:
        _pane_capture_times.clear()


class SessionInputError(Exception):
    """validate_session_input / send_session_input failed; `.status` is
    the HTTP status POST /api/session-input should answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def validate_session_input(body):
    """Reduce a POST /api/session-input body to exactly one of
    ("text", <str>) or ("key", <str>), or raise SessionInputError(400).

    Text must be a single line of printable characters, 1..
    MAX_SESSION_INPUT_TEXT_CHARS long: no newline (Enter is appended by
    the server, so an embedded one would be a second, unreviewed submit),
    no other control characters (pasted literally, they could reach the
    TUI as Ctrl-<x> input, which is not "typing a reply"). Keys must be
    one of SESSION_INPUT_KEYS -- see the note on that constant for why an
    open-ended key name is not accepted. The addressing fields are
    accepted for the endpoint's JSON-body fallback; every other field is
    rejected (task-77) so an input mode that was removed cannot silently
    remain usable.

    Exactly one of the two: a body carrying both is a 400, not a guess
    about which the user meant."""
    if not isinstance(body, dict):
        raise SessionInputError("request body must be a JSON object")
    allowed_fields = {"text", "key", "project", "taskId", "task"}
    unknown_fields = sorted(
        (field for field in body if field not in allowed_fields), key=str
    )
    if unknown_fields:
        label = "field" if len(unknown_fields) == 1 else "fields"
        names = ", ".join(repr(field) for field in unknown_fields)
        raise SessionInputError(f"unknown request body {label}: {names}")
    has_text = "text" in body
    has_key = "key" in body
    if has_text == has_key:
        raise SessionInputError('body must contain exactly one of "text" or "key"')
    if has_key:
        key = body.get("key")
        if not isinstance(key, str) or key not in SESSION_INPUT_KEYS:
            raise SessionInputError(f"key must be one of {list(SESSION_INPUT_KEYS)!r}")
        return "key", key
    text = body.get("text")
    if not isinstance(text, str):
        raise SessionInputError("text must be a string")
    if not text.strip():
        raise SessionInputError("text must not be empty")
    if len(text) > MAX_SESSION_INPUT_TEXT_CHARS:
        raise SessionInputError(
            f"text must be at most {MAX_SESSION_INPUT_TEXT_CHARS} characters (single-line replies only)"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SessionInputError(
            "text must be a single line with no control characters (multi-line input is not supported)"
        )
    return "text", text


def _new_paste_buffer_name():
    """A fresh, unguessable tmux buffer name for one reply (task-72). Named
    explicitly -- never "the most recent buffer" -- because the tmux server
    is shared: the user's own copy-mode yanks and a second drawer's reply
    can land in the buffer list between our load-buffer and paste-buffer,
    and an unnamed paste-buffer would then paste THEIRS. Its own function
    so tests can pin the exact argv."""
    return f"centrale-reply-{uuid.uuid4().hex}"


def _is_session_gone(stderr_lowered):
    """tmux stderr that means "no such session" / "no tmux server at all"
    (404 territory) rather than a genuine failure (500)."""
    return (
        "can't find" in stderr_lowered
        or "no server running" in stderr_lowered
        or "no such file or directory" in stderr_lowered
        or "error connecting" in stderr_lowered
    )


def send_session_input(name, kind, value):
    """Deliver a validated reply into the live tmux session `name`
    (task-61) through run_tmux. Exact-match target "=<name>:" for the
    same reason capture_session_pane uses it (a bare prefix could reach a
    different task's session).

    A key goes as one `send-keys <keyname>` (task-135), never through the
    load-buffer/paste-buffer path below: bracketed paste is for text, and
    a pasted key name is just the letters E-s-c-a-p-e.

    Text goes by tmux's BRACKETED-PASTE path (task-72), three calls:

      load-buffer -b <buf> -          (the text on stdin, so its length,
                                       a leading dash, ";" or a key-name-
                                       looking word never meet an argv)
      paste-buffer -d -p -b <buf> -t =<name>:
      send-keys -t =<name>: Enter

    NOT `send-keys -l <text>` + Enter, which typed the text as a rapid
    unmarked keystroke burst: agent TUIs guess "this is a paste" from
    inter-keystroke timing, and the Enter that followed within that guess
    window was swallowed into the paste instead of submitting -- the
    user then had to attach and press Enter by hand (reproduced against
    codex 0.151.0; claude 2.1.258 raced the same way, just less often).
    With `-p`, tmux wraps the buffer in the bracketed-paste control codes
    (ESC[200~ ... ESC[201~) whenever the pane's application has requested
    bracketed paste mode -- claude and codex both do, verified by
    recording their startup bytes -- so the TUI sees an explicit paste
    terminator and processes paste-then-Enter sequentially off the pty:
    no heuristic window, nothing to race, no sleeps anywhere. `-d`
    deletes the buffer once pasted (no residue in the user's buffer
    list); a failed paste deletes it explicitly, best effort.

    For an application that never requested bracketed paste, `-p` pastes
    the raw bytes (verified: tmux does NOT skip the paste), i.e. exactly
    the burst `send-keys -l` produced -- no worse than before, and no
    observation-driven Enter retry is layered on top: neither supported
    agent needs one, and a capture-pane check for "text still sitting in
    the composer" cannot tell typed text from Claude Code's dim ghost-text
    prompt suggestions (rendered on the same composer line), so a retry
    would risk submitting something the user never typed.

    Raises SessionInputError(status=404) when the session doesn't exist
    or no tmux server is running, SessionInputError(status=500) on any
    other tmux failure. Returns the list of argv lists that were run."""
    target = f"={name}:"

    def fail(args, proc):
        stderr = (proc.stderr or proc.stdout or "").strip()
        if _is_session_gone(stderr.lower()):
            raise SessionInputError(f"no live session {name}", status=404)
        raise SessionInputError(
            f"tmux {args[0]} failed: {stderr or 'unknown error'}", status=500
        )

    if kind == "key":
        args = ["send-keys", "-t", target, value]
        proc = run_tmux(args)
        if proc.returncode != 0:
            fail(args, proc)
        return [args]

    buf = _new_paste_buffer_name()
    load = ["load-buffer", "-b", buf, "-"]
    paste = ["paste-buffer", "-d", "-p", "-b", buf, "-t", target]
    enter = ["send-keys", "-t", target, "Enter"]
    proc = run_tmux(load, input=value)
    if proc.returncode != 0:
        fail(load, proc)
    proc = run_tmux(paste)
    if proc.returncode != 0:
        # -d never ran, so the buffer is still there: drop it (best
        # effort -- the paste failure is the error worth reporting).
        run_tmux(["delete-buffer", "-b", buf])
        fail(paste, proc)
    proc = run_tmux(enter)
    if proc.returncode != 0:
        fail(enter, proc)
    return [load, paste, enter]


_GIT_QUOTE_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    "\\": "\\", '"': '"',
}


def dequote_git_path(path):
    """Reverses git's C-style path quoting. Every git command that
    prints a path (`status --porcelain`, `diff --name-only`, ...)
    wraps it in double quotes and backslash-escapes it -- \\, ", and
    control characters (tab, newline, ...) always, any byte >= 0x80
    (i.e. non-ASCII) as an octal \\NNN escape -- the instant the path
    contains ANY of those, space included. `core.quotePath` does NOT
    govern this for space/quote/backslash/control characters -- verified
    empirically (a plain space is quoted the same with quotePath=false)
    -- it only controls whether the non-ASCII-byte octal-escaping
    happens at all, so it's not a viable defense on its own and this
    module doesn't rely on it.

    An already-unquoted path (no special characters, e.g. every filename
    without a space or punctuation this rule cares about) passes through
    unchanged. Every consumer of a git path list in this codebase must
    dequote through this one function -- comparing a quoted path
    (`status --porcelain`) against the same file's unquoted form from a
    different command (`diff --name-only` sometimes emits one unquoted
    even when status quotes it) silently never matches, which is
    exactly how a Backlog.md task file (spaced names, always) slipped
    past harvest.py's overlap check once."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    body = path[1:-1]
    out = []
    octal_bytes = bytearray()

    def flush_octal():
        if octal_bytes:
            out.append(octal_bytes.decode("utf-8", "surrogateescape"))
            octal_bytes.clear()

    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if nxt in _GIT_QUOTE_SIMPLE_ESCAPES:
                flush_octal()
                out.append(_GIT_QUOTE_SIMPLE_ESCAPES[nxt])
                i += 2
                continue
            if i + 3 < n and all(ch in "01234567" for ch in body[i + 1:i + 4]):
                octal_bytes.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            # Not a recognized escape -- keep the backslash literally
            # rather than guess; defensive, shouldn't happen against
            # real git output.
            flush_octal()
            out.append(c)
            i += 1
            continue
        flush_octal()
        out.append(c)
        i += 1
    flush_octal()
    return "".join(out)


def _collect_touched_files(wt_dir, cap=TOUCHED_FILES_CAP):
    """Best-effort: files touched in a worktree so far — tracked changes
    vs HEAD (`git diff --name-only HEAD`) plus untracked files (the `??`
    lines of `git status --porcelain`), deduped. Returns
    (files_capped_at_`cap`, total_count), or (None, None) if the worktree
    directory doesn't exist any more (e.g. a killed session that was
    later cleaned up). Every path is dequoted (see dequote_git_path) --
    without it, a Backlog.md task file (always spaced) shows up in the
    sessions panel with stray literal double-quotes around its name."""
    if not os.path.isdir(wt_dir):
        return None, None

    diff_proc = run_git(["diff", "--name-only", "HEAD"], cwd=wt_dir)
    diff_files = (
        [dequote_git_path(line.strip()) for line in (diff_proc.stdout or "").splitlines() if line.strip()]
        if diff_proc.returncode == 0
        else []
    )

    status_proc = run_git(["status", "--porcelain"], cwd=wt_dir)
    untracked = []
    if status_proc.returncode == 0:
        for line in (status_proc.stdout or "").splitlines():
            if line.startswith("??"):
                path = dequote_git_path(line[2:].strip())
                if path:
                    untracked.append(path)

    seen = []
    for f in diff_files + untracked:
        if f not in seen:
            seen.append(f)

    return seen[:cap], len(seen)


def enrich_sessions_with_files(sessions, config):
    """Best-effort: attach the files touched so far and agent lifecycle
    fields (task-37/task-62 -- see get_agent_lifecycle), to each session dict in `sessions`
    (mutated in place), so parallel spawns in the same project can be
    spotted before they collide. A session whose project/task can't be
    resolved from its name is left without any of these keys; one whose
    worktree no longer exists still gets "project"/lifecycle fields (none
    depends on the worktree existing) but not "files" -- rather than
    guessing. Never raises. Returns `sessions`.
    """
    import spawn  # local import: avoids a circular import at module load

    for session in sessions:
        project, task_id_lower = _parse_session_project_and_task(session.get("name", ""), config)
        if project is None:
            continue
        session["project"] = project["name"]
        session.update(get_agent_lifecycle(project["name"], task_id_lower))
        wt_dir = spawn.worktree_dir(config, project["name"], task_id_lower)
        files, total = _collect_touched_files(wt_dir)
        if files is None:
            continue
        session["files"] = files
        session["filesTotal"] = total

    return sessions


# ---------------------------------------------------------------------------
# Cross-site request refusal (task-99)
# ---------------------------------------------------------------------------
#
# Centrale binds 127.0.0.1, so nothing remote can reach it -- but every
# page the user visits in their browser can. Without a check, a form on
# any website could POST /api/settings (which writes the agents map:
# name -> argv) and then POST /api/spawn (which executes that argv):
# arbitrary command execution triggered by visiting a web page while
# Centrale runs. And a GET is not harmless here either: GET /api/harvest
# runs the full merge gate for every task branch, which creates a scratch
# worktree, performs a real merge in it and runs the project's own
# checkCommand -- so a plain cross-site <img src> would make Centrale
# execute the project's test suite. GET /api/session-pane arms
# POST /api/session-input for that session. The reads themselves
# (/api/board, /api/task, /api/settings, /api/session-pane) disclose
# board data, local paths and live pane contents.
#
# Four rules close that. Three of them (1-3) run at the top of BOTH
# do_GET and do_POST before any routing, so a refused request reaches no
# git, tmux, backlog, checkCommand, temporary-worktree or pane-capture
# boundary, and a new endpoint is covered the moment it is added:
#
#   1. Host must be a loopback name at the port this process actually
#      bound: 127.0.0.1, localhost or [::1] at server_address[1]. This
#      is what refuses DNS rebinding (task-122). An attacker who points
#      evil.example at 127.0.0.1 gets a browser that treats their page
#      as SAME-ORIGIN with Centrale -- CORS stops nothing, Origin is
#      theirs and matches, and the response is readable. The one thing
#      that request cannot fake is the Host header: the browser puts the
#      name the user's page was loaded from in it, and evil.example is
#      not on the whitelist. Checking the header against a whitelist is
#      the point -- the value is attacker-controlled, which is exactly
#      why an attacker cannot make it say 127.0.0.1 while their page
#      still counts as same-origin.
#   2. Sec-Fetch-Site, when present, must be same-origin or none. This
#      is what refuses a direct cross-site request: an <img>, <script>,
#      <iframe>, form or window.open aimed at 127.0.0.1 carries a
#      perfectly valid Host, and may carry no Origin and (under
#      <meta name="referrer" content="no-referrer">) no Referer either.
#      Sec-Fetch-Site is a forbidden header name: page JavaScript cannot
#      set it, referrer policy cannot suppress it, and the browser fills
#      it in from where the request was initiated. "none" is a user
#      typing the address or opening a bookmark; "same-origin" is
#      Centrale's own UI. Everything else ("cross-site", "same-site")
#      is somebody else's page. A request with no Sec-Fetch-Site at all
#      is not a browser request -- curl, a script and centrale_notify.py
#      send none -- and passes.
#
#      Rules 1 and 2 are not two spellings of one idea, and neither can
#      be dropped. Driving both attacks through a real Chromium showed
#      why: a rebound page (rule 1's case) sends NO Sec-Fetch-* headers
#      at all, because fetch metadata is only appended for a
#      potentially-trustworthy URL and http://rebind.example is not one
#      -- so rule 2 never sees it. A direct cross-site request (rule 2's
#      case) sends a Host of 127.0.0.1:<our port>, which is genuinely
#      ours -- so rule 1 never sees it. Each rule catches exactly what
#      the other cannot.
#   3. Origin (or, when absent, Referer) must be one of this process's
#      own origins. Defence in depth behind rule 2, and the rule that
#      catches an older browser that sends no Sec-Fetch-Site.
#
# And on POST only:
#
#   4. Content-Type must be application/json. A cross-origin HTML form
#      can only send text/plain, application/x-www-form-urlencoded or
#      multipart/form-data; any other content type turns the request
#      into a CORS preflight, which this server never answers (it emits
#      no CORS headers at all). Without this check a form with
#      enctype="text/plain" delivers a body that json.loads() happily
#      parses -- a simple request, no preflight, no server opt-in.
#
# A request carrying neither Origin nor Referer still passes rule 3:
# curl, a script and centrale_notify.py send neither. That used to be
# the whole hole -- a rebound page produces exactly that shape -- and it
# is rules 1 and 2, not rule 3, that close it. Non-browser callers are
# unaffected by all four: any HTTP client sends a loopback Host of its
# own accord, and none of them send Sec-Fetch-Site.
#
# Deliberately NOT authentication: no accounts, no passwords, no tokens.
# The question this answers is only "could this request have come from
# Centrale's own UI?".

CROSS_SITE_REFUSAL_HINT = (
    "Centrale accepts requests only from its own origin "
    "(see docs/api.md, \"Request requirements\")."
)


# The only names Centrale's own UI can be reached under. A whitelist,
# not a pattern: "any name that resolves to 127.0.0.1" is precisely the
# set a DNS-rebinding attacker can add themselves to.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def loopback_origins(port):
    """The origins Centrale's own UI can be loaded from, given the port
    this process actually listens on. The PORT comes from the bound
    socket rather than from the request, because it is a fact about this
    process; the host NAMES come from LOOPBACK_HOSTNAMES, because a name
    the user's browser was pointed at is attacker-controlled and so has
    to be checked against a whitelist rather than trusted (see
    host_is_own, and the module comment above)."""
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def request_host(header_value):
    """(hostname, port) parsed out of a Host header, or None when the
    header is absent, empty or unparseable. Brackets around an IPv6
    literal are stripped ("[::1]:7420" -> ("::1", 7420)) and an absent
    port means http's default, 80 -- the shape a browser sends when
    Centrale is bound there. A value carrying userinfo ("@") is rejected
    outright: no real Host header has any, and treating one as a host
    would be a parser difference an attacker could aim at."""
    value = (header_value or "").strip()
    if not value or "@" in value:
        return None
    try:
        parts = urllib.parse.urlsplit(f"//{value}")
        hostname, port = parts.hostname, parts.port
    except ValueError:  # a non-numeric or out-of-range port
        return None
    if not hostname:
        return None
    return (hostname.lower(), 80 if port is None else port)


def host_is_own(header_value, port):
    """True when a Host header names this server: a loopback hostname at
    the port this process actually bound. `port` must come from the
    listening socket -- deriving it from the header would let the header
    vouch for itself."""
    parsed = request_host(header_value)
    if parsed is None:
        return False
    hostname, host_port = parsed
    return hostname in LOOPBACK_HOSTNAMES and host_port == port


# Sec-Fetch-Site values that are not another site's page. "none" is a
# request the user initiated themselves (address bar, bookmark, a
# desktop shortcut); "same-origin" is Centrale's own UI. Any other value
# ("cross-site", "same-site") means some other page initiated it.
SAME_SITE_FETCH_VALUES = frozenset({"same-origin", "none"})


def request_origin(header_value):
    """The scheme://host[:port] of an Origin or Referer header value,
    normalized for comparison against loopback_origins(). Returns None
    when the value is not an absolute http(s) URL -- including the
    literal "null" a sandboxed iframe or a file:// page sends, which is
    an origin that is never ours."""
    if not header_value:
        return None
    parsed = urllib.parse.urlsplit(header_value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def json_content_type(header_value):
    """True when a Content-Type header names application/json. The
    parameters after the first ";" (charset=utf-8, most often) are
    ignored -- only the media type decides."""
    base = (header_value or "").split(";", 1)[0].strip().lower()
    return base == "application/json"


# ---------------------------------------------------------------------------
# Static file serving helpers
# ---------------------------------------------------------------------------

def _resolve_static_path(rel_path):
    """Resolve rel_path under STATIC_DIR, refusing to escape it. Returns the
    resolved absolute path, or None if it would escape the static dir."""
    rel_path = (rel_path or "").lstrip("/")
    candidate = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
    base_real = os.path.realpath(STATIC_DIR)
    candidate_real = os.path.realpath(candidate)
    if candidate_real != base_real and not candidate_real.startswith(base_real + os.sep):
        return None
    return candidate_real


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Centrale/1.0"

    # -- logging -------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        # Suppress the default noisy per-request stderr logging.
        pass

    # -- response helpers ------------------------------------------------
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    def _send_error_json(self, status, message):
        self._send_json(status, {"error": message})

    # -- cross-site refusal ----------------------------------------------
    def _own_origins(self):
        """This process's own origins, from the port it is actually
        listening on (server_address, not the config -- a test server
        bound to port 0 and the real one must both be right)."""
        return loopback_origins(self.server.server_address[1])

    def _refuse_untrusted_host(self):
        """Refuse (and return True) a request whose Host header is not a
        loopback name at the port this process bound (task-122).

        This is the DNS-rebinding gate, and it is the reason every other
        header check here is not enough on its own: once evil.example
        resolves to 127.0.0.1, the browser treats the attacker's page as
        SAME-ORIGIN with Centrale -- Origin is theirs and matches, no
        CORS preflight happens, and the response body is readable. The
        Host header is the one part of that request the attacker cannot
        change without also losing same-origin: it says evil.example,
        and evil.example is not a loopback name.

        Runs before routing on every method, so a refused request never
        reaches a subprocess, a temporary worktree or the pane-capture
        bookkeeping."""
        host = self.headers.get("Host")
        if host_is_own(host, self.server.server_address[1]):
            return False

        shown = (host or "").strip()[:120] or "no Host header was sent"
        self._send_error_json(403, (
            f"refused: request for a host this server does not serve ({shown}). "
            + CROSS_SITE_REFUSAL_HINT
        ))
        return True

    def _refuse_cross_site_fetch(self):
        """Refuse (and return True) a request the browser itself labels
        as initiated by another site (task-122).

        Sec-Fetch-Site is a forbidden header name: page JavaScript cannot
        set it and no referrer policy suppresses it, so unlike
        Origin/Referer it is present on every browser request whatever
        the attacker does. That is what closes the direct cross-site
        shape -- an <img>, <script>, <iframe>, form or window.open aimed
        straight at 127.0.0.1, which carries a valid Host and can carry
        neither Origin nor Referer. Requests with no Sec-Fetch-Site are
        not browser requests (curl, a script, centrale_notify.py) and
        pass."""
        value = self.headers.get("Sec-Fetch-Site")
        if value is None:
            return False
        if value.strip().lower() in SAME_SITE_FETCH_VALUES:
            return False

        shown = value.strip()[:120]
        self._send_error_json(403, (
            f"refused: cross-site request (Sec-Fetch-Site: {shown}). "
            + CROSS_SITE_REFUSAL_HINT
        ))
        return True

    def _refuse_untrusted_request(self):
        """The whole trust boundary, in the order that refuses the most
        for the least work, run before routing on every method. See the
        module comment above loopback_origins for what each rule catches
        and why no two of them are redundant."""
        return (
            self._refuse_untrusted_host()
            or self._refuse_cross_site_fetch()
            or self._refuse_cross_site()
        )

    def _refuse_cross_site(self):
        """Refuse (and return True) when this request could not have come
        from Centrale's own UI: it carries an Origin -- or, absent that, a
        Referer -- that is not one of this server's own origins. Neither
        header present is allowed; see the module comment above
        loopback_origins for why."""
        header = "Origin"
        value = self.headers.get("Origin")
        if value is None:
            header = "Referer"
            value = self.headers.get("Referer")
        if value is None:
            return False

        origin = request_origin(value)
        if origin in self._own_origins():
            return False

        # The header is echoed back so the refusal is diagnosable, but
        # it is attacker-controlled and unbounded -- truncate it.
        shown = value.strip()[:120]
        self._send_error_json(403, (
            f"refused: cross-site request ({header}: {shown}). "
            + CROSS_SITE_REFUSAL_HINT
        ))
        return True

    def _refuse_non_json_content_type(self):
        """Refuse (and return True) any POST whose body is not declared
        application/json -- the content types a cross-origin HTML form
        can produce (text/plain, form-urlencoded, multipart) among
        them."""
        value = self.headers.get("Content-Type")
        if json_content_type(value):
            return False

        got = (value or "").strip()[:120]
        detail = f"got {got}" if got else "no Content-Type header was sent"
        self._send_error_json(415, (
            f"refused: POST bodies must be Content-Type: application/json ({detail}). "
            + CROSS_SITE_REFUSAL_HINT
        ))
        return True

    def _serve_static_file(self, rel_path):
        resolved = _resolve_static_path(rel_path)
        if resolved is None:
            self._send_error_json(400, "invalid path")
            return
        if not os.path.isfile(resolved):
            self._send_error_json(404, f"not found: {rel_path}")
            return
        ctype, _ = mimetypes.guess_type(resolved)
        ctype = ctype or "application/octet-stream"
        try:
            with open(resolved, "rb") as f:
                data = f.read()
        except OSError:
            self._send_error_json(500, "failed to read file")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    # -- config access -----------------------------------------------
    @property
    def config(self):
        return self.server.centrale_config

    # -- API handlers --------------------------------------------------
    def _handle_board(self, query):
        force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
        board = get_board(self.config, force=force)
        # Shallow-copy rather than mutate: `board` may be the cached dict
        # get_board() hands back on every call, and capabilities/harvest
        # mode aren't part of that aggregation's own cached contract.
        response = dict(board)
        response["capabilities"] = {"tmux": tmux_capability(self.config)}
        response["version"] = served_version(self.config)
        # task-128: derived per request, never cached -- the whole point
        # is to notice the checkout moving under a process that cannot.
        response["codeDrift"] = code_drift(self.config)
        response["harvestMode"] = (self.config.get("harvest") or {}).get("mode", "click")
        response["sessionPreviewMode"] = session_preview_mode(self.config)
        response["refreshIntervalSeconds"] = self.config.get(
            "refreshIntervalSeconds", DEFAULT_REFRESH_INTERVAL_SECONDS
        )
        self._send_json(200, response)

    def _handle_task(self, query):
        project_name = (query.get("project") or [None])[0]
        task_id = (query.get("id") or [None])[0]

        if not project_name:
            self._send_error_json(400, "missing required query param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p["name"] == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return

        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        try:
            data = run_backlog(["task", "view", task_id, "--json"], cwd=project["path"])
        except BacklogError as exc:
            self._send_error_json(502, str(exc))
            return

        if not isinstance(data, dict) or data.get("schemaVersion") != 1:
            self._send_error_json(502, "unsupported schemaVersion from backlog CLI")
            return

        response = dict(data)

        # This task's Done status, checked ACs, and implementation notes
        # are committed only on task/<id> until that branch is merged --
        # the read above, against the main checkout, won't see them.
        # _branch_task_view finds that branch wherever it actually lives
        # (Centrale worktree, a foreign checkout, or parked -- task-79)
        # and is best-effort throughout: anything unreadable there just
        # means no "branchTask" in the response, not a failed request
        # (the main-checkout view above is still a valid response on its
        # own).
        branch_data = _branch_task_view(self.config, project, task_id)
        if branch_data is not None:
            response["branchTask"] = branch_data

        self._send_json(200, response)

    def _handle_sessions(self):
        try:
            sessions = list_sessions()
        except BacklogError as exc:
            self._send_error_json(502, str(exc))
            return
        enrich_sessions_with_files(sessions, self.config)
        self._send_json(200, {"sessions": sessions})

    def _handle_session_pane(self, query):
        """GET /api/session-pane?project=<name>&task=<taskId>[&lines=N]
        (task-60): the rendered text of a live agent session's pane, for
        the drawer's read-only live preview. Identity validation mirrors
        /api/end-session; the session name comes from spawn.session_name
        (task-59's tmux-safe encoding, so dotted subtask ids resolve).
        Refuses with 403 -- before touching tmux -- whenever
        sessionPreview.mode is "off": disabling the feature disables the
        endpoint, not just the UI. Exactly one tmux call per request; see
        capture_session_pane."""
        import spawn  # local import: avoids a circular import at module load

        if session_preview_mode(self.config) == "off":
            self._send_error_json(
                403, "session preview is disabled (sessionPreview.mode is \"off\")"
            )
            return

        project_name = (query.get("project") or [None])[0]
        task_id = (query.get("task") or [None])[0]
        if not project_name:
            self._send_error_json(400, "missing required query param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        max_lines = _normalize_pane_lines((query.get("lines") or [None])[0])
        # One capture-pane call, no list-sessions preflight, so the
        # drawer's ~2s poll tick keeps costing exactly one tmux call.
        name = spawn.session_name(project_name, task_id)
        try:
            lines = capture_session_pane(name, max_lines)
        except PaneCaptureError as exc:
            if exc.status == 404:
                self._send_error_json(404, f"no live session for {project_name}/{task_id}")
            else:
                self._send_error_json(exc.status, str(exc))
            return

        record_pane_capture(name)  # task-61: arms POST /api/session-input for this session
        captured_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._send_json(200, {
            "session": name,
            "lines": lines,
            "lineCount": len(lines),
            "capturedAt": captured_at,
        })

    # -- routing ---------------------------------------------------------
    # GET routes are dispatched here; POST routes (currently just
    # /api/spawn) are dispatched from do_POST below.
    def do_GET(self):
        try:
            # task-122: the trust boundary runs before any routing, so a
            # refused read reaches no git, tmux, backlog, checkCommand,
            # scratch-worktree or pane-capture boundary -- and a GET route
            # with side effects (/api/harvest runs the merge gate;
            # /api/session-pane arms POST /api/session-input) cannot be
            # driven by a page that is not Centrale's own.
            if self._refuse_untrusted_request():
                return

            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/":
                self._serve_static_file("index.html")
            elif path.startswith("/static/"):
                self._serve_static_file(path[len("/static/"):])
            elif path == "/favicon.ico":
                # index.html declares /static/favicon.svg, so browsers do
                # not fall back to this conventional path -- but anything
                # that hits it directly (a bookmark manager, a cached
                # expectation) gets the icon rather than a 404. Same file,
                # same content type: browsers go by Content-Type, not the
                # extension in the URL.
                self._serve_static_file("favicon.svg")
            elif path == "/api/board":
                self._handle_board(query)
            elif path == "/api/task":
                self._handle_task(query)
            elif path == "/api/sessions":
                self._handle_sessions()
            elif path == "/api/session-pane":
                self._handle_session_pane(query)
            elif path == "/api/harvest":
                self._handle_harvest_get(query)
            elif path == "/api/harvest-progress":
                self._handle_harvest_progress_get()
            elif path == "/api/discard-preview":
                self._handle_discard_preview(query)
            elif path == "/api/settings":
                self._handle_settings_get()
            else:
                self._send_error_json(404, "not found")
        except Exception:  # pragma: no cover - defensive, never leak tracebacks
            try:
                self._send_error_json(500, "internal server error")
            except Exception:
                pass

    def _handle_harvest_get(self, query):
        import harvest  # local import: avoids a circular import at module load

        project_name = (query.get("project") or [None])[0]
        if not project_name:
            self._send_error_json(400, "missing required query param: project")
            return

        try:
            branches = harvest.evaluate_all_branches(self.config, project_name)
        except harvest.HarvestError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        events = harvest.recent_events(project_name)
        self._send_json(200, {"branches": branches, "events": events})

    def _handle_harvest_progress_get(self):
        """task-96: what the one in-flight harvest is doing right now,
        or null when nothing is being harvested.

        Deliberately the cheapest endpoint here -- one in-memory read, no
        subprocess, no config walk, no params -- because the frontend
        polls it every second while its own POST /api/harvest is still
        outstanding, and that POST holds harvest.py's merge lock the whole
        time. (The server is a ThreadingHTTPServer, so the poll is served
        on its own thread rather than queueing behind the merge.)
        """
        import harvest  # local import: avoids a circular import at module load

        self._send_json(200, {"progress": harvest.current_progress()})

    def _handle_settings_get(self):
        import settings  # local import: avoids a circular import at module load

        self._send_json(200, settings.current_settings(self.config))

    def _handle_settings_post(self):
        import settings  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return

        try:
            result = settings.apply_settings(self.config, body)
        except settings.ValidationError as exc:
            self._send_json(400, {"error": str(exc), "fields": exc.fields})
            return
        except settings.SettingsError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        self._send_json(200, result)

    def _read_json_body(self, max_bytes=65536):
        """Read and parse a JSON object body. Returns the parsed dict, or
        raises ValueError on a missing/oversized/malformed body."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            length = 0

        if length <= 0:
            raise ValueError("missing request body")
        if length > max_bytes:
            raise ValueError("request body too large")

        raw = self.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _handle_spawn(self):
        import spawn  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return

        project = body.get("project")
        task_id = body.get("taskId", body.get("task_id"))

        # task-121: creating this task's worktree is a lifecycle
        # operation like any other, so it waits for one already running
        # (a discard, an abandon, a cleanup, a merge) rather than racing
        # it. A spawn of a DIFFERENT task holds a different lock and is
        # unaffected.
        try:
            with task_lifecycle_lock(project, task_id):
                result = spawn.spawn(self.config, project, task_id)
        except spawn.SpawnError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        self._send_json(200, result)

    def _handle_resume(self):
        import spawn  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return

        project = body.get("project")
        task_id = body.get("taskId", body.get("task_id"))

        # task-66: an optional flag, never a separate endpoint -- the
        # reconcile variant IS a resume (same validation/409/worktree
        # path, see spawn.resume), differing only in the prompt it runs.
        reconcile = body.get("reconcile") is True

        try:
            with task_lifecycle_lock(project, task_id):  # task-121, as for spawn above
                result = spawn.resume(self.config, project, task_id, reconcile=reconcile)
        except spawn.SpawnError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        self._send_json(200, result)

    def _handle_browser(self):
        import browser  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return

        project = body.get("project")

        try:
            result = browser.launch_or_reuse(self.config, project)
        except browser.BrowserError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        response = dict(result)
        # task-157: derived per request, never cached and never acted on
        # -- null while the board and the `backlog` on PATH agree (or
        # while either cannot be asked), otherwise the two versions, for
        # the frontend to say so next to the board it just opened.
        response["versionDrift"] = browser.version_drift(project)
        self._send_json(200, response)

    def _handle_agent_event(self, query):
        """POST /api/agent-event: identity travels via the query string
        (?project=&task=, plus the optional built-in agentKind that
        CENTRALE_EVENT_URL carries -- see spawn.event_url), state via a
        JSON body {"state": ...}.

        task-99 removed the ?state= query fallback (a bodyless POST could
        drive the endpoint through it) but deliberately kept the query
        identity here, and only here: it is the CENTRALE_EVENT_URL
        machine contract, not a UI convenience. A URL is the only carrier
        the notify hook has -- centrale_notify.py is handed one env var
        and knows nothing about which task it belongs to -- and every
        already-running session holds its URL baked into an environment
        that cannot be changed for that session's lifetime. The cross-site
        guard in do_POST covers this route like any other: a page cannot
        send application/json cross-origin without a preflight this
        server never answers, and the JSON body is now mandatory.

        Validates project/taskId with the same rules as every other
        endpoint; 4xx JSON on any failure, never a traceback."""
        project_name = (query.get("project") or [None])[0]
        task_id = (query.get("task") or [None])[0]
        agent_kind = (query.get("agentKind") or [None])[0]

        if not project_name:
            self._send_error_json(400, "missing required query param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        # task-99: the state comes from the JSON body only -- the old
        # ?state= query fallback let a bodyless POST drive this endpoint.
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return
        state = body.get("state")

        try:
            record_agent_event(project_name, task_id, state, agent_kind=agent_kind)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return

        self._send_json(200, {"ok": True})

    def _handle_end_session(self):
        """POST /api/end-session: identity travels in the JSON body,
        {"project", "taskId"} -- matching /api/spawn's shape. (task-99
        removed the ?project=&task= query form: a bare cross-origin form
        POST could populate it without a body at all.)
        Validates project/taskId with the same rules as every other
        endpoint. Computes the exact session name via spawn.session_name --
        the same helper spawn()/resume() themselves use, so this never
        drifts from their naming -- confirms it's actually live via
        list_sessions() (a 404 JSON error if not, never assumed), then
        kills it with `tmux kill-session -t =<name>`: the leading "="
        forces exact-name matching, since tmux -t otherwise prefix-matches
        and could kill an unrelated session (see AGENTS.md on the
        injectable run_tmux boundary and why this is the one place in
        end-session that touches tmux)."""
        import spawn  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return
        project_name = body.get("project")
        task_id = body.get("taskId", body.get("task"))

        if not project_name:
            self._send_error_json(400, "missing required param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        try:
            existing = list_sessions()
        except BacklogError as exc:
            self._send_error_json(502, f"failed to check existing sessions: {exc}")
            return

        existing_names = {s.get("name") for s in existing}
        name = spawn.session_name(project_name, task_id)
        if name not in existing_names:
            self._send_error_json(404, f"no live session for {project_name}/{task_id}")
            return

        proc = run_tmux(["kill-session", "-t", "=" + name])
        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "tmux kill-session failed").strip()
            self._send_error_json(500, f"tmux kill-session failed: {stderr}")
            return

        self._send_json(200, {"ok": True, "session": name})

    def _handle_session_input(self):
        """POST /api/session-input (task-61): type a single-line reply, or
        press one session key, in the live agent session the drawer is
        previewing. Body: {"project", "taskId"} plus exactly one of
        {"text": "<one line>"} -- delivered literally, then Enter -- or
        {"key": "Escape" | "Enter"} (task-135), sent by key name. Identity
        travels exactly like /api/end-session: the JSON body only
        (task-99 removed the ?project=&task= query form).

        Refusal order, each before any tmux call:
          403  sessionPreview.mode is not "interact" (disabling the
               reply tier disables the endpoint, not just the UI);
          400/404  identity validation, same rules as every endpoint;
          400  body validation (validate_session_input);
          404  the computed session name does not round-trip through the
               shared TASK-59-safe reverse lookup to this same known
               project/task -- the endpoint never targets a session it
               can't attribute to a board task, and never accepts a
               session name from the client at all;
          409  no pane capture of this session within
               SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS -- a reply is only
               allowed against a pane the drawer has just shown.
        Then send_session_input; its own 404 covers a session that ended
        between the capture and the click."""
        import spawn  # local import: avoids a circular import at module load

        mode = session_preview_mode(self.config)
        if mode != "interact":
            self._send_error_json(
                403, f"session reply is disabled (sessionPreview.mode is \"{mode}\")"
            )
            return

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_error_json(400, f"invalid request body: {exc}")
            return
        project_name = body.get("project")
        task_id = body.get("taskId", body.get("task"))

        if not project_name:
            self._send_error_json(400, "missing required param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        try:
            kind, value = validate_session_input(body)
        except SessionInputError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        # No tmux preflight: the freshness gate below demands a recent
        # capture anyway, and an uncaptured session falls through to the
        # ordinary stale-capture 409.
        name = spawn.session_name(project_name, task_id)
        owner, owner_task = _parse_session_project_and_task(name, self.config)
        if owner is None or owner.get("name") != project_name or owner_task != task_id.lower():
            self._send_error_json(
                404, f"session {name} does not resolve to a known board task"
            )
            return

        age = pane_capture_age(name)
        if age is None or age > SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS:
            self._send_json(409, {
                "error": (
                    "no fresh pane capture for this session -- the reply was not sent. "
                    f"The drawer must have previewed the pane within the last "
                    f"{SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS}s."
                ),
                "captureAgeSeconds": None if age is None else round(age, 1),
            })
            return

        try:
            send_session_input(name, kind, value)
        except SessionInputError as exc:
            if exc.status == 404:
                self._send_error_json(404, f"no live session for {project_name}/{task_id}")
            else:
                self._send_error_json(exc.status, str(exc))
            return

        self._send_json(200, {
            "ok": True,
            "session": name,
            "sent": {kind: value},
            "captureAgeSeconds": round(age, 1),
        })

    def _handle_cleanup_branch(self):
        """POST /api/cleanup-branch (task-43): removes a task/<id>
        branch's worktree and deletes the branch itself once it's fully
        merged into the base branch -- whether by Centrale's own harvest
        or entirely out-of-band (the TASK-1 incident in another repo: a branch
        merged by a user-side monitor agent, leaving Centrale still
        offering a Merge button that would only ever refuse). Identity
        travels the same way as /api/end-session: the JSON body's
        {"project", "taskId"} only (task-99 removed the ?project=&task=
        query form -- a bare cross-origin form POST could populate it).

        Never trusts the client's board-derived alreadyMerged flag --
        re-verifies it itself via _branch_already_merged (ancestor AND
        main-side status Done, task-45), the exact same combined helper
        board aggregation uses, so the two can never disagree. Refuses
        (409) if a live tmux session is still running
        for this task, or if the branch exists but isn't actually fully
        merged. Collects the worktree's uncommitted/untracked paths
        (dequoted) BEFORE removing it, so the response can name exactly
        what a forced removal is about to discard -- the TASK-9 lesson
        that untracked files can be real unsaved work.

        task-80: a branch checked out in a worktree Centrale does NOT
        manage (spawn.checkout_state kind "external" -- an out-of-band
        merge whose foreign checkout is still around, the TASK-74
        reproduction) is refused with 409 and the exact sentence
        spawn/resume use (spawn.external_checkout_reason), classified
        from `git worktree list --porcelain` BEFORE any side effect --
        never by parsing `git branch -d`'s refusal, whose wording
        changes across git versions ("used by worktree at" in 2.43,
        "checked out at" earlier). It sits ahead of the not-fully-merged
        check on purpose: one cheap git call, no backlog CLI call, and
        the foreign worktree is the root cause either way (merging it
        would fail harvest's gate 2 for the same reason).

        Removal is `git worktree remove --force` then `git branch -d`
        (never -D: ancestry was just re-verified, so a safe delete
        should always succeed; a failure there is surfaced as a 500
        with git's own stderr rather than silently forced through).
        main is never touched by any of this. Missing worktree but a
        still-present branch skips removal and just deletes the branch;
        neither present is a 404 -- there's nothing here to clean up."""
        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return
        project_name = body.get("project")
        task_id = body.get("taskId", body.get("task"))

        if not project_name:
            self._send_error_json(400, "missing required param: project")
            return
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return

        repo_path = project.get("path") or ""
        if not os.path.isdir(repo_path):
            self._send_error_json(502, f"project path not found: {repo_path or '(empty)'}")
            return

        # task-121: everything below removes a worktree and deletes a
        # branch, so it runs under this task's lifecycle lock -- a
        # discard, an abandon, a merge or a re-spawn of the same task
        # waits rather than interleaving its git operations with these.
        with task_lifecycle_lock(project_name, task_id):
            self._cleanup_branch_locked(project, project_name, task_id, repo_path)

    def _cleanup_branch_locked(self, project, project_name, task_id, repo_path):
        """The body of /api/cleanup-branch, under this task's lifecycle
        lock (task-121). Split out for the same reason the discard's is:
        the lock belongs on one visible line in the handler."""
        import spawn  # local import: avoids a circular import at module load

        try:
            existing = list_sessions()
        except BacklogError as exc:
            self._send_error_json(502, f"failed to check existing sessions: {exc}")
            return
        existing_names = {s.get("name") for s in existing}
        live = spawn.session_name(project_name, task_id)
        if live in existing_names:
            self._send_error_json(409, f"a live tmux session is still running: {live}")
            return

        branch = spawn.branch_name(task_id)
        wt_dir = spawn.worktree_dir(self.config, project_name, task_id)
        wt_exists = os.path.isdir(wt_dir)
        branch_exists = _task_branch_exists(repo_path, branch)

        if not wt_exists and not branch_exists:
            self._send_error_json(404, f"no worktree or branch found for {project_name}/{task_id}")
            return

        if branch_exists:
            # task-80: refuse a branch checked out outside Centrale with
            # the honest 409 before touching anything (see the docstring).
            # The centrale kind (checked out at our own worktree path,
            # whether or not the dir still exists) and the none kind (a
            # parked branch) fall through to today's behavior unchanged.
            state = spawn.checkout_state(self.config, project, task_id)
            if state["kind"] == "external":
                self._send_error_json(409, spawn.external_checkout_reason(branch, state["path"]))
                return
            # task-45: the same combined ancestor-AND-Done decision board
            # aggregation uses (_branch_already_merged), so the flag and
            # this re-verification can never disagree. Status comes from
            # the main checkout (repo_path), never a worktree -- an
            # empty, still-in-progress claim branch would otherwise be a
            # trivial ancestor of main and pass this check wrongly.
            task_status = _read_main_task_status(repo_path, task_id)
            if not _branch_already_merged(repo_path, task_id, task_status):
                self._send_error_json(409, f"{branch} is not fully merged into the base branch")
                return

        discarded_paths = []
        if wt_exists:
            status_proc = run_git(["status", "--porcelain"], cwd=wt_dir)
            if status_proc.returncode != 0:
                stderr = (status_proc.stderr or status_proc.stdout or "unknown error").strip()
                self._send_error_json(500, f"failed to check worktree status: {stderr}")
                return
            discarded_paths = _dirty_paths_from_status(status_proc.stdout or "")

            remove_proc = run_git(["worktree", "remove", "--force", wt_dir], cwd=repo_path)
            if remove_proc.returncode != 0:
                stderr = (remove_proc.stderr or remove_proc.stdout or "unknown error").strip()
                self._send_error_json(500, f"failed to remove worktree {wt_dir}: {stderr}")
                return

        if branch_exists:
            branch_del_proc = run_git(["branch", "-d", branch], cwd=repo_path)
            if branch_del_proc.returncode != 0:
                stderr = (branch_del_proc.stderr or branch_del_proc.stdout or "unknown error").strip()
                self._send_error_json(500, f"failed to delete branch {branch}: {stderr}")
                return

        self._send_json(200, {
            "ok": True,
            "branch": branch,
            "worktreeRemoved": wt_exists,
            "discardedPaths": discarded_paths,
        })

    # ------------------------------------------------------------------
    # Throwing an attempt away (task-119)
    # ------------------------------------------------------------------
    #
    # /api/cleanup-branch is the "this was already merged" route and
    # deletes with the safe `git branch -d`, which refuses unmerged
    # commits by design -- exactly the case these two routes exist for.
    # They are separate POST routes rather than a mode flag on that one
    # for the same reason they are separate from each other: whether the
    # branch survives is the whole difference, and it is not a thing to
    # infer from a boolean buried in a shared handler's body.
    #
    #   /api/discard-attempt   -- worktree AND branch go. The task is
    #                             left with no branch at all, so the
    #                             ordinary Spawn button comes back and
    #                             the next spawn branches fresh from the
    #                             base (spawn._ensure_worktree REUSES an
    #                             existing branch, so parking the branch
    #                             would make every re-spawn start from
    #                             the bad commits again).
    #   /api/abandon-worktree  -- only the worktree goes. The branch is
    #                             left parked (checkout_state's "none"
    #                             kind, task-70), still mergeable by the
    #                             worktree-less gated merge.
    #
    # Neither ever touches the backlog task's status. The board is the
    # source of truth for where a task stands, and "this attempt was
    # bad" is not the same statement as "this task is back to To Do" --
    # that call is the user's, made on the board.

    def _discard_identity(self, body):
        """(project, task_id) for the three task-119 routes, or None
        after having already sent the error response.

        Factored out rather than triplicated (unlike the older handlers,
        which each carry their own copy) precisely because these three
        must agree byte for byte about what they accept: two of them
        destroy things, and the third exists only to describe what those
        two would destroy."""
        project_name = body.get("project")
        task_id = body.get("taskId", body.get("task"))

        if not project_name:
            self._send_error_json(400, "missing required param: project")
            return None
        project = next(
            (p for p in self.config.get("projects", []) if p.get("name") == project_name),
            None,
        )
        if project is None:
            self._send_error_json(404, f"unknown project: {project_name}")
            return None
        if not task_id or not TASK_ID_RE.match(task_id):
            self._send_error_json(400, "invalid or missing task id")
            return None
        if not os.path.isdir(project.get("path") or ""):
            self._send_error_json(502, f"project path not found: {project.get('path') or '(empty)'}")
            return None
        return project, task_id

    def _live_session_for(self, project_name, task_id):
        """(name, None) for a still-live session, (None, None) for none,
        or (None, error_message) when tmux could not be asked at all."""
        import spawn  # local import: avoids a circular import at module load

        try:
            existing = list_sessions()
        except BacklogError as exc:
            return None, f"failed to check existing sessions: {exc}"
        existing_names = {s.get("name") for s in existing}
        name = spawn.session_name(project_name, task_id)
        return (name if name in existing_names else None), None

    def _survey_attempt(self, project, task_id):
        """Everything the confirm sentence and the recovery line need,
        measured from git at the moment it is asked: what the branch
        carries over its base, what the worktree holds that was never
        committed, and where the branch tip is.

        Deliberately NOT board fields. The board's numbers would be up
        to a refresh interval stale, and this is the sentence a user
        reads immediately before an irreversible click -- "3 commits and
        4 uncommitted files" has to be true of the repository as it is
        now, not as it was when the board last polled. It also costs
        nothing until someone actually reaches for the action, instead
        of adding a git call per branch-bearing task to every board
        read."""
        import spawn  # local import: avoids a circular import at module load

        repo_path = project["path"]
        project_name = project["name"]
        branch = spawn.branch_name(task_id)
        wt_dir = spawn.worktree_dir(self.config, project_name, task_id)
        wt_exists = os.path.isdir(wt_dir)
        base_branch = spawn.current_branch(repo_path)
        tip = _branch_tip_sha(repo_path, branch)

        survey = {
            "branch": branch,
            "branchExists": tip is not None,
            "branchTip": tip,
            "baseBranch": base_branch,
            "commitCount": _branch_commit_count(repo_path, branch, base_branch) if tip else None,
            "worktreePath": wt_dir,
            "worktreeExists": wt_exists,
            "dirtyPaths": None,
            "dirtyFileCount": None,
        }
        if wt_exists:
            proc = run_git(["status", "--porcelain"], cwd=wt_dir)
            if proc.returncode == 0:
                paths = _dirty_paths_from_status(proc.stdout or "")
                survey["dirtyPaths"] = paths
                survey["dirtyFileCount"] = len(paths)
        else:
            # No worktree is not an unmeasurable count -- it is zero
            # uncommitted files, and the confirm can say so honestly.
            survey["dirtyPaths"] = []
            survey["dirtyFileCount"] = 0
        return survey

    def _handle_discard_preview(self, query):
        """GET /api/discard-preview?project=<name>&task=<taskId>
        (task-119): what the two destructive routes below would destroy,
        without destroying any of it.

        The frontend fetches this on the FIRST (arming) click and puts
        the numbers straight into the confirming button's own label, so
        the click that actually destroys something names it: "Discard 3
        commits and 4 uncommitted files?". A generic "are you sure" is
        not an acceptable confirm for this action, and a count this
        endpoint could not measure (dirtyFileCount or commitCount null)
        is reported as null rather than guessed at -- the frontend
        refuses to arm on a null rather than round it down to a
        comfortable-looking zero.

        Read-only: no tag, no removal, no branch delete, and no backlog
        call at all. It also reports the two refusals the POSTs would
        make (liveSession, externalCheckout) so the UI can say why an
        action is unavailable instead of offering a click the server
        would only 409."""
        import spawn  # local import: avoids a circular import at module load

        identity = self._discard_identity({
            "project": (query.get("project") or [None])[0],
            "task": (query.get("task") or query.get("taskId") or [None])[0],
        })
        if identity is None:
            return
        project, task_id = identity

        survey = self._survey_attempt(project, task_id)
        if not survey["branchExists"] and not survey["worktreeExists"]:
            self._send_error_json(
                404, f"no worktree or branch found for {project['name']}/{task_id}")
            return

        live, session_error = self._live_session_for(project["name"], task_id)
        if session_error is not None:
            self._send_error_json(502, session_error)
            return

        external = None
        if survey["branchExists"]:
            state = spawn.checkout_state(self.config, project, task_id)
            if state["kind"] == "external":
                external = {
                    "path": state["path"],
                    "reason": spawn.external_checkout_reason(survey["branch"], state["path"]),
                }

        response = dict(survey)
        response["liveSession"] = live
        response["externalCheckout"] = external
        response["recoveryCommand"] = (
            recovery_command(survey["branch"], survey["branchTip"]) if survey["branchTip"] else None
        )
        self._send_json(200, response)

    def _refuse_discard_or_none(self, project, task_id, branch):
        """The refusals /api/discard-attempt and /api/abandon-worktree
        share, in the order they run -- all of them BEFORE any side
        effect, so a refused request leaves the repository exactly as it
        found it. Returns True when a response has already been sent.

        A live session first (killing an agent's worktree out from under
        it is how work gets lost, and End session is the way out), then
        a branch checked out in a worktree Centrale does not manage:
        git refuses to delete a checked-out branch and would refuse to
        remove someone else's worktree, so this says so in the same
        sentence /api/spawn, /api/resume and /api/cleanup-branch already
        use rather than surfacing git's own version-dependent wording."""
        import spawn  # local import: avoids a circular import at module load

        live, session_error = self._live_session_for(project["name"], task_id)
        if session_error is not None:
            self._send_error_json(502, session_error)
            return True
        if live is not None:
            self._send_error_json(409, f"a live tmux session is still running: {live}")
            return True

        if _task_branch_exists(project["path"], branch):
            state = spawn.checkout_state(self.config, project, task_id)
            if state["kind"] == "external":
                self._send_error_json(409, spawn.external_checkout_reason(branch, state["path"]))
                return True
        return False

    def _remove_worktree_or_error(self, project, wt_dir):
        """`git worktree remove --force` on Centrale's own worktree for
        this task. Returns an error message, or None on success (and
        None for a worktree that isn't there -- nothing to remove is not
        a failure)."""
        if not os.path.isdir(wt_dir):
            return None
        proc = run_git(["worktree", "remove", "--force", wt_dir], cwd=project["path"])
        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "unknown error").strip()
            return f"failed to remove worktree {wt_dir}: {stderr}"
        return None

    # ------------------------------------------------------------------
    # Binding a confirm to the state it described (task-121)
    # ------------------------------------------------------------------
    #
    # The preview and the destructive POST are two requests, and the
    # repository can move between them -- an agent commits, a file is
    # written, another lifecycle route runs. Before task-121 the POST
    # carried only {project, taskId}, so an armed confirm reading
    # "Discard 2 commits and 3 uncommitted files?" could destroy a third
    # commit it never named, and the response would then report a count
    # the user had never been shown.
    #
    # So the confirm's own numbers travel with the click: the tip SHA the
    # preview reported and the exact uncommitted paths it listed. The
    # handler re-measures under the lifecycle lock and refuses with 409
    # if either has moved -- before the tag, before the worktree removal,
    # before the branch delete. Nothing is destroyed by a refusal, and
    # the UI's answer to it is another preview, whose confirm names the
    # new numbers.
    #
    # The expectation is REQUIRED, not optional-and-honored: a POST that
    # states no expectation has reviewed no state, which is exactly the
    # gap this closes. GET /api/discard-preview is where the two values
    # come from, for the frontend and for curl alike.

    def _expected_state_or_none(self, body):
        """(expected_tip, expected_dirty_paths) from a destructive POST
        body, or None after having already sent the 400.

        expectedBranchTip is null for "I reviewed a task with no branch"
        -- a real, previewable state (a worktree whose branch someone
        else already deleted), and not the same statement as omitting
        the field."""
        for field in ("expectedBranchTip", "expectedDirtyPaths"):
            if field not in body:
                self._send_error_json(400, (
                    f"missing required param: {field} -- a destructive confirm must name the"
                    " state it reviewed; GET /api/discard-preview reports branchTip and"
                    " dirtyPaths for exactly this"))
                return None

        tip = body["expectedBranchTip"]
        if tip is not None and not (isinstance(tip, str) and _FULL_SHA_RE.match(tip)):
            self._send_error_json(
                400, "expectedBranchTip must be a full 40-character commit sha, or null for a"
                     " task whose branch the preview did not find")
            return None

        paths = body["expectedDirtyPaths"]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            self._send_error_json(
                400, "expectedDirtyPaths must be the list of paths GET /api/discard-preview"
                     " reported (an empty list for a clean or absent worktree)")
            return None
        return tip, paths

    def _refuse_stale_state_or_none(self, survey, expected_tip, expected_paths):
        """True (409 already sent) when the repository is no longer in
        the state the confirm described. Called with the lifecycle lock
        held and before any side effect, so a refusal leaves everything
        exactly as it was.

        Paths are compared order-insensitively: `git status --porcelain`
        order is not part of what the user reviewed ("3 uncommitted
        files" is), and a reordering that destroys nothing new should not
        cost them a second confirm."""
        branch = survey["branch"]
        tip = survey["branchTip"]
        if tip != expected_tip:
            if expected_tip is None:
                reason = f"{branch} exists now and did not when that preview was taken"
            elif tip is None:
                reason = f"{branch} is already gone -- it was at {expected_tip[:10]}"
            else:
                reason = (f"{branch} has moved: it is at {tip[:10]} now,"
                          f" not {expected_tip[:10]}")
            self._send_error_json(409, (
                f"the repository changed after that preview -- {reason}. Nothing was"
                " discarded; check again what would go."))
            return True

        paths = survey["dirtyPaths"]
        if paths is None or sorted(paths) != sorted(expected_paths):
            now = "an unmeasurable number of" if paths is None else str(len(paths))
            self._send_error_json(409, (
                "the worktree changed after that preview -- it holds"
                f" {now} uncommitted files now, not the"
                f" {len(expected_paths)} the confirm named. Nothing was discarded; check"
                " again what would go."))
            return True
        return False

    def _handle_discard_attempt(self):
        """POST /api/discard-attempt (task-119): throw a bad attempt
        away -- remove the worktree AND delete the task's branch -- so
        the task can be started over from the base.

        Body: {"project", "taskId", "expectedBranchTip",
        "expectedDirtyPaths"} -- the identity /api/end-session and
        /api/cleanup-branch use, plus (task-121) the state the confirm
        described, taken from GET /api/discard-preview.

        Why the branch has to go and not just the worktree:
        spawn._ensure_worktree REUSES an existing task/<id> branch
        (`git worktree add <dir> <branch>`), and only creates one from
        the base when there is none. Parking the branch would make every
        subsequent Re-spawn start from the same bad commits; a genuine
        redo needs the branch gone.

        The commits are unmerged by definition -- that is what this route
        is for -- so the safe `git branch -d` would refuse. That makes
        this the only irreversible act in the product, and the order
        below is what makes it recoverable anyway. It all runs under this
        task's lifecycle lock (task-121), so no spawn, resume, merge or
        cleanup of the same task can interleave with it:

          1. refuse a live session or a foreign checkout;
          2. survey (tip SHA, commits over base, uncommitted paths) --
             read before anything is touched, so the response can name
             what it destroyed even though it is gone;
          3. refuse a survey that no longer matches the confirm the user
             armed (task-121);
          4. refuse a live session or foreign checkout that appeared in
             the meantime -- checked twice on purpose, see below;
          5. `git tag <recovery tag> <tip>` -- see recovery_tag_name for
             the measurement behind this: nothing else in the repository
             still references those commits once the worktree and the
             branch are both gone, so a failure here aborts the whole
             request with NOTHING removed;
          6. `git worktree remove --force`;
          7. `git update-ref -d refs/heads/<branch> <tip>` -- a
             compare-and-delete rather than `git branch -D` (task-121):
             it deletes the branch only while it still points at the tip
             that was just tagged, so the recovery anchor can never name
             a different commit from the one that was destroyed. A tip
             that moved under a non-Centrale git process fails here with
             the branch intact.

        The task's backlog status is never read and never written. "This
        attempt was bad" is not "this task is back to To Do" -- the
        board is the source of truth for where a task stands, and that
        call belongs to the user."""
        try:
            body = self._read_json_body(max_bytes=DISCARD_BODY_MAX_BYTES)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return
        identity = self._discard_identity(body)
        if identity is None:
            return
        project, task_id = identity
        expected = self._expected_state_or_none(body)
        if expected is None:
            return

        with task_lifecycle_lock(project["name"], task_id):
            self._discard_attempt_locked(project, task_id, *expected)

    def _discard_attempt_locked(self, project, task_id, expected_tip, expected_paths):
        """The body of /api/discard-attempt, under this task's lifecycle
        lock. Split out only so the lock is one unmissable line in the
        handler rather than an indent level over a hundred."""
        import spawn  # local import: avoids a circular import at module load

        repo_path = project["path"]
        branch = spawn.branch_name(task_id)

        if self._refuse_discard_or_none(project, task_id, branch):
            return

        survey = self._survey_attempt(project, task_id)
        if not survey["branchExists"] and not survey["worktreeExists"]:
            self._send_error_json(
                404, f"no worktree or branch found for {project['name']}/{task_id}")
            return
        if survey["worktreeExists"] and survey["dirtyPaths"] is None:
            self._send_error_json(
                500, f"failed to check worktree status in {survey['worktreePath']}")
            return
        if self._refuse_stale_state_or_none(survey, expected_tip, expected_paths):
            return

        # Checked once before the survey and once here, immediately
        # before the first destructive call (task-121). The lifecycle
        # lock rules out Centrale's own spawn/resume, but a session
        # started outside Centrale -- or a worktree someone added by
        # hand -- can still appear between the two, and this is the last
        # moment at which noticing it costs nothing.
        if self._refuse_discard_or_none(project, task_id, branch):
            return

        tag = None
        if survey["branchTip"]:
            tag = recovery_tag_name(task_id)
            tag_proc = run_git(["tag", tag, survey["branchTip"]], cwd=repo_path)
            if tag_proc.returncode != 0:
                stderr = (tag_proc.stderr or tag_proc.stdout or "unknown error").strip()
                # Nothing has been removed at this point, and nothing
                # will be: without the tag the commits would be
                # unreachable the moment the branch goes.
                self._send_error_json(500, f"failed to tag {survey['branchTip']} as {tag}: {stderr}")
                return

        error = self._remove_worktree_or_error(project, survey["worktreePath"])
        if error is not None:
            self._send_error_json(500, error)
            return

        branch_deleted = False
        if survey["branchExists"]:
            del_proc = run_git(
                ["update-ref", "-d", f"refs/heads/{branch}", survey["branchTip"]], cwd=repo_path)
            if del_proc.returncode != 0:
                stderr = (del_proc.stderr or del_proc.stdout or "unknown error").strip()
                self._send_error_json(
                    500,
                    f"failed to delete branch {branch} at {survey['branchTip']}: {stderr}")
                return
            branch_deleted = True

        self._send_json(200, {
            "ok": True,
            "branch": branch,
            "branchDeleted": branch_deleted,
            "branchTip": survey["branchTip"],
            "baseBranch": survey["baseBranch"],
            "commitCount": survey["commitCount"],
            "worktreeRemoved": survey["worktreeExists"],
            "discardedPaths": survey["dirtyPaths"] or [],
            "recoveryTag": tag,
            "recoveryCommand": (
                recovery_command(branch, survey["branchTip"]) if survey["branchTip"] else None
            ),
        })

    def _handle_abandon_worktree(self):
        """POST /api/abandon-worktree (task-119): the milder half --
        remove this task's worktree and leave the branch exactly where
        it is.

        Body: {"project", "taskId", "expectedBranchTip",
        "expectedDirtyPaths"}. Same refusals, in the same order, as
        /api/discard-attempt (see _refuse_discard_or_none and
        _refuse_stale_state_or_none), under the same lifecycle lock, and
        with the same silence about the backlog task's status.

        What is left behind is task-70's "parked" branch: a branch that
        exists and is checked out nowhere (checkout_state kind "none"),
        which the gated merge can still merge later without a worktree.
        This is not a lesser discard -- it is the other statement: not
        merging now, but not throwing the work away either. Nothing
        committed is destroyed; only the worktree's uncommitted and
        untracked files are, which is why the response names them the
        same way /api/cleanup-branch does and the confirm click names
        their count.

        404 when there is no worktree to remove -- unlike the discard,
        this route has nothing to do without one, and a silent 200 would
        read as "abandoned" for a task whose worktree someone else had
        already taken away."""
        try:
            body = self._read_json_body(max_bytes=DISCARD_BODY_MAX_BYTES)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return
        identity = self._discard_identity(body)
        if identity is None:
            return
        project, task_id = identity
        expected = self._expected_state_or_none(body)
        if expected is None:
            return

        with task_lifecycle_lock(project["name"], task_id):
            self._abandon_worktree_locked(project, task_id, *expected)

    def _abandon_worktree_locked(self, project, task_id, expected_tip, expected_paths):
        """The body of /api/abandon-worktree, under this task's lifecycle
        lock -- see _discard_attempt_locked for why it is split out."""
        import spawn  # local import: avoids a circular import at module load

        branch = spawn.branch_name(task_id)

        if self._refuse_discard_or_none(project, task_id, branch):
            return

        survey = self._survey_attempt(project, task_id)
        if not survey["worktreeExists"]:
            self._send_error_json(
                404, f"no worktree found for {project['name']}/{task_id}")
            return
        if survey["dirtyPaths"] is None:
            self._send_error_json(
                500, f"failed to check worktree status in {survey['worktreePath']}")
            return
        if self._refuse_stale_state_or_none(survey, expected_tip, expected_paths):
            return
        if self._refuse_discard_or_none(project, task_id, branch):
            return

        error = self._remove_worktree_or_error(project, survey["worktreePath"])
        if error is not None:
            self._send_error_json(500, error)
            return

        self._send_json(200, {
            "ok": True,
            "branch": branch,
            "branchKept": survey["branchExists"],
            "branchTip": survey["branchTip"],
            "baseBranch": survey["baseBranch"],
            "commitCount": survey["commitCount"],
            "worktreeRemoved": True,
            "discardedPaths": survey["dirtyPaths"],
        })

    def _handle_harvest_post(self):
        import harvest  # local import: avoids a circular import at module load

        try:
            body = self._read_json_body()
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "malformed request body")
            return

        project = body.get("project")
        task_id = body.get("taskId", body.get("task_id"))
        harvest_all = bool(body.get("all"))
        adopt_done = body.get("adoptDone", False)
        discard_main_task_edit = body.get("discardMainTaskEdit", False)

        if not isinstance(adopt_done, bool) or not isinstance(discard_main_task_edit, bool):
            self._send_error_json(400, "harvest action flags must be booleans")
            return
        if harvest_all and (adopt_done or discard_main_task_edit):
            self._send_error_json(400, "harvest actions require one taskId, not all")
            return

        try:
            if harvest_all:
                result = harvest.harvest_all_ready(self.config, project)
            else:
                action = {}
                if adopt_done:
                    action["adopt_done"] = True
                if discard_main_task_edit:
                    action["discard_main_task_edit"] = True
                result = harvest.harvest_branch(
                    self.config, project, task_id, **action
                )
        except harvest.HarvestError as exc:
            self._send_error_json(exc.status, str(exc))
            return

        self._send_json(200, result)

    def do_POST(self):
        try:
            # task-99/task-122: every refusal runs before any routing, so
            # a new endpoint is covered the moment it is added rather than
            # whenever someone remembers to guard it. The Host and
            # Sec-Fetch-Site rules inside _refuse_untrusted_request are
            # what refuse a POST that sends neither Origin nor Referer --
            # the shape a DNS-rebound page produces, which used to reach
            # /api/settings and /api/spawn.
            if self._refuse_untrusted_request():
                return
            if self._refuse_non_json_content_type():
                return

            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path

            if path == "/api/spawn":
                self._handle_spawn()
            elif path == "/api/resume":
                self._handle_resume()
            elif path == "/api/browser":
                self._handle_browser()
            elif path == "/api/harvest":
                self._handle_harvest_post()
            elif path == "/api/settings":
                self._handle_settings_post()
            elif path == "/api/agent-event":
                self._handle_agent_event(urllib.parse.parse_qs(parsed.query))
            elif path == "/api/end-session":
                self._handle_end_session()
            elif path == "/api/session-input":
                self._handle_session_input()
            elif path == "/api/cleanup-branch":
                self._handle_cleanup_branch()
            elif path == "/api/discard-attempt":
                self._handle_discard_attempt()
            elif path == "/api/abandon-worktree":
                self._handle_abandon_worktree()
            else:
                self._send_error_json(404, "not found")
        except Exception:  # pragma: no cover - defensive, never leak tracebacks
            try:
                self._send_error_json(500, "internal server error")
            except Exception:
                pass


class CentraleHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_cls, config):
        self.centrale_config = config
        super().__init__(server_address, handler_cls)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class _TerminateRequested(SystemExit):
    """Raised by install_terminate_handlers()'s SIGTERM/SIGINT handler so
    a signal unwinds through main()'s normal try/finally (running its own
    cleanup, and letting every atexit-registered hook -- browser.py's
    launched-child terminator among them -- run too), instead of the OS
    default action for SIGTERM: kill the process immediately, skipping
    all of that. That gap (atexit never running on SIGTERM) is why a
    `pkill`/`systemctl stop` orphaned every `backlog browser` child the
    server had launched -- this is the actual fix for it."""


def install_terminate_handlers():
    """Installs a handler for SIGTERM (and, for symmetry/robustness,
    SIGINT -- Python's own default SIGINT handler already raises
    KeyboardInterrupt, which main() already catches, but installing our
    own here means both signals are handled identically and explicitly)
    that raises _TerminateRequested instead of the OS default action. A
    small, separately-callable function -- rather than inlined in main()
    -- so tests can verify it registers the right handlers, and that
    those handlers actually raise, without ever touching real process
    signal state."""
    def _handle(signum, frame):
        raise _TerminateRequested()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def bind_failure_message(address, exc):
    """A clear, one-line explanation for main()'s startup bind failure --
    naming the port and the likely cause -- instead of a raw traceback.
    A small, separately-callable function (rather than inlined in
    main()) purely so a test can check its wording without needing to
    provoke a real bind failure inside main() itself."""
    return (
        f"Centrale: could not bind {address[0]}:{address[1]} ({exc}) -- "
        f"is another Centrale instance already running on this port?"
    )


# ---------------------------------------------------------------------------
# `python3 server.py --check` -- a doctor command a stranger following the
# README's quickstart runs first, before "python3 server.py", to catch a missing
# prerequisite with a clear line instead of a startup traceback or (worse)
# a silently half-working board.
# ---------------------------------------------------------------------------

DOCTOR_MIN_PYTHON = (3, 12)


def backlog_version(proc):
    """The plain X.Y.Z the `backlog` CLI reported, or None if this
    output does not carry one.

    `backlog --version` prints the bare number today ("1.51.0"), but
    that is a formatting detail of somebody else's CLI, not a contract:
    the first dotted number anywhere in the output is what is wanted,
    and anything without one -- a failed call, an empty answer, a future
    banner -- is None rather than a guess. Nothing downstream fails on a
    None; --check simply has one fewer thing to say (task-156).

    Shares its validation with _version_token() (task-162), the parser
    browser.version_drift() reads both sides of a drift comparison
    through, so a malformed answer on either side reads the same way."""
    if proc.returncode != 0:
        return None
    return _version_token((proc.stdout or "") + " " + (proc.stderr or ""))


def run_doctor_check(config_path=None):
    """Runs every prerequisite check --check reports, without starting a
    server or binding a port. Returns (lines, ok): `lines` is one
    already-formatted "[PASS]"/"[WARN]"/"[FAIL] <message>" string per
    check, in the order run; `ok` is False if any check FAILed (a WARN
    alone -- e.g. no tmux, a restricted bwrap userns, or one project
    missing backlog/config.yml -- still leaves ok True, matching how
    Centrale itself degrades: the server still starts, claude spawns are
    unaffected by a bwrap restriction, and the board still renders for
    every other project). Every external-tool check goes through the
    same injectable boundaries (`which`, `run_git`, `run_backlog_raw`,
    `run_bwrap_probe`, `apparmor_userns_restricted`) the rest of this
    module uses, so this is exactly as hermetically testable as anything
    else here despite shelling out for real when actually invoked."""
    lines = []
    ok = True

    def check(status, message):
        nonlocal ok
        lines.append(f"[{status}] {message}")
        if status == "FAIL":
            ok = False

    # task-107: first, before any prerequisite -- the one question a bug
    # report has to answer is "which build is this", and --check is the
    # command a stranger runs before anything else works. Resolved the
    # same way the running server resolves it, so the two agree.
    check("PASS", f"Centrale {detect_version()}")

    py_version = ".".join(str(v) for v in sys.version_info[:3])
    min_version = ".".join(str(v) for v in DOCTOR_MIN_PYTHON)
    if sys.version_info[:2] >= DOCTOR_MIN_PYTHON:
        check("PASS", f"Python {py_version} (>= {min_version} required)")
    else:
        check("FAIL", f"Python {py_version} is too old (>= {min_version} required)")

    if which("git") is None:
        check("FAIL", "git not found on PATH -- required for worktree management")
    else:
        proc = run_git(["--version"], cwd=None)
        # Not `version`: that name is the module holding the constants
        # the backlog check below reads.
        git_name = proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "git"
        check("PASS", f"{git_name} found on PATH")

    if which("backlog") is None:
        check(
            "FAIL",
            "backlog CLI not found on PATH -- required for all task data; "
            "install it with 'npm i -g backlog.md' (needs Node.js and npm), or "
            "see https://github.com/MrLesk/Backlog.md#installation for Bun, "
            "Homebrew and Nix",
        )
    else:
        proc = run_backlog_raw(["--version"], cwd=BASE_DIR)
        output = (proc.stdout or proc.stderr or "").strip()
        backlog_name = f"backlog {output}" if proc.returncode == 0 and output else "backlog"
        # task-156: Centrale is a veneer over this CLI, so which one is
        # installed is part of the answer to "why is the board showing
        # that". The tested baseline is stated here, next to the
        # installed version, so a reader has both numbers in one line
        # rather than one here and one in a README paragraph.
        #
        # ALWAYS a PASS, whatever the comparison says -- informational,
        # never a verdict. Upstream ships a minor every few days, so
        # being ahead of the baseline is the ordinary state of a
        # perfectly healthy machine within a fortnight of any release;
        # a WARN there would fire for nearly everyone, nearly always,
        # and teach its readers to skim past the WARNs that mean
        # something. Running a newer CLI is the user's call. This line
        # exists so that when something does behave oddly, the version
        # gap is in front of them already.
        installed = backlog_version(proc)
        baseline = version.TESTED_BACKLOG_VERSION
        if installed is None:
            detail = f" (this Centrale is tested against backlog {baseline})"
        elif installed == baseline:
            detail = " -- the version this Centrale is tested against"
        else:
            detail = (
                f" -- note: this Centrale is tested against {baseline}, you have "
                f"{installed}. That is fine and nothing to fix; if the board ever "
                f"misreads a task, check this gap first."
            )
        check("PASS", f"{backlog_name} found on PATH{detail}")

    if which("tmux") is None:
        check(
            "WARN",
            "tmux not found on PATH -- spawning is disabled, but the board, drawer, "
            "and open-board button still work (viewer-only); install it (e.g. "
            "sudo apt install tmux) and restart to enable spawning",
        )
    else:
        check("PASS", "tmux found on PATH")

    # codex's own sandboxing shells out to bwrap, which needs an
    # unprivileged user namespace -- on Ubuntu 24.04+, AppArmor can block
    # that for any process without a permissive profile, breaking codex
    # spawns while leaving claude ones untouched. Only meaningful on
    # Linux, and only worth probing at all if bwrap is even installed;
    # otherwise this is silently not applicable, not a warning.
    if sys.platform.startswith("linux") and which("bwrap") is not None:
        proc = run_bwrap_probe()
        if proc.returncode == 0:
            check("PASS", "bubblewrap sandbox works (unprivileged user namespaces available)")
        elif apparmor_userns_restricted():
            check(
                "WARN",
                "codex agents: bubblewrap sandbox blocked by AppArmor userns restriction -- "
                "see docs/operations.md troubleshooting",
            )
        else:
            detail = (proc.stderr or proc.stdout or "unknown error").strip()
            check(
                "WARN",
                f"codex agents: bubblewrap sandbox probe failed ({detail}) -- see docs/operations.md troubleshooting",
            )

    path = config_path if config_path is not None else DEFAULT_CONFIG_PATH
    try:
        config = load_config(path)
    except ConfigError as exc:
        check("FAIL", f"projects.json failed to load: {exc}")
        return lines, ok

    projects = config.get("projects", [])
    plural = "" if len(projects) == 1 else "s"
    if config.get("zeroConfig"):
        # task-53: no projects.json at all -- this is a first run, not a
        # problem, so it reads as guidance rather than the generic
        # "0 projects configured" phrasing a present-but-empty file
        # would also produce below.
        check(
            "PASS",
            "no projects.json found -- that's fine, this is a first run: starting "
            "with defaults. Add a project from the settings gear in the UI once the "
            "server is running, or copy projects.example.json to projects.json and "
            "edit its 'projects' to point at repos on this machine first.",
        )
    else:
        check("PASS", f"projects.json parses ({len(projects)} project{plural} configured)")
        if not projects:
            check("WARN", "no projects configured -- add one from the settings gear in the UI")

    for project in projects:
        name, proj_path = project["name"], project["path"]
        if not os.path.isdir(proj_path):
            check("WARN", f"{name}: path does not exist: {proj_path} -- will show as an error banner on the board")
            continue
        config_yml = os.path.join(proj_path, "backlog", "config.yml")
        if not os.path.isfile(config_yml):
            check("WARN", f"{name}: {proj_path} has no backlog/config.yml -- run 'backlog init' there first")
            continue
        check("PASS", f"{name}: {proj_path} exists and has backlog/config.yml")

    # task-128: the same comparison the board shows, for the terminal.
    # --check runs in a fresh process, so its own detect_version() IS the
    # checkout; the process that can be behind is the one already
    # serving the configured port, and only it knows what it loaded. So
    # ask it, and relay its own codeDrift rather than recomputing one
    # here: that server may run from a different checkout than this
    # command does (an agent's worktree, say), and the honest comparison
    # is the one between IT and ITS checkout. Nothing listening is not a
    # finding -- --check is documented as safe to run with or without a
    # server up -- so that case prints nothing at all.
    port = config.get("port")
    board = probe_running_server(port) if isinstance(port, int) else None
    if board is not None:
        drift = board.get("codeDrift", "absent")
        if drift == "absent":
            check(
                "WARN",
                f"the Centrale serving port {port} ({board['version']}) predates this "
                "check and cannot say whether it is behind its checkout -- restart it "
                "to be sure",
            )
        elif isinstance(drift, dict):
            later = drift.get("commitsBehind")
            suffix = (
                f", {later} commit{'' if later == 1 else 's'} later" if isinstance(later, int) else ""
            )
            check(
                "WARN",
                f"the Centrale serving port {port} started from {drift.get('loaded')} "
                f"but its checkout is now at {drift.get('current')}{suffix} -- the code "
                "has changed since that process started; restart it to load it",
            )
        else:
            check(
                "PASS",
                f"the Centrale serving port {port} ({board['version']}) is running its "
                "checkout's current code",
            )

    return lines, ok


def build_arg_parser():
    """The server.py CLI's argument parser (task-55): stdlib argparse, not
    hand-rolled `if "--check" in sys.argv[1:]` string matching -- that old
    approach silently ignored anything else on the command line, so a
    stranger's typo (or a --port/--config guess that doesn't exist -- config
    is projects.json, deliberately not a CLI flag, see run_doctor_check/
    load_config) would just run the server with defaults and no warning,
    exactly the kind of nasty first-run trap the zero-config work (task-53)
    was trying to eliminate elsewhere. Any unrecognized argument now hits
    argparse's own standard behavior: an error + usage message on stderr and
    exit code 2. A separate function (not inlined in main()) so tests can
    exercise parsing -- including the unknown-argument/exit-2 path -- via
    parse_args() directly, without going through main()'s own side effects
    (installing signal handlers, binding a port, ...)."""
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Centrale: a local dashboard aggregating Backlog.md boards across "
        "repos, with agent spawning and gated merging.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run startup diagnostics (Python/git/backlog/tmux, codex sandbox, "
        "projects.json and each configured project) and exit -- never binds a "
        "port or starts the server",
    )
    return parser


def main():
    args = build_arg_parser().parse_args()
    if args.check:
        lines, ok = run_doctor_check()
        for line in lines:
            print(line)
        raise SystemExit(0 if ok else 1)

    install_terminate_handlers()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Centrale: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if config.get("zeroConfig"):
        # task-53: no projects.json found -- not an error, just a first
        # run. The server still starts fully (empty board, sane
        # defaults); the UI's own empty-state panel carries the rest of
        # this guidance for anyone who missed it here.
        print(
            "Centrale: no projects.json found -- starting with defaults and an empty "
            "board. Add a project from the settings gear in the UI, or copy "
            "projects.example.json to projects.json and edit its 'projects' to "
            "point at repos on this machine.",
            file=sys.stderr,
        )
    configure_subprocess_timeout(config["subprocessTimeoutSeconds"])
    config["capabilities"] = detect_capabilities()
    # task-107: resolved HERE, once, and then only read back -- what the
    # footer and the API report is the build THIS process started from,
    # not whatever the checkout has become since.
    config["version"] = detect_version()
    if not config["capabilities"]["tmux"]:
        print(
            "Centrale: tmux not found on PATH -- spawning is disabled; "
            "the board, drawer, and open-board button still work.",
            file=sys.stderr,
        )

    import browser  # local import: avoids a circular import at module load

    # Heals after a SIGKILLed previous instance (the one case
    # install_terminate_handlers()/atexit can never catch): kills any
    # registry-recorded child that's still alive and still actually a
    # `backlog browser` process, drops every other stale entry. Silent
    # when there's nothing to report.
    sweep = browser.sweep_orphaned_browsers()
    if sweep["killed"] or sweep["stale"]:
        print(
            f"Centrale: boot sweep killed {len(sweep['killed'])} orphaned backlog browser "
            f"process(es), cleared {len(sweep['stale'])} stale registry entr"
            f"{'y' if len(sweep['stale']) == 1 else 'ies'}.",
            file=sys.stderr,
        )

    import harvest  # local import: avoids a circular import at module load

    # Always started, regardless of the initial harvest.mode -- it reads
    # the mode live from `config` every cycle and no-ops when it isn't
    # "auto", so a later settings change (task-20) takes effect on the
    # thread's very next cycle without restarting the server.
    auto_harvest_thread = harvest.start_auto_harvest_thread(config)

    address = ("127.0.0.1", config["port"])
    try:
        server = CentraleHTTPServer(address, Handler, config)
    except OSError as exc:
        print(bind_failure_message(address, exc), file=sys.stderr)
        auto_harvest_thread.stop()
        raise SystemExit(1) from exc
    # flush=True: stdout is fully block-buffered whenever it isn't a TTY
    # (redirected to a log file, piped, or under systemd/nohup) -- without
    # this, the one line confirming the server actually started can sit
    # unflushed in the buffer indefinitely, while the stderr diagnostics
    # above it (bind failure, tmux warning, boot sweep) appear immediately
    # since stderr is unbuffered.
    print(f"Centrale serving on http://{address[0]}:{address[1]}", flush=True)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, _TerminateRequested):
        pass
    finally:
        auto_harvest_thread.stop()
        server.server_close()


if __name__ == "__main__":
    main()
