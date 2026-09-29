# Changelog

What changed between released versions of Centrale, written by hand.

It is written by hand on purpose. Centrale is published as a curated
snapshot — one squashed commit per release, built from `git archive HEAD`
(see [docs/operations.md](docs/operations.md#releasing)) — so the
development repo's commit log is not published and a generated changelog
would have nothing honest to generate from. A short list of what actually
changed for the person running it is more useful than a diff summary
anyway.

Versions here are the ones `version.py` names and `scripts/release.sh`
tags: the annotated tag `vX.Y.Z` on the public snapshot, the heading
`## vX.Y.Z` below, and `__version__` in `version.py` are the same number,
and a release refuses to go out when they disagree. `python3 server.py
--check` and the sidebar footer report the version the running server
booted from.

Centrale follows [semantic versioning](https://semver.org) loosely: it is
an application rather than a library, so the "public API" a major bump
would protect is the shape of `projects.json`, the `/api` endpoints
documented in [docs/api.md](docs/api.md), and what a spawned agent is
handed.

## v0.2.0 — orchestrating agents

This release is about a second kind of user: an agent that runs other
agents. Master agents in several projects were spawning, reviewing and
merging through Centrale's API, and the gaps they hit are what changed.

Tested against **`backlog` v1.51.0**, the same baseline as v0.1.0.

**Changed defaults — read these first:**

- **Spawn no longer guesses the agent.** A task whose first assignee is
  not a configured agent (unassigned, or a person) is refused with `409`
  instead of silently getting the default agent. Pass `"agent"` on
  `POST /api/spawn`, or pick one in the drawer. The chosen agent is
  recorded as the task's assignee in the claim, ahead of any person
  already assigned. `requireAgentAssignment: false` in `projects.json`
  restores the old behavior.
- **A spawned task's file is locked on the main checkout.** Right after the
  claim, the main checkout's copy of that one task file is made read-only,
  so a `backlog task edit` on it fails instead of creating a conflict with
  the agent's copy on its branch. Merging the branch releases it; discard,
  abandon and cleanup release it; a stale lock is cleared at startup and
  by `--check`. The card shows a "task file locked" badge. Turn it off
  with `lockSpawnedTaskFiles: false`. It is a guard rail, not security.

**New:**

- **Rulings go to the agent.** `POST /api/rule` sends a ruling to a
  spawned task's agent and confirms it arrived; the agent is told to
  record it on its task as a comment by the sender. With no agent
  running, the ruling is committed as a comment on the task's branch.
- **Deliver a message and know it arrived.** `POST /api/deliver` types a
  message into a spawned agent's session and waits for it to show up on
  screen, including a message Codex queues while it is mid-turn. Every
  delivery is logged; `GET /api/deliveries` reads the log.
- **Wait instead of polling.** `GET /api/orchestrator-wait` holds the
  request until something needs the orchestrator — a task finished, is
  waiting for input, merged, or had its merge blocked — and answers with
  one plain line and a cursor for the next call. Nothing is lost between
  calls, and repeated identical results are sent once. Codex only reports
  "waiting" when an approval dialog has actually been on screen for 30
  seconds, not for every request its own reviewer approves.
- **A guide for agents, served by Centrale.** `GET /api/agent-guide` is a
  short operating guide for an agent using Centrale: spawning, rulings,
  merging (through the API is recommended), states and badges, waiting.
- **One setup checkbox.** Adding a project now offers "Set up Backlog.md
  and Centrale in this repo", which runs `backlog init` if needed and adds
  a short pointer to the guide in `CLAUDE.md` and `AGENTS.md`, committing
  only those files. `POST /api/setup-project` does the same for a project
  already added, and `--check` lists projects without the pointer.
- **Per-project spawn settings.** `maxAgents` caps a project's live agent
  sessions (counted from tmux on every spawn and resume; a refusal names
  the sessions). `worktreeLinks` symlinks paths such as `.venv` or
  `node_modules` from the main checkout into every new worktree, keeps
  them out of git, and removes the link, never its target, on cleanup.

**Fixed:**

- **Spawned Claude agents' hooks are passed inline.** They were read from
  one shared `~/.cache/centrale/hooks-settings.json`, rewritten on every
  spawn with the path of whichever Centrale copy spawned last; a copy run
  from a since-deleted worktree then blocked every agent's tool calls.
  Each agent's settings are now fixed at launch, and the file is no
  longer written.
- **Spawned agents don't stop on setup offers or hold messages.** A new
  Claude agent no longer shows the "teach auto mode" offer, and accepts
  messages from your other sessions instead of holding them for approval.
- **The spawn claim commits only the board.** It no longer sweeps other
  staged changes in the main checkout into the claim commit.
- **Ending a session stops what it left running.** End-session and cleanup
  also stop processes still running inside the task's worktree.

## v0.1.0 — first public release

The first published snapshot. Everything below is what Centrale does at
this point; later entries will list only what changed.

Tested against **`backlog` v1.51.0** (`--json` `schemaVersion: 1`) on
Node 20 and Python 3.12, on Linux. Centrale is a veneer over that CLI, so
the version it was exercised with is part of what this release claims:
the full suite, the real-process integration tier, and a real spawn,
claim, branch-side edit and gated merge all ran against 1.51.0. Upstream
ships often — a newer `backlog` is expected to work and is not refused,
but this is the one the release was actually run on.

- **One board across repos.** The kanban columns of every configured
  Backlog.md project in one view, in Backlog.md's own visual language,
  light and dark. Cards carry the task id, a project chip, priority,
  labels, a milestone chip, a "ready" dot or "blocked" badge, and an amber
  dot for an unmerged branch. Search matches title, task id or label
  (`Ctrl+K` focuses it); a "ready to start" filter, a milestone filter and
  per-project show/hide or solo narrow the board further, and all of them
  survive a reload. A project that fails to load gets an error banner
  instead of a board that fails whole.
- **A task drawer that shows the agent's branch.** Description,
  acceptance criteria, dependencies resolved to their titles (click one to
  open it), plan and notes, plus a separate section for the same task as
  committed on an unmerged `task/<id>` branch — the work `main` cannot
  see yet, read from wherever that branch is checked out. The drawer keeps
  following the task while it is open, so criteria tick as the agent
  checks them, without losing your place. **Open task** opens the task
  itself on that project's Backlog.md web board, launched on demand; the
  ⧉ button on a sidebar project chip opens the board root.
- **Reading and answering a running agent without attaching.** The
  drawer tails the live tmux pane as plain text with a capture age, and a
  reply row types into the session — with Esc and Enter beside it, sent
  as those keys, for a dialog a line of text cannot answer. **Expand**
  widens the drawer and **Maximize** opens the session theater, where the
  ticket sits in a rail beside the terminal that collapses to give the
  pane the whole overlay.
  Live sessions are also listed in the sidebar with the attach command
  ready to copy, and a row opens its task's drawer. The whole tier, server
  endpoint included, can be turned off in Settings.
- **Spawning routed by assignee.** Spawn claims the task, cuts a worktree
  (inside the project by default) and a `task/<id>` branch, and starts
  the agent the task's own `@assignee` names (`@claude`, `@codex`, or a
  CLI configured for the project) in its own tmux session, with a
  standard Backlog.md workflow prompt — one that tells the agent to
  consult the repo's decision records and never to merge its own branch —
  which the config can replace. Guards refuse a `Done` task, arm a confirm
  for a task already claimed by a worker the board cannot see, and warn
  when other agents are already active in the same project — the sidebar
  lists the files each of them has touched so far.
- **Resume and Re-spawn.** A branch left behind by a session that died —
  with uncommitted changes or without — is offered **Resume**, which
  continues the same conversation for claude (`claude --continue`) and for
  codex (`codex resume --last`, scoped to that worktree) alike, or runs the
  resume command configured for an agent of your own. When a branch has
  fallen behind `main`, Resume hands the agent the reconciling, with the
  conflict named in its prompt. **Re-spawn** sends a fresh agent back into
  an existing worktree, for fixing what the gates refused.
- **Honest agent lifecycle badges** built from the events the agent CLI
  itself emits, never inferred from CPU or pane activity — including a
  deliberately ambiguous "turn ended · may need input" state for codex,
  which cannot distinguish finished from waiting. A badge belongs to its
  session and goes when the session does. **End session** closes a
  session from the drawer or the sidebar without a terminal, and every
  live session has it, whatever its badge says. A "working" agent is the
  one state that asks twice: the first click arms the button, which
  relabels itself to say the agent is mid-turn, and a second click
  within the window ends it anyway.
- **Six-gate merging**, one branch at a time, all ready branches at once,
  or hands-off on a background thread: no live session, the task `Done`
  with every criterion checked, a clean worktree, a conflict-free dry-run
  merge, an optional per-project test command against the merged tree,
  and a last look at the destination checkout. Unrelated uncommitted files
  in the destination are tolerated; only an overlap refuses, and the
  refusal lists the files. The button names the gate it is on, so a slow
  test command looks like one. When a card was moved to `Done` on the
  board by hand while the branch still says otherwise, the drawer offers
  to adopt that `Done` onto the branch and run the gates again.
- **Work done outside Centrale still fits**: a `task/<id>` branch adopted
  into a foreign worktree is detected from git alone and reported rather
  than failed on, a branch parked with no worktree at all merges through
  the same gates, and an already-merged branch is offered a one-click
  cleanup.
- **A bad attempt has a way out.** **Discard attempt & start over**
  removes the spawn's worktree and deletes its branch — the one
  irreversible act in Centrale — so the task returns to an ordinary Spawn
  and the next agent starts fresh from the base. It takes two clicks: the
  first measures the repository and relabels the button with exactly what
  would go ("3 commits and 4 uncommitted files"), the second acts, and
  only on that exact state — if the branch tip or the uncommitted files
  moved in between, the server refuses and asks for a fresh look. A live
  session blocks it until End session has run, the deleted tip is tagged
  before it goes, and the drawer keeps the one-line command that puts the
  branch back. A milder **Abandon worktree, keep branch** frees the
  worktree and leaves the branch parked and mergeable.
- **Settings in the UI.** The gear at the foot of the sidebar adds and
  removes projects (running `backlog init` for a repo without a board, on
  request), edits the agents map — a name to match an `@assignee`, a
  command line, an optional prompt suffix — picks the default agent, sets
  each project's test command for the merge gate, switches merging
  between click and automatic, and toggles the live pane. It writes a
  whitelisted subset of `projects.json` and leaves the rest of the file
  alone. Removing a project deletes only its entry — never the repo —
  including the last one you have, since an empty board is a supported
  state; a removal is refused only while it would strand work Centrale
  is managing, naming the live session or unmerged branch in the way.
- **Zero config to start.** No build step, no `pip install`, no config
  file to write: `python3 server.py` runs with defaults and an empty
  board, and projects are added from the Settings gear. Linux, with
  best-effort macOS. Without `tmux`, the board, drawer, Open task and
  merging all still work, and spawning says why it is disabled.
- **Local only.** Centrale binds `127.0.0.1` and refuses any request
  whose origin or host is not its own, on reads as well as writes, so a
  web page you happen to be visiting can neither read your board nor
  drive an agent.
- **`python3 server.py --check`**, a doctor command that reports the
  version and every prerequisite (Python, git, the `backlog` CLI, tmux,
  the codex sandbox, `projects.json` and each configured project) without
  binding a port. The sidebar footer shows the same version for the
  server that is actually running.
- **A stale-process banner.** Python is loaded once at start, so a merge
  that touches it changes nothing until a restart. The board compares
  the commit the server booted from with `HEAD` of its checkout on every
  refresh and says so — both commits, how many commits behind, and that a
  restart is needed — the moment they differ; `--check` reports the same
  comparison for a running server. Silent when they agree, and on any
  doubt (a downloaded snapshot with no `.git`, no `git`, uncommitted
  edits on the same commit).
- **A stale-board notice.** The same problem one layer down: a
  `backlog browser` keeps the version it was started with, so upgrading
  backlog.md leaves an already-open board serving the old one. Opening a
  board asks it what it is running, compares that with `backlog
  --version` on `PATH`, and says so beside it when they differ — both
  versions, and the fix (stop that board, open it again). The notice
  stays until dismissed, since opening a board moves you to the new tab.
  Nothing is killed or restarted for you; silent when they agree or when
  either side cannot be asked.

### Naming

Centrale was called Planner while it was private and was renamed before
it was ever published, so no released version has ever read the old
names. A tmux session is `centrale-<project>-<taskid>`, a worktree lives
in `.centrale-worktrees/`, the `backlog browser` registry is
`~/.cache/centrale/browsers.json`, and the environment overrides are
`CENTRALE_SPAWN_CMD` and `CENTRALE_EVENT_URL`.
