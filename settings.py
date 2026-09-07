"""Read/write a whitelisted subset of projects.json for GET/POST
/api/settings.

Mirrors spawn.py/browser.py/harvest.py's pattern: imported lazily by
server.py (inside the request handler) to avoid a circular import, and
every access to server's shared constants goes through the `server`
module object at call time.

Python 3.12 stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile

import server

DEFAULT_REFRESH_INTERVAL_SECONDS = 10
MIN_REFRESH_INTERVAL_SECONDS = 5

# Filesystem/URL-safe project name: matches how project names already get
# used as path segments (worktree dirs) and tmux session-name components
# (spawn.py) -- letters, digits, '-', '_', '.', starting with a letter or
# digit so it never collides with a flag-like or hidden-file-like name.
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# task-78: an agent name is matched case-insensitively against a task's
# first assignee (spawn.resolve_agent) and exported as CENTRALE_AGENT=<name>
# into the spawned session, so it just has to be a single non-blank token
# -- no whitespace, not absurdly long. Anything stricter would refuse
# names an existing hand-edited projects.json already uses.
AGENT_NAME_RE = re.compile(r"^\S{1,64}$")


class SettingsError(Exception):
    """Raised for a /api/settings request-level failure that isn't a
    per-field validation problem (a malformed body shape, an I/O
    failure writing projects.json). `.status` is the HTTP status to
    respond with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status

    def __str__(self):
        return self.message


class ValidationError(Exception):
    """One or more whitelisted fields failed validation. `.fields` maps
    field name (dotted for per-project ones, e.g. "checkCommands.my-app")
    to a human-readable reason. Always a 400; nothing is written or
    applied to the live config when this is raised -- either every
    provided field is valid, or none of them take effect."""

    def __init__(self, fields):
        super().__init__("invalid settings: " + ", ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


def current_settings(config):
    """The current value of every whitelisted setting -- the GET
    /api/settings response, and the base of what a successful POST
    /api/settings returns."""
    check_commands = {p["name"]: p.get("checkCommand") for p in config.get("projects", [])}
    projects = [{"name": p["name"], "path": p.get("path", "")} for p in config.get("projects", [])]
    return {
        "harvestMode": (config.get("harvest") or {}).get("mode", "click"),
        "sessionPreviewMode": server.session_preview_mode(config),
        "refreshIntervalSeconds": config.get("refreshIntervalSeconds", DEFAULT_REFRESH_INTERVAL_SECONDS),
        "checkCommands": check_commands,
        "defaultAgent": config.get("defaultAgent") or server.DEFAULT_AGENT_NAME,
        "agents": sorted((config.get("agents") or {}).keys()),
        "agentEntries": agent_entries(config),
        "projects": projects,
    }


def agent_entries(config):
    """task-78: the configured agents map as an ordered list for the
    settings modal's Agents editor -- one entry per agent, in the map's
    own (projects.json) order: ``{"name", "cmd": [argv], "cmdText":
    shlex.join(argv), "promptSuffix": str | None, "builtin": bool,
    "onPath": bool}``.

    ``cmdText`` is the one-line, shell-quoted form the editor shows and
    accepts back (see _validate_agents): shlex.join/shlex.split are exact
    inverses, so an argv with spaces or quotes inside an argument
    round-trips through the text field unchanged. ``builtin`` flags the
    two names DEFAULT_AGENTS ships (the editor shows them as quiet,
    non-deletable, overridable rows). ``onPath`` is a PATH lookup of
    argv[0] through the injectable server.which boundary -- the "this
    command isn't installed" hint, informational only, never a reason to
    refuse anything."""
    entries = []
    for name, entry in (config.get("agents") or {}).items():
        cmd = list(entry["cmd"])
        entries.append({
            "name": name,
            "cmd": cmd,
            "cmdText": shlex.join(cmd),
            "promptSuffix": entry.get("promptSuffix"),
            "builtin": name in server.DEFAULT_AGENTS,
            "onPath": _executable_on_path(cmd[0]),
        })
    return entries


def _executable_on_path(executable):
    return server.which(os.path.expanduser(executable)) is not None


def _path_warnings(agents_map):
    """Human-readable warnings for every agent in `agents_map` whose
    argv[0] isn't found on PATH -- returned alongside a *successful*
    save (warn, never block: the user may be about to install it, or be
    editing a config for another machine)."""
    warnings = []
    for name, entry in agents_map.items():
        executable = entry["cmd"][0]
        if not _executable_on_path(executable):
            warnings.append(
                f"agent '{name}': '{executable}' was not found on PATH -- a spawn with it will fail "
                f"until it is installed or the command is fixed"
            )
    return warnings


def _validate_harvest_mode(value):
    if not isinstance(value, str) or value not in server.HARVEST_MODES:
        return f"must be one of {list(server.HARVEST_MODES)!r}"
    return None


def _validate_session_preview_mode(value):
    if not isinstance(value, str) or value not in server.SESSION_PREVIEW_MODES:
        return f"must be one of {list(server.SESSION_PREVIEW_MODES)!r}"
    return None


def _validate_refresh_interval(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be a whole number of seconds"
    if value < MIN_REFRESH_INTERVAL_SECONDS:
        return f"must be at least {MIN_REFRESH_INTERVAL_SECONDS}"
    return None


def _validate_check_command(value):
    # None (or a blank string) clears it -- "empty = no test gate", same
    # as load_config's own tolerance for a project with none configured.
    if value is None:
        return None
    if not isinstance(value, str):
        return "must be a string, or null/empty to clear it"
    return None


def _normalized_check_command(value):
    if isinstance(value, str) and value.strip():
        return value
    return None


def _validate_default_agent(value, config, agents_map=None):
    """`agents_map` (task-78) is the map an `agents` field in the same
    request would install -- the default has to name a key in the map
    that will be live after the save, not the one before it."""
    source = agents_map if agents_map is not None else (config.get("agents") or {})
    known = sorted(source.keys())
    if not isinstance(value, str) or value not in known:
        return f"must be one of {known!r}"
    return None


def _validate_agents(value, config):
    """task-78: validates the whole-map `agents` field -- an ordered list
    of ``{"name", "cmd", "promptSuffix"}`` objects, the settings modal's
    Agents editor rows -- and returns (fields, normalized_map).

    `fields` maps "agents.<index>.name" / "agents.<index>.cmd" /
    "agents.<index>.promptSuffix" (or "agents.<name>" / "agents" for
    map-level problems) to a reason; empty when everything is valid, in
    which case `normalized_map` is ``{name: {"cmd": [argv],
    "promptSuffix": str | None}}`` in submission order (the order the
    map is written in), else None.

    Rules: a name is required, one non-blank token (AGENT_NAME_RE), and
    unique case-insensitively (spawn.resolve_agent lowercases both sides,
    so "Codex" and "codex" would collide); `cmd` may be an argv list of
    strings or a single shell-quoted string (split with shlex, the exact
    inverse of the `cmdText` current_settings reports) and must be
    non-empty; `promptSuffix` is optional (null/blank means none); a
    built-in agent (DEFAULT_AGENTS) currently in the map cannot be
    dropped, only edited; and the map can't end up empty.

    Deliberately NOT a rule: whether argv[0] exists on PATH -- see
    _path_warnings."""
    if not isinstance(value, list):
        raise SettingsError("agents must be a list of {name, cmd, promptSuffix} objects")

    fields = {}
    seen = {}
    normalized = {}
    for index, entry in enumerate(value):
        key = f"agents.{index}"
        if not isinstance(entry, dict):
            fields[key] = "must be an object with name and cmd"
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            fields[f"{key}.name"] = "name is required"
            name = None
        else:
            name = name.strip()
            if not AGENT_NAME_RE.match(name):
                fields[f"{key}.name"] = "must be a single word of at most 64 characters (no spaces)"
                name = None
            elif name.lower() in seen:
                fields[f"{key}.name"] = (
                    f"duplicates agent '{seen[name.lower()]}' (names are matched case-insensitively)"
                )
                name = None
            else:
                seen[name.lower()] = name

        cmd_raw = entry.get("cmd")
        cmd = None
        if isinstance(cmd_raw, str):
            try:
                cmd = shlex.split(cmd_raw)
            except ValueError as exc:
                fields[f"{key}.cmd"] = f"could not parse the command: {exc}"
            else:
                if not cmd:
                    fields[f"{key}.cmd"] = "command is required"
                    cmd = None
        elif isinstance(cmd_raw, list):
            if not cmd_raw:
                fields[f"{key}.cmd"] = "command is required"
            elif not all(isinstance(v, str) for v in cmd_raw):
                fields[f"{key}.cmd"] = "must contain only strings"
            else:
                cmd = list(cmd_raw)
        else:
            fields[f"{key}.cmd"] = "must be a command string or a list of strings"

        suffix = entry.get("promptSuffix")
        if suffix is not None and not isinstance(suffix, str):
            fields[f"{key}.promptSuffix"] = "must be a string, or null/empty for none"
            suffix = None
        elif isinstance(suffix, str) and not suffix.strip():
            suffix = None

        if name is not None and cmd is not None:
            normalized[name] = {"cmd": cmd, "promptSuffix": suffix}

    if fields:
        return fields, None

    current = config.get("agents") or {}
    for builtin in server.DEFAULT_AGENTS:
        if builtin in current and builtin not in normalized:
            fields[f"agents.{builtin}"] = (
                f"built-in agent '{builtin}' cannot be removed (its command can be edited instead)"
            )
    if not normalized:
        fields["agents"] = "at least one agent is required"
    if fields:
        return fields, None
    return {}, normalized


def _live_sessions_for_project(name, config):
    """The live agent sessions a removal of `name` would strand, or none
    when tmux cannot be asked at all. An unanswerable question blocks
    nothing here: those sessions are untouched by a removal either way,
    and refusing on a failed probe would trade one dead end for a
    subtler one."""
    try:
        return server.live_sessions_for_project(name, config)
    except server.BacklogError:
        return []


def _unmerged_task_branches(project):
    """The project repo's `task/*` branches not merged into the branch
    its main checkout is on -- Centrale's own spawn branches with work
    still on them.

    One read-only `git for-each-ref`, through the same injectable
    boundary as everything else. A repo that cannot be read (path gone,
    no longer a git repo, git missing) yields none rather than an error:
    a project whose repo has vanished is exactly the one a user most
    needs to be able to remove."""
    import spawn  # local import: same reason server.py imports it lazily

    path = os.path.expanduser(project.get("path") or "")
    if not path or not os.path.isdir(path):
        return []
    base_branch = spawn.current_branch(path)
    proc = server.run_git(
        ["for-each-ref", "--format=%(refname:short)", "--no-merged", base_branch,
         "refs/heads/task"],
        cwd=path,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _validate_remove_project(value, config):
    """Returns an error string, or None if `value` names a project that's
    safe to remove.

    Removing a project deletes its `projects.json` entry and nothing
    else -- never the repo, never a branch, never a worktree -- so the
    question a guard here can honestly ask is not "would this destroy
    work" but "would this STRAND work": a removed project's live
    sessions and unmerged spawn branches go on existing with nothing
    left in the dashboard able to end, merge, discard or abandon them.
    Both refusals below therefore name what would be stranded and the
    action that clears it.

    Note what is deliberately NOT a reason: how many projects would be
    left. Until task-167 this refused to remove the last one, on the
    rationale that "the board must always have at least one configured
    project" -- true when it was written, and false since task-53 made a
    board with zero projects a supported, documented state with its own
    welcome panel and its own passing `--check`. A stranger who added
    their first project with a typo in the path had no way back out of
    the UI, which is the state the settings page exists to prevent."""
    projects = config.get("projects", [])
    known = {p["name"]: p for p in projects if p.get("name")}
    if not isinstance(value, str) or not value:
        return "must be a project name"
    if value not in known:
        return "unknown project"

    sessions = _live_sessions_for_project(value, config)
    if sessions:
        listed = ", ".join(sorted(sessions))
        one = len(sessions) == 1
        it = "it" if one else "them"
        subject = (
            f"a live agent session is still running in this project ({listed})"
            if one
            else f"{len(sessions)} live agent sessions are still running in this project ({listed})"
        )
        return (
            f"{subject} -- end {it} from the board first, or removing the project "
            f"leaves {it} running with nothing here able to reach it"
        )

    branches = _unmerged_task_branches(known[value])
    if branches:
        listed = ", ".join(sorted(branches))
        one = len(branches) == 1
        it = "it" if one else "them"
        subject = (
            f"an unmerged spawn branch is still open in this project ({listed})"
            if one
            else f"{len(branches)} unmerged spawn branches are still open in this project ({listed})"
        )
        return (
            f"{subject} -- merge, discard or abandon {it} from the board first, "
            f"or removing the project leaves that work with nothing here able to reach it"
        )
    return None


def _validate_add_project(value, config):
    """Returns (fields, normalized) -- `fields` maps "addProject.name" /
    "addProject.path" to a reason (empty dict if both are valid), and
    `normalized` is {"name", "path"} ready to write once every other
    field in the request has also validated (never touches the
    filesystem -- see _finalize_add_project for the backlog-init step,
    which only runs once we know the whole request will be applied)."""
    fields = {}
    if not isinstance(value, dict):
        return {"addProject": "must be an object with name and path"}, None

    name = value.get("name")
    path_raw = value.get("path")
    known_names = {p["name"] for p in config.get("projects", [])}

    if not isinstance(name, str) or not name.strip():
        fields["addProject.name"] = "name is required"
    else:
        name = name.strip()
        if not PROJECT_NAME_RE.match(name):
            fields["addProject.name"] = (
                "must contain only letters, numbers, '-', '_', '.', starting with a letter or number"
            )
        elif name in known_names:
            fields["addProject.name"] = "a project with this name already exists"

    if not isinstance(path_raw, str) or not path_raw.strip():
        fields["addProject.path"] = "path is required"
    else:
        path_raw = path_raw.strip()
        expanded = os.path.expanduser(path_raw)
        if not os.path.isdir(expanded):
            fields["addProject.path"] = f"path does not exist: {path_raw}"
        else:
            proc = server.run_git(["rev-parse", "--is-inside-work-tree"], cwd=expanded)
            if proc.returncode != 0 or proc.stdout.strip() != "true":
                fields["addProject.path"] = f"not a git repository: {path_raw}"

    if fields:
        return fields, None

    return {}, {
        "name": name,
        "path": path_raw,
        "initBacklog": bool(value.get("initBacklog")),
    }


def _finalize_add_project(add_project):
    """Runs once every field in the request is otherwise known-valid, right
    before the write: if the target repo has no backlog/config.yml, either
    refuses (returning an error string) or runs `backlog init` through the
    same injectable run_backlog_raw boundary every other write command
    uses, depending on the caller-supplied initBacklog flag. Returns None
    on success."""
    expanded = os.path.expanduser(add_project["path"])
    config_yml = os.path.join(expanded, "backlog", "config.yml")
    if os.path.isfile(config_yml):
        return None
    if not add_project["initBacklog"]:
        return (
            "this repo has no backlog/config.yml -- tick \"Initialize Backlog.md in this repo\" "
            "to run 'backlog init' here, or initialize it yourself first"
        )
    proc = server.run_backlog_raw(
        ["init", add_project["name"], "--defaults", "--integration-mode", "cli", "--agent-instructions", "claude,agents"],
        cwd=expanded,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "backlog init failed").strip()
        return f"backlog init failed: {message}"
    return None


def apply_settings(config, body, path=None):
    """Validates `body` (a partial /api/settings POST payload -- any of
    "harvestMode", "refreshIntervalSeconds", "checkCommands" may be
    omitted, leaving that setting untouched) against the whitelist.

    Only once every *provided* field is valid does this update `config`
    in place (so the change takes effect immediately, no restart) and
    rewrite `path` (projects.json by default; overridable so tests never
    touch the real file) atomically (temp file + os.replace): the file
    is read fresh and only the whitelisted keys are ever touched, so
    every other key -- and every other project field -- round-trips
    through json load/dump untouched.

    "sessionPreviewMode" ("interact" | "view" | "off" -- the tiered
    task-60/61 drawer live-pane + reply knob, stored as projects.json's
    sessionPreview.mode),
    "defaultAgent" (must name a key in the configured agents map -- or,
    when "agents" is in the same request, in the map that request
    installs),
    "agents" (task-78: the WHOLE agents map as an ordered list of
    {"name", "cmd", "promptSuffix"} -- see _validate_agents; the
    current default agent must still be in it, or the same request must
    pick a new one; the response additionally carries "warnings", one
    line per saved agent whose executable isn't on PATH),
    "removeProject" (a project name -- removes that entry from
    projects.json only, never touches the repo itself; refused only
    while that project has a live session or an unmerged spawn branch
    the removal would strand, task-167), and "addProject" ({"name", "path",
    "initBacklog": bool}) are also whitelisted -- see
    _validate_default_agent / _validate_remove_project /
    _validate_add_project for their rules.

    Raises ValidationError, naming every invalid field at once, for bad
    values. Raises SettingsError for a structurally malformed body (e.g.
    "checkCommands" present but not an object) or a write failure.
    Never partially applies: on any error, nothing in `config` or on
    disk changes.

    Returns current_settings(config) after applying.
    """
    if path is None:
        path = server.DEFAULT_CONFIG_PATH
    if not isinstance(body, dict):
        raise SettingsError("request body must be a JSON object")

    fields = {}

    harvest_mode = body.get("harvestMode")
    if "harvestMode" in body:
        err = _validate_harvest_mode(harvest_mode)
        if err:
            fields["harvestMode"] = err

    session_preview_mode = body.get("sessionPreviewMode")
    if "sessionPreviewMode" in body:
        err = _validate_session_preview_mode(session_preview_mode)
        if err:
            fields["sessionPreviewMode"] = err

    refresh_interval = body.get("refreshIntervalSeconds")
    if "refreshIntervalSeconds" in body:
        err = _validate_refresh_interval(refresh_interval)
        if err:
            fields["refreshIntervalSeconds"] = err

    check_commands = body.get("checkCommands")
    if "checkCommands" in body:
        if not isinstance(check_commands, dict):
            raise SettingsError("checkCommands must be an object mapping project name to command")
        known_names = {p["name"] for p in config.get("projects", [])}
        for name, value in check_commands.items():
            if not isinstance(name, str) or name not in known_names:
                fields[f"checkCommands.{name}"] = "unknown project"
                continue
            err = _validate_check_command(value)
            if err:
                fields[f"checkCommands.{name}"] = err

    agents_map = None
    if "agents" in body:
        agent_fields, agents_map = _validate_agents(body.get("agents"), config)
        fields.update(agent_fields)

    default_agent = body.get("defaultAgent")
    if "defaultAgent" in body:
        err = _validate_default_agent(default_agent, config, agents_map)
        if err:
            fields["defaultAgent"] = err
    elif agents_map is not None:
        # The map is changing under the existing default: it has to
        # survive, or this request has to name a replacement -- never
        # write a config whose default points at nothing.
        effective_default = config.get("defaultAgent") or server.DEFAULT_AGENT_NAME
        err = _validate_default_agent(effective_default, config, agents_map)
        if err:
            fields["defaultAgent"] = (
                f"'{effective_default}' is the default agent and would no longer exist -- "
                f"pick another default in the same save ({err})"
            )

    remove_project = body.get("removeProject")
    if "removeProject" in body:
        err = _validate_remove_project(remove_project, config)
        if err:
            fields["removeProject"] = err

    add_project = None
    if "addProject" in body:
        add_fields, add_project = _validate_add_project(body.get("addProject"), config)
        fields.update(add_fields)

    if fields:
        raise ValidationError(fields)

    # addProject's backlog-init step is a real side effect (it can run
    # `backlog init` against the target repo), so it only happens once
    # every other field in the request is already known-valid -- keeping
    # the "nothing changes unless the whole request is valid" guarantee
    # for everything that's just a config write.
    if add_project is not None:
        err = _finalize_add_project(add_project)
        if err:
            raise ValidationError({"addProject.path": err})

    _write_whitelisted_changes(
        path, body, harvest_mode, refresh_interval, check_commands,
        default_agent, remove_project, add_project, session_preview_mode,
        agents_map,
    )
    _apply_to_live_config(
        config, body, harvest_mode, refresh_interval, check_commands,
        default_agent, remove_project, add_project, session_preview_mode,
        agents_map,
    )

    result = current_settings(config)
    result["warnings"] = _path_warnings(agents_map) if agents_map is not None else []
    return result


def _write_whitelisted_changes(
    path, body, harvest_mode, refresh_interval, check_commands,
    default_agent, remove_project, add_project, session_preview_mode=None,
    agents_map=None,
):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"failed to read projects.json: {exc}", status=500) from exc
    if not isinstance(raw, dict):
        raw = {}

    if "harvestMode" in body:
        raw_harvest = raw.get("harvest")
        raw["harvest"] = dict(raw_harvest) if isinstance(raw_harvest, dict) else {}
        raw["harvest"]["mode"] = harvest_mode

    if "sessionPreviewMode" in body:
        raw_preview = raw.get("sessionPreview")
        raw["sessionPreview"] = dict(raw_preview) if isinstance(raw_preview, dict) else {}
        raw["sessionPreview"]["mode"] = session_preview_mode

    if "refreshIntervalSeconds" in body:
        raw["refreshIntervalSeconds"] = refresh_interval

    if "defaultAgent" in body:
        raw["defaultAgent"] = default_agent

    if agents_map is not None:
        raw_agents = raw.get("agents")
        raw["agents"] = _merge_agents_into_raw(raw_agents if isinstance(raw_agents, dict) else {}, agents_map)

    if "removeProject" in body:
        raw_projects = raw.get("projects")
        if not isinstance(raw_projects, list):
            raw_projects = []
        raw["projects"] = [
            entry for entry in raw_projects
            if not (isinstance(entry, dict) and entry.get("name") == remove_project)
        ]

    if "checkCommands" in body:
        raw_projects = raw.get("projects")
        if not isinstance(raw_projects, list):
            raw_projects = []
        for entry in raw_projects:
            if not (isinstance(entry, dict) and isinstance(entry.get("name"), str)):
                continue
            if entry["name"] not in check_commands:
                continue
            normalized = _normalized_check_command(check_commands[entry["name"]])
            if normalized is None:
                entry.pop("checkCommand", None)
            else:
                entry["checkCommand"] = normalized
        raw["projects"] = raw_projects

    if add_project is not None:
        raw_projects = raw.get("projects")
        if not isinstance(raw_projects, list):
            raw_projects = []
        raw_projects.append({"name": add_project["name"], "path": add_project["path"]})
        raw["projects"] = raw_projects

    try:
        _atomic_write_json(path, raw)
    except OSError as exc:
        raise SettingsError(f"failed to write projects.json: {exc}", status=500) from exc


def _merge_agents_into_raw(raw_agents, agents_map):
    """The projects.json 'agents' value to write for `agents_map` (the
    validated submission, in order). Each entry keeps the file's own
    spelling where it can: an existing object entry is updated in place
    (only its "cmd" and "promptSuffix" change, so a hand-written
    "resumeCmd" or any other key survives an editor save untouched); an
    existing plain-list entry, or a new agent, is written as the plain
    argv list unless it has a promptSuffix, which only the object form
    can carry. Agents missing from the submission are dropped -- that is
    how the editor removes one."""
    merged = {}
    for name, entry in agents_map.items():
        existing = raw_agents.get(name)
        if isinstance(existing, dict):
            updated = dict(existing)
            updated["cmd"] = list(entry["cmd"])
            if entry["promptSuffix"] is None:
                updated.pop("promptSuffix", None)
            else:
                updated["promptSuffix"] = entry["promptSuffix"]
            merged[name] = updated
        elif entry["promptSuffix"] is None:
            merged[name] = list(entry["cmd"])
        else:
            merged[name] = {"cmd": list(entry["cmd"]), "promptSuffix": entry["promptSuffix"]}
    return merged


def _apply_to_live_config(
    config, body, harvest_mode, refresh_interval, check_commands,
    default_agent, remove_project, add_project, session_preview_mode=None,
    agents_map=None,
):
    if agents_map is not None:
        # Same canonical shape server.normalize_agents_map produces, so
        # spawn.resolve_agent picks the new map up on the very next spawn
        # -- no restart. A resumeCmd the live entry already carried
        # (from the file at startup) is kept, mirroring the on-disk merge.
        previous = config.get("agents") or {}
        config["agents"] = {
            name: {
                "cmd": list(entry["cmd"]),
                "promptSuffix": entry["promptSuffix"],
                "resumeCmd": (previous.get(name) or {}).get("resumeCmd"),
            }
            for name, entry in agents_map.items()
        }
    if "harvestMode" in body:
        config["harvest"] = {"mode": harvest_mode}
    if "sessionPreviewMode" in body:
        config["sessionPreview"] = {"mode": session_preview_mode}
    if "refreshIntervalSeconds" in body:
        config["refreshIntervalSeconds"] = refresh_interval
    if "defaultAgent" in body:
        config["defaultAgent"] = default_agent
    if "removeProject" in body:
        config["projects"] = [p for p in config.get("projects", []) if p["name"] != remove_project]
    if "checkCommands" in body:
        for project in config.get("projects", []):
            if project["name"] in check_commands:
                project["checkCommand"] = _normalized_check_command(check_commands[project["name"]])
    if add_project is not None:
        config.setdefault("projects", []).append({
            "name": add_project["name"],
            "path": os.path.expanduser(add_project["path"]),
            "browserPort": None,
            "checkCommand": None,
            "checkTimeoutSeconds": server.DEFAULT_CHECK_TIMEOUT_SECONDS,
        })


def _atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".projects-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
