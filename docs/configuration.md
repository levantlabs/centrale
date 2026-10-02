# Centrale configuration

The `projects.json` reference — every key, the `agents` map, the
`spawnPrompt` override, `worktreeRoot`/`@repo` — and the Settings view
that edits a whitelisted subset of it live. Part of the Centrale manual;
the [README](../README.md) is the front door and lists the other chapters.

## Setup / configuration

Centrale reads `projects.json` next to `server.py` at startup. **A fresh clone
does not contain one** — `projects.json` is gitignored, so it is yours to
create, and there are two ways to get there. It is the only file Centrale
itself reads; the release script has a config file of its own
(`.release-remote`: `RELEASE_REMOTE`, `RELEASE_AUTHOR_NAME`,
`RELEASE_AUTHOR_EMAIL`, `RELEASE_BRANCH`, `RELEASE_PRIVATE_NAMES`), which
the server never touches — see "Releasing" in
[docs/operations.md](operations.md#releasing).

### The zero-config first run

You do not have to write a config file at all. With no `projects.json` present,
Centrale boots on built-in defaults (port `7420`, `worktreeRoot` `"@repo"`,
`defaultAgent` `claude`, the `claude`/`codex` agents map, no projects) and
serves an empty board that points at the settings gear:

```bash
python3 server.py
```

Both `--check` and the startup line say so explicitly — a missing
`projects.json` is reported as a first run, not a failure. Adding your first
project from the gear icon writes the file for you, with only the keys you
actually set; everything else keeps following the defaults. If that is how you
started, the rest of this chapter is a reference for the keys the Settings view
does not expose rather than something you have to do up front.

### Starting from the shipped example

If you would rather write the file yourself, copy the shipped
[`projects.example.json`](../projects.example.json) and then **edit it**:

```bash
cp projects.example.json projects.json
$EDITOR projects.json
```

The edit is the point of the copy. The example's two entries — `~/code/my-app`
and `~/code/my-lib` — are placeholders that show the shape; unless those paths
happen to exist on your machine, running with them unedited gives you a board
of two error banners rather than columns. Point `projects` at repos that exist
(or empty the list and add them from the gear icon instead).

It looks like this:

```json
{
  "port": 7420,
  "worktreeRoot": "@repo",
  "browserPortBase": 6421,
  "defaultAgent": "claude",
  "agents": {
    "claude": ["claude"],
    "codex": ["codex"]
  },
  "projects": [
    {"name": "my-app", "path": "~/code/my-app"},
    {"name": "my-lib", "path": "~/code/my-lib", "checkCommand": "python3 -m unittest discover tests"}
  ]
}
```

Every key in it is optional, `projects` included: a file of just `{}` loads
on the same defaults the zero-config run uses. Anything you omit follows the
default named under that field below.

Fields:

- `port` — the TCP port the server listens on. Defaults to `7420` if omitted
  or not an integer.
- `worktreeRoot` — where spawned-agent worktrees are created (see "Spawning
  an agent" in [docs/agents.md](agents.md#spawning-an-agent)): `<worktreeRoot>/<project>-<taskid-lower>`. Two forms:
  - **`"@repo"`** (the default — both the shipped config's explicit value
    and what missing/omitted resolves to) — each project's worktrees live
    *inside* that project, at `<project path>/.centrale-worktrees/`, instead
    of one shared directory outside every repo. This exists specifically
    for **codex**: its own sandboxing trusts a directory only if it's a
    filesystem descendant of a repo you've already told it to trust: a
    worktree at a shared sibling path (the old default, below) is
    untrusted, so a spawned codex agent runs there under codex's strictest
    sandbox — which, combined with an AppArmor user-namespace restriction
    some distros ship (see "Troubleshooting" in [docs/operations.md](operations.md#troubleshooting)), can fail to start at
    all. An in-repo worktree inherits the parent repo's trust instead.
    `claude` spawns are unaffected either way. Centrale adds
    `.centrale-worktrees/` to that repo's `.git/info/exclude` the first
    time it creates one there (untracked-only — never touches the tracked
    `.gitignore` — see "Cleanup" in [docs/operations.md](operations.md#cleanup)), so it never shows up in `git
    status`.
  - **A plain path** (e.g. `~/code/.centrale-worktrees`) — one shared
    directory outside every project instead. `~` is expanded. Use this if
    you don't spawn codex, or don't need its directory-trust behavior.

  Also settable per-project — a `"worktreeRoot"` field on an entry in
  `projects` (either form, above) overrides the global one for that
  project only; omit it to just follow the global setting. The key being
  **absent entirely** — at either level — defaults to `"@repo"`; there is
  no other implicit default to fall back to.
- `browserPortBase` — the base TCP port used to assign each project its own
  `backlog browser` instance (see "Opening a project's Backlog.md board"
  in [docs/board.md](board.md#opening-a-projects-backlogmd-board)). Defaults to `6421` if omitted or not an integer. Project *N* (by
  position in `projects`, 0-indexed) gets `browserPortBase + N`, unless that
  project sets its own `browserPort`.
- `defaultAgent` — the agent name to spawn when a task has no assignee, or an
  assignee that doesn't match any key in `agents` — but only while
  `requireAgentAssignment` is `false`; with it on, such a spawn is refused
  instead. Defaults to `"claude"`. (Resume still falls back to it: it
  relaunches work already under way.)
- `requireAgentAssignment` — whether a spawn refuses to guess the agent
  (task-171). `true` (the default, and what any value other than `false`
  means): a task whose first assignee is missing, a person, or a typo — no
  key in `agents` — is refused with a 409 naming the task, its assignee and
  the configured agents, and the spawn control offers a picker of agents
  instead. `false`: such a spawn launches `defaultAgent` and the response
  carries a warning saying so, which the board shows as a toast. Either way
  an agent chosen explicitly (the picker, or `POST /api/spawn`'s `agent`
  field) wins over the assignee, and when the agent that ran did not come
  from the assignee the spawn's claim commit records it as the task's first
  assignee, keeping any person already assigned behind it (see
  [`POST /api/spawn`](api.md#post-apispawn)). Editable in Settings.
- `lockSpawnedTaskFiles` — whether a spawn makes the main checkout's copy
  of the task's own file read-only while the task is spawned (task-172),
  so the agent on its branch is that file's only writer and a ruling goes
  through `POST /api/rule` instead (see "Ruling on a spawned task" in
  [docs/agents.md](agents.md#ruling-on-a-spawned-task)). `true` (the
  default, and what any value other than `false` means); `false`: spawn
  behaves exactly as it did without the lock. Existing locks are kept
  either way — a merge, discard, abandon or cleanup still releases them,
  and startup releases one with no task branch behind it. Editable in
  Settings.
- `agents` — a map of agent name to command, used to pick which CLI to spawn
  based on a task's assignee (see "Agent selection" in [docs/agents.md](agents.md#agent-selection)). Defaults to
  `{"claude": ["claude"], "codex": ["codex"]}` if omitted or an empty object.
  Each entry can be given in either of two forms:
  - a plain argv list, e.g. `"codex": ["codex"]`;
  - an object, e.g.
    ```json
    "layout-bot": {
      "cmd": ["claude", "--append-system-prompt", "You are a layout specialist."],
      "promptSuffix": "Prefer the layout-reviewer subagent for any layout changes.",
      "resumeCmd": ["claude", "--continue"]
    }
    ```
    where `cmd` is the argv (required, a non-empty list of strings),
    `promptSuffix` (optional) is a string appended, after a blank line, to
    the standard Backlog.md workflow prompt before it's passed as the
    trailing argument, and `resumeCmd` (optional, object form only) is the
    argv used instead of `cmd` to resume this agent's interrupted session
    (see "Resuming an interrupted agent" in
    [docs/agents.md](agents.md#resuming-an-interrupted-agent)). A plain
    Resume runs it with no prompt argument appended — the resumed
    conversation already has its context. **"Resume to reconcile"** runs
    the same argv with exactly **one trailing argument**, the reconcile
    prompt (see "Resume to reconcile" in
    [docs/agents.md](agents.md#resume-to-reconcile-a-branch-that-fell-behind-main)),
    so a wrapper script configured here must tolerate one extra argument
    the way `claude --continue [prompt]` and `codex resume <UUID>
    [prompt]` already do. This is how you point an assignee at a wrapper script,
    `claude`/`codex` with extra flags or a custom system prompt, or a
    prompt that steers the agent toward a repo-defined `.claude/agents`
    subagent — all purely through config, no code changes needed.

    **Codex exception (task-195):** Resume selects an explicit UUID from
    `$CODEX_HOME/sessions` (default `~/.codex/sessions`) for the exact task
    worktree, or starts fresh with the prior-work note. A Codex `resumeCmd`
    returns 409; remove it to use verified selection. Session/directory
    overrides in `cmd` also refuse on Resume. Ordinary model/config/approval
    arguments in `cmd` are preserved. These records are read only; Centrale
    adds no cache or session registry.

    Both forms are normalized to one internal shape when Centrale starts
    (`server.load_config`), so a malformed entry — a missing, empty, or
    non-list `cmd`; a `cmd` containing something other than strings; a
    `promptSuffix` that isn't a string; or a `resumeCmd` that's present but
    not a non-empty list of strings — is rejected **at startup** with a
    clear error naming the offending agent and field (e.g. `projects.json:
    agents.layout-bot.cmd must be a non-empty list of strings`), rather than
    being silently dropped or misspawning at request time. Fix the entry
    and restart the server.

    You don't have to edit this map by hand: the Settings view's
    **Agents** section (see "Settings" below) adds, edits and removes
    entries in this same map, applied live — a spawn right after saving
    already uses the new command, no restart. Hand-editing stays the
    power-user path (it's the only way to set `resumeCmd`, per-agent
    environment variables via a wrapper script, and so on).
- `harvest` — controls how finished branches get merged (see "Merging
  finished branches" in [docs/merging.md](merging.md#merging-finished-branches); the config key and API keep the name `harvest`,
  but everything you see in the UI calls it Merge). An object with one
  field:
  ```json
  "harvest": {"mode": "click"}
  ```
  `mode` is either `"click"` (default — merging only ever happens when
  you press a Merge button) or `"auto"` (a background thread additionally
  evaluates every branch's five gates on its own, roughly every 30 seconds,
  and merges any that are all-green — through the exact same gate-evaluation
  and merge code a click would use, no separate logic). Defaults to
  `{"mode": "click"}` if the key is omitted entirely. A malformed value — not
  an object, or a `mode` other than `"click"`/`"auto"` — is rejected at
  startup the same way a malformed `agents` entry is. Switching modes only
  requires editing this file and restarting the server if you're doing it
  by hand; the gear icon's Settings view (see "Settings" below) changes it
  live, no restart needed.
- `sessionPreview` (optional) — controls the drawer's live session pane
  (see "Live session pane" under "Using the board" in [docs/board.md](board.md#using-the-board)). An object with one
  field:
  ```json
  "sessionPreview": {"mode": "interact"}
  ```
  `mode` is one of three tiers: `"interact"` (default — the drawer of a
  task with a live session shows a read-only, continuously refreshing
  capture of the agent's tmux pane *plus* a reply row: a single-line text
  box and an Esc / Enter key button pair), `"view"`
  (the pane only — the reply row is absent *and* `POST /api/session-input`
  refuses with a 403), or `"off"` (the drawer section is absent *and* `GET
  /api/session-pane` refuses too — the whole feature is disabled end to
  end, not just hidden). Defaults to `{"mode": "interact"}` if omitted; a
  malformed value is rejected at startup the same way `harvest` is. One
  tiered key rather than two booleans, so the reply can be switched off
  independently of the read-only pane without a second setting to forget.
  A `projects.json` that already says `"view"` (written by the settings
  modal before the reply tier existed) stays preview-only until you flip
  the reply toggle. The gear icon's Settings view changes it live.
- `refreshIntervalSeconds` (optional) — how often, in seconds, the board
  auto-refreshes in the browser. Defaults to `10` if omitted; must be a
  whole number `>= 5` if present (a malformed value is rejected at startup,
  same style as `harvest`). Purely client-side — the server doesn't act on
  a timer for this, it just tells the frontend what interval to use.
- `parkedAfterSeconds` (optional) — how long, in seconds, an agent must sit
  idle (hook state finished/idle, or no hook yet) before a dialog on its pane
  or an undelivered message flags it PARKED. Defaults to `180`; a whole number
  `>= 10` if present (malformed values are rejected at startup). Read on every
  poll from the live config; a restart is needed after editing the file.
- `subprocessTimeoutSeconds` (optional) — the timeout every `backlog`/`git`/
  `tmux` subprocess call shares (one knob for `run_backlog`, `run_backlog_raw`,
  `run_git`, `run_tmux`, not four separate ones). Defaults to `30` seconds
  if omitted; must be a positive number if present (a malformed value is
  rejected at startup, same style as `harvest`/`refreshIntervalSeconds`). A
  hung external CLI call can never freeze board rendering, a spawn, or a
  merge — it just fails that one call the same way a non-zero exit would
  (see `checkTimeoutSeconds` below for the one deliberate exception, which
  needs much more headroom).
- `statusPage` (optional) — the opt-in, read-only status page for checking
  on agents from a phone (see "Checking status from a phone" in
  [docs/operations.md](operations.md#checking-status-from-a-phone), which
  has the security warning and setup recipes). An object:
  `{"enabled": true, "port": 7421}`, plus an optional `"bind"`. Off when
  omitted. `enabled` (boolean, default `false`) starts a separate HTTP
  listener that serves only that page, its data and its static files;
  `port` (default `7421`, must differ from `port`) is where; it listens on
  every network interface unless `bind` names one interface (`"tailscale0"`)
  or one address (`"127.0.0.1"`). There is no key here: Centrale generates
  one the first time the page is on and keeps it in its state directory
  (`$XDG_STATE_HOME/centrale/status-page.key`, overridable with
  `CENTRALE_STATUS_KEY_FILE`), and every request must carry it. A malformed
  value is rejected at startup; a port that cannot be listened on is not --
  Centrale starts without the page and says why. The Settings view changes
  it live.
- `spawnPrompt` (optional) — the prompt template every spawned agent is
  given (see "Spawning an agent" in [docs/agents.md](agents.md#spawning-an-agent) for the built-in text). Omit it to
  use the built-in default, which is what you want unless you have a
  reason to change what agents are told: the default is the product's
  coordination layer (the Backlog.md workflow, the standing-decisions
  check, the no-self-merge rule), and an omitted key tracks future
  improvements to it automatically, while a present one freezes your copy.
  The shipped `projects.example.json` deliberately omits the key for that
  reason — add it only when you actually want to override the text, and
  delete it again to go back to following the built-in default. Rules,
  checked **at startup** the same way a malformed `agents` entry is: the
  value must be a non-empty string and
  must contain the `{task_id}` placeholder (replaced with the task's id,
  e.g. `TASK-2`, at spawn time); any other `{placeholder}` or an unbalanced
  brace is rejected too (write a literal brace as `{{` or `}}`), so a
  template can never blow up at spawn time instead. The string is used
  verbatim — nothing from the default is added back — and a per-agent
  `promptSuffix` (see `agents` above) still appends to it exactly as it
  does to the default. It applies to a spawn and to the fresh-start
  fallback of a resume; the "Resume to reconcile" prompt ([docs/agents.md](agents.md#resume-to-reconcile-a-branch-that-fell-behind-main)) is separate and
  not affected.
- `projects` — a list of entries, one per repo to aggregate:
  - `name` — the label used everywhere in the UI and API (project chips,
    worktree/branch/session names). Entries missing a `name` are skipped.
  - `path` — the repo's working directory, with `~` expanded.
  - `browserPort` (optional) — overrides the `browserPortBase`-derived port
    for this project's `backlog browser` instance.
  - `checkCommand` (optional) — a shell-style command string (e.g.
    `"python3 -m unittest discover tests"`), shlex-split into argv and run
    against the *merged* tree in a temporary worktree as the last of the
    five safety gates (see "Merging finished branches" in [docs/merging.md](merging.md#merging-finished-branches)). A project
    without one skips that gate — it passes vacuously,
    the same way a task with no dependencies is trivially unblocked.
  - `maxAgents` (optional) — a positive integer limiting this project's live
    Centrale tmux sessions. Omitted or `null` means no cap. Each spawn,
    re-spawn and resume counts tmux sessions again before claiming or
    creating anything; at the cap it returns 409 naming the live sessions.
    Launches in the same server process are serialized per project so two
    requests cannot both take the last slot. Agent lifecycle badges do not
    affect the count: an idle or finished agent still occupies its session.
    The browser does not pre-check the cap: the launch controls stay enabled
    and a launch at the cap is refused by the server with that 409, shown as
    the error. The [Fleet view](board.md#views) shows used/max slots per
    project. Other projects have independent caps. Settings applies a changed cap to the
    next launch; lowering it does not stop existing sessions.
  - `worktreeLinks` (optional) — an array of literal repo-relative paths,
    e.g. `[".venv", "local/data"]`. Omitted, `null` or `[]` means no links.
    Each **new** worktree receives symlinks to these paths in the configured
    main checkout. Existing worktrees are reused unchanged. The contents
    are shared: writing through a link writes to the source. Removing the
    worktree during discard, abandon, cleanup or harvest removes the link,
    preserving the source.

    Centrale first adds root-anchored entries such as `/.venv` (without a
    trailing slash) to the repo's shared `.git/info/exclude`, and verifies
    that git ignores the destination, keeping links out of ordinary
    `git add`/`git add -A`. Do not force-add these links. Missing sources
    produce spawn/resume warnings and are skipped. Tracked or existing
    destinations, symlinked destination parents, exclusion failures and
    overriding `.gitignore` rules also warn and skip the link. Exclusions
    remain when links or settings are removed.

    Paths must be canonical and non-overlapping: no absolute paths, empty
    components, `.`/`..`/`.git` components, leading/trailing whitespace,
    backslashes, ASCII control characters, or pattern characters `*?[]!#`.
    Malformed `maxAgents` or `worktreeLinks` values raise a configuration
    error at startup and a field error in Settings. Both keys belong on a
    `projects` entry next to `checkCommand`, not in the target repo itself:

    ```json
    {"name": "my-app", "path": "~/code/my-app", "maxAgents": 4, "worktreeLinks": [".venv"]}
    ```
  - `checkTimeoutSeconds` (optional) — how long `checkCommand` is allowed to
    run before it's killed and treated as a failed gate 5 ("checkCommand
    timed out after Xs", not a hung request). Defaults to `600` (10 minutes
    — a real test suite can legitimately take a while) if omitted; a
    malformed value (not a positive number) is quietly ignored and falls
    back to that same default, the same lenient style `browserPort`/
    `checkCommand` already use for a per-project field, rather than
    refusing to start the whole server the way a malformed top-level key
    does.

To add or remove a repo from the board, edit this list and restart the
server — refreshing the board is not enough, since `projects.json` is only
read at process start. (The Settings view's own Add/Remove, below, writes
the same list *and* applies it to the running server, so that route needs
no restart.)

### Migrating `worktreeRoot` to `@repo`

Switching an existing config's `worktreeRoot` from a plain path to `"@repo"`
(or the reverse) changes *where Centrale looks* for a task's worktree, not
where any existing worktree actually is — nothing is moved automatically.
Since that lookup only follows whatever's in `projects.json` at the next
restart (see "Restarting after a change" in [docs/operations.md](operations.md#restarting-after-a-change)), any worktree created under
the *old* root stops being Centrale's the moment you restart with the new
setting. The branch itself is unaffected — the card keeps its "unmerged
branch" state, which is read from git's refs, not from any directory — but
git still lists that worktree at a path Centrale no longer expects, so the
task now reads as a branch checked out *outside* Centrale (see "Branches
worked outside Centrale" in [docs/agents.md](agents.md#branches-worked-outside-centrale)): the card gets the
"worked externally" treatment, Spawn/Resume refuse with the 409 any foreign
checkout gets, and Merge fails its worktree gate naming the old path. If a
tmux session is still attached there, that session isn't affected either
(it's just a directory some process happens to be running in). Before
switching, for each task with a worktree under the old root: either merge
or abandon it normally first (see "Merging finished branches" in [docs/merging.md](merging.md#merging-finished-branches) / "Cleanup" in
[docs/operations.md](operations.md#cleanup) — do this *before* restarting with the new config, since those flows
also need Centrale to still be able to find the worktree), or, if you'd
rather keep the in-progress work, move it by hand
(`git worktree move <old> <new>` from the project's repo, where `<new>` is
where the new setting will look) before restarting.

If a configured `path` doesn't exist, or the `backlog` CLI fails or returns
something Centrale can't parse for that repo, that single project degrades to
an error banner in the UI (and an `"error"` field in `/api/board`) — the
other configured projects still load and render normally.

## Owner questions

Needs you reads tasks labelled `needs-owner-approval` across all configured
projects; no setting is needed. Using the convention is optional. Put the
question in a task comment and stop work until the owner answers. Whoever
records the decision removes the label. Done tasks and tasks with a final
summary are excluded. Centrale reads the label without changing it or enforcing
an approval gate. See the [agent guide](../static/agent-guide.md#owner-questions-optional-convention).

## Settings

The gear icon at the bottom of the sidebar opens a Settings view over the
main area, backed by `GET`/`POST /api/settings`. It edits a **whitelisted**
subset of `projects.json`:

1. **Automatic merging** — the same `harvest.mode` toggle described above,
   labeled "Automatically merge branches when all safety gates pass." Read
   live by the auto-merge thread, so this takes effect on its next cycle —
   no restart.
2. **Per-project settings** — each project's `checkCommand`, `maxAgents`,
   and `worktreeLinks` (see above). Empty test command means no test gate;
   blank max agents means no cap; links are one repo-relative path per
   line, with an empty field meaning no links. Changes apply to future
   launches and merge attempts. Links are added only when a new worktree
   is created.
3. **Board auto-refresh interval** — `refreshIntervalSeconds`, a whole
   number of seconds, minimum 5. Purely client-side; changing it doesn't
   affect a countdown already in progress, only the one after it.
   Below it, **Read-only status page** — `statusPage`'s switch, port and
   optional interface or address. Saving starts, stops or moves the
   listener at once, then shows whether it is listening (or why not) and
   the links to open on a phone.
4. **Agents** — a collapsed section ("Agents (N configured, default:
   …)") that, once expanded, edits the `agents` map itself (see "Setup /
   configuration") — the file-edit alternative for adding your own agent
   without touching `projects.json` by hand. It opens with a line naming
   that backing store, then one row per agent: the two built-ins
   (`claude`, `codex`) as quiet, non-deletable rows whose command and
   prompt suffix can still be overridden, your own agents fully editable
   with a **Remove** button, and a **+ Add agent** button. Each row has a
   **name** (matched case-insensitively against a task's `@assignee`, one
   word), a **command** (the argv written like a shell command line —
   quote an argument that contains spaces, e.g. `claude
   --append-system-prompt 'You are a layout specialist.'`; it's split
   with POSIX shell quoting rules and stored as the exact argv list, and
   shown back the same way, so a list round-trips unchanged) and an
   optional **prompt suffix**. Below the rows sits the **Default agent**
   dropdown (`defaultAgent`), listing the rows above — including one you
   just added and haven't saved yet. Everything here is saved with the
   main **Save** button; an untouched section never rewrites the map (a
   config running on the built-in defaults keeps doing so). Rules:
   - A name and a command are required; a blank one blocks the save with
     an inline error under that row. Names must be unique ignoring case
     (matching is case-insensitive), a built-in can't be removed, and the
     map can't be emptied.
   - A command whose executable isn't found on `PATH` is a **warning,
     not a block**: the save goes through, the status line says which
     agent and which executable, and the row shows a "not on PATH" tag —
     catching a typo before a spawn wastes it (the tag also shows for a
     built-in that simply isn't installed).
   - **Removing** an agent that some board task is assigned to, or that
     is the current default, is never silent: the first click arms the
     button and explains which tasks point at it and what they'll fall
     back to (the picked default, or `claude` when the default itself is
     going) — a second click within 5 seconds removes it; an unreferenced
     agent goes on the first click. Removing the default without picking
     a new one in the same save is refused server-side.
   - A hand-written `resumeCmd` (or any other key) on an entry survives
     an editor save untouched; the editor only writes `cmd` and
     `promptSuffix`.
5. **Require an agent assignee** — `requireAgentAssignment`, the toggle
   "Refuse to spawn a task whose assignee isn't a configured agent"
   beside Automatic merging's. On by default; off, such a spawn falls back to
   the default agent with a warning. Takes effect on the next spawn.
   Under it, **Lock a spawned task's file on the main checkout** —
   `lockSpawnedTaskFiles`, on by default; takes effect on the next spawn.
6. **Live session pane** — two toggles editing the one tiered
   `sessionPreview.mode` (see "Setup / configuration"): "Show the live
   session pane in the task drawer" and, under it, "Allow replying to the
   agent from the drawer (send text and keys)". Both on → `"interact"`; pane on,
   reply off → `"view"`; pane off → `"off"` (the reply toggle is greyed
   out, since there is nothing to reply into). Both on by default. Turning
   the reply off removes the reply row *and* makes `POST
   /api/session-input` refuse with a 403 while the pane keeps working;
   turning the pane off removes the whole section *and* makes `GET
   /api/session-pane` refuse too — effective immediately, the next pane
   poll stops on its own.

Saving posts the form's current values (harvest mode, refresh interval,
check commands, default agent, `requireAgentAssignment`, `lockSpawnedTaskFiles`, live-pane tier, and — only if the Agents
section was touched — the whole agents map) to `POST /api/settings`. Only if every
provided field passes validation does anything change: the in-memory
config is updated immediately (so it applies without a restart wherever
feasible, as above) and `projects.json` is rewritten atomically (temp file
+ `os.replace`), reading the file fresh each time so every key outside the
whitelist — and every other field on a project entry, like `path` or
`browserPort` — round-trips through `json.load`/`json.dump` untouched. An
invalid field (a `checkCommand` that isn't a string, a
`refreshIntervalSeconds` under 5, an unrecognized project name, a
`defaultAgent` not in the agents map, ...) fails the whole save with a
field-level error shown inline next to that field — nothing is written and
nothing already-valid in the same submission is applied either, so a save
either fully succeeds or has no effect at all. A successful save also
shows a toast and immediately re-fetches the board, since
`harvestMode`/`refreshIntervalSeconds` may have changed.

Below the form, a separate **Projects** section lists every configured
project with a confirm-armed **Remove** button (same two-click pattern as
Re-spawn/Resume — click once to arm, click again within 5 seconds to
confirm) and an **Add a project** form. Each of these posts its own
one-field `POST /api/settings` immediately, rather than waiting for the
main Save button, and both refetch the board on success so the change is
visible right away.

- **Remove** only deletes that project's entry from `projects.json` — it
  never touches the repo itself (no directory deletion, no branch/worktree
  cleanup). Removing the last remaining project is allowed: a board with
  zero projects is a supported state and shows the same first-run welcome
  panel a machine with no `projects.json` does, so a first project added
  with the wrong path can simply be removed again. What *is* refused, with
  the reason shown under the Projects list, is a removal that would
  **strand** work: a project with a live agent session, or with a `task/*`
  spawn branch not yet merged into the branch its checkout is on. Neither
  is destroyed by a removal — they would just go on existing with nothing
  left in the dashboard able to end, merge, discard or abandon them — so
  end the session or merge/discard/abandon the branch first, and the
  removal goes through.
- **Add a project** takes a name and a path, plus one "Set up Backlog.md
  and Centrale in this repo" checkbox (off by default). Server-side
  validation, in order: the name must be non-empty, unused, and
  filesystem/URL-safe (letters, digits, `-`, `_`, `.`, starting with a
  letter or digit); the path must expand (`~` supported) and exist; the
  expanded path must be a git repository (`git rev-parse
  --is-inside-work-tree`, through the same injectable `run_git` boundary
  as everything else). If all of that passes but the repo has no
  `backlog/config.yml`, the add is refused with a message telling you to
  tick the checkbox. When ticked, setup runs `backlog init <name> --defaults
  --integration-mode cli --agent-instructions claude,agents` only if the
  config is missing, and installs a short Centrale guide pointer in both
  `CLAUDE.md` and `AGENTS.md` even when Backlog already exists. Setup commits
  only files it changed; unrelated staging and edits stay as they were.
  Dirty target files, symlinks and malformed pointer markers refuse before
  writing. Identical pointers are a no-op; a changed server port is repaired
  in place. Failures refuse the add with their reason. If initialization or
  a later write/commit fails, inspect any files it left behind; a failed
  commit names the exact files to commit before retrying. On success the new
  entry (name + path as typed, so a `~/...`
  path round-trips the same way existing entries do) is appended to
  `projects.json` and to the live config, with `checkCommand`/`browserPort`
  left at their defaults — edit those from their own settings afterward.

For projects already added, use [POST /api/setup-project](api.md#post-apisetup-project).
There is no additional UI button. `python3 server.py --check` reports missing
or stale pointers and prints the single command that runs setup for each
project. Setup is always an explicit owner action, never a startup side effect.
The pointer uses the configured Centrale port; re-run setup after changing it.

**API:**

- `GET /api/settings` responds `{"harvestMode", "sessionPreviewMode",
  "refreshIntervalSeconds",
  "checkCommands": {"<project>": "<command>" | null, ...}, "defaultAgent",
  "agents": ["<name>", ...], "requireAgentAssignment": bool, "lockSpawnedTaskFiles": bool, "agentEntries": [{"name", "cmd": [argv],
  "cmdText", "promptSuffix": str | null, "builtin": bool, "onPath": bool},
  ...], "projects": [{"name", "path"}, ...], "statusPage": {"enabled",
  "port", "bind", "running", "error", "links", "labelledLinks"}}` — the
  current value of every whitelisted setting, plus `agents` (every
  configured agent name, sorted), `agentEntries` (the full map in
  `projects.json` order, for the Agents editor: `cmdText` is the
  shell-quoted one-line form of `cmd` — `shlex.join`, whose exact
  inverse `shlex.split` is what a string `cmd` is parsed with on POST;
  `builtin` marks the shipped `claude`/`codex` names; `onPath` is a
  `PATH` lookup of `argv[0]`) and `projects` (every configured project's
  name/path, for the Projects list).
- `POST /api/settings` accepts a partial version of that same shape.
  Besides the original three, eight more keys are whitelisted:
  - `sessionPreviewMode` (`"interact"` | `"view"` | `"off"`) — the live
    session pane / reply tier; written to `sessionPreview.mode`.
  - `statusPage` (`{"enabled", "port", "bind"}`, each optional) — the
    read-only status page; written to `statusPage` and applied to the
    running listener at once. The key is never accepted or written.
  - `requireAgentAssignment` (boolean) — see the Fields list above;
    anything but a JSON boolean is a 400.
  - `lockSpawnedTaskFiles` (boolean) — see the Fields list above;
    anything but a JSON boolean is a 400.
  - `defaultAgent` (string) — must name a key in the configured `agents`
    map (or in the map an `agents` field in the same request installs).
  - `agents` (a list of `{"name", "cmd", "promptSuffix"}`) — the WHOLE
    map, in the order it should be written; `cmd` is either an argv list
    of strings or one shell-quoted string; `promptSuffix` is optional
    (null/blank means none). Written to the top-level `agents` key
    (entries that already use the object form keep their other keys,
    such as `resumeCmd`; a new agent is written as a plain list unless
    it has a suffix) and applied to the live config at once. Validation
    (400, indexed fields such as `"agents.2.name"` / `"agents.2.cmd"`,
    map-level ones as `"agents.<name>"` / `"agents"`): name required, one
    word of at most 64 characters, unique ignoring case; command
    required (a quoting error is reported with shlex's reason); a
    built-in currently in the map can't be dropped; the map can't be
    empty; and the effective default agent must survive (name a new
    `defaultAgent` in the same request otherwise). A successful response
    additionally carries `"warnings": [...]`, one line per saved agent
    whose executable isn't on `PATH` — informational only.
  - `removeProject` (string) — a project name; see "Remove" above.
  - `addProject` (`{"name", "path", "initBacklog": bool}`) — see "Add a
    project" above; `initBacklog` defaults to `false` if omitted and must be
    a boolean. The existing API key now opts into both Backlog and Centrale setup.

  Any of these ten top-level fields may be omitted, leaving that setting
  untouched. Responds with the same shape, updated, on success (200). A
  validation failure responds 400 with `{"error", "fields": {"<field
  name>": "<reason>", ...}}` — dotted for a per-project, per-add or
  per-agent field, e.g. `"checkCommands.my-app"`, `"addProject.name"`,
  `"addProject.path"`, `"agents.0.cmd"`.
  A structurally malformed body (e.g. `checkCommands` present but not an
  object, `addProject` present but not an object, or `agents` present but
  not a list) also responds 400 with just `{"error"}`, no `fields`.
