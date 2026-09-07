# Centrale agents

The agent contract: spawning (guards, assignee-driven selection, the
prompt), resuming an interrupted agent and resume-to-reconcile, branches
worked outside Centrale, lifecycle events, the multi-agent workflow, the
`CENTRALE_SPAWN_CMD` override, and running without tmux. Part of the
Centrale manual; the [README](../README.md) is the front door and lists
the other chapters.

## Spawning an agent

Clicking "Spawn agent" (or "Spawn (\<agent\>)" once the task has an
assignee that resolves to a specific agent — see "Agent selection" below)
on a ready task (inline on its card, or in the task drawer) sends
`POST /api/spawn` with that project and task ID — except when the button
first has to arm an informed confirm instead (see "Spawn guards" below).
On the server side this (see `spawn.py`):

1. Validates the project name and task ID, rejects the request with a 409
   if a tmux session for that project+task is already running, and
   refuses with a 409 naming the status if the task is already `Done` —
   before any claim, worktree, or session is touched.
2. Claims the task and commits that claim, on the repository's current
   branch, *before* the worktree is cut: runs `backlog task edit <id> -s
   "In Progress"` (leaving the assignee untouched), then `git add backlog`
   and commits with message `backlog: claim <id> for spawn` — skipped
   silently if that leaves nothing under `backlog/` to commit. This
   happens even when `CENTRALE_SPAWN_CMD` is set (only the launched
   command is overridden, not this step). If claiming or committing fails, the
   spawn still proceeds — the problem comes back as a `warnings` entry in
   the response (see below) instead of blocking anything.
3. Ensures a git worktree exists at
   `<worktreeRoot>/<project>-<taskid-lower>`, on a branch named
   `task/<taskid-lower>`, cut from the repository's current branch (as
   resolved by `git symbolic-ref --short HEAD`, falling back to
   `git rev-parse --abbrev-ref HEAD`, and finally to `HEAD` if detached) —
   which, thanks to step 2, now already contains the claimed task, so the
   worktree and the source repo never disagree about who owns it. If the
   worktree directory already exists and is registered with git (for
   example after a previous session was killed but never cleaned up), it
   is reused as-is instead of being recreated. If only the branch already
   exists, the existing branch is checked out into a new worktree instead
   of creating a new one. When `worktreeRoot` resolves to somewhere inside
   the project itself (`"@repo"` — see "Setup / configuration" in
   [docs/configuration.md](configuration.md#setup--configuration)),
   this step also idempotently adds that worktree directory to the
   project's `.git/info/exclude` the first time, so it never shows up in
   `git status` there.
4. Resolves which agent to run (see "Agent selection" below), unless
   `CENTRALE_SPAWN_CMD` is set, in which case that always wins.
5. Starts a detached tmux session named `centrale-<project>-<taskid-lower>`,
   running the resolved agent command with a standard workflow prompt, with
   its working directory set to the worktree, a `CENTRALE_EVENT_URL`
   environment variable always set on the session (see "Agent lifecycle
   events" below), and (unless `CENTRALE_SPAWN_CMD` overrode the agent) a `CENTRALE_AGENT=<agent>` environment variable plus, for a resolved
   `claude`- or `codex`-family agent, extra argv hooking it up to report
   its own lifecycle back to Centrale — also detailed in "Agent lifecycle
   events".
6. Responds with `{"session": "<name>", "attach": "tmux attach -t <name>",
   "agent": "<agent>"}`, plus a `"warnings"` array of human-readable
   strings if step 2 hit a problem. The UI shows each warning as a
   dismissible toast alongside the normal spawn success message.

For example, spawning on project `my-app`, task `TASK-2`, produces worktree
`<worktreeRoot>/my-app-task-2` on branch `task/task-2`, and tmux session
`centrale-my-app-task-2`.

### Spawn guards

Two frontend-only guards (decided in `static/spawn.js`, worded in
`static/tasks.js`, rendered on the card by `static/board.js` — no extra
API calls) catch a task that's already spoken for before a spawn ever
reaches the server:

- **Already claimed outside the board** — a task whose status is already
  `In Progress`, but with no Centrale session and no spawn branch for it
  (i.e. it's being worked by something Centrale can't see: a self-directed
  agent, a teammate, a delegated subagent), doesn't spawn on the first
  click. The button instead arms an informed confirm using data already in
  the board summary — e.g. "In Progress — claimed by @claude, updated 12
  minutes ago. Spawn anyway?" (the claimed-by clause drops out gracefully
  when there's no assignee) — and a second click within the window proceeds
  anyway; letting the window lapse reverts to a plain Spawn button. This
  guard is real prevention for a real incident: a board-driven spawn once
  started a second agent on a task another one was already mid-way through.
  It never triggers for an interrupted re-spawn (a Centrale-owned branch or
  worktree already exists for the task) or for a task in any other status —
  both stay a single click.

  One case in that shape isn't a claim at all: right after you **discard**
  an attempt the task is still `In Progress` on the board (a discard
  deliberately never touches status) with no branch, no worktree and no
  session — the exact signature above. The recovery tag the discard left
  behind says otherwise, so the confirm names it instead: "Previous attempt
  discarded 17:05 today; still In Progress on main. Spawn fresh?" It still
  arms, and still takes the second click — the task really is claimed with
  nobody on it — only the sentence changes. With no recovery tag the copy is
  the claimed-elsewhere one above. Derived per board load from the tags
  themselves (`lastDiscardedAt`, see [docs/api.md](api.md#get-apiboard)), so
  deleting a tag takes the sentence with it.
- **Already Done** — covered above: the server refuses a spawn or resume on
  a `Done` task outright (409), and the drawer's own render pass stops
  offering Spawn immediately after a successful in-drawer merge, without
  waiting on the next board refetch to notice the task is now Done.

### Agent selection

Which CLI gets spawned is driven by the task's first assignee (`assignees[0]`
from `backlog task view <id> --json`, `@` stripped, matched
case-insensitively against the `agents` map in `projects.json`):

- An assignee that matches a key in `agents` (e.g. `@codex` when `agents`
  has a `codex` key) spawns that agent's `cmd`, with its `promptSuffix` (if
  any) appended to the standard workflow prompt (or to your `spawnPrompt`
  override, if you set one).
- No assignee, or an assignee that matches nothing in `agents`, spawns
  `defaultAgent`'s `cmd`/`promptSuffix`.
- If the `backlog task view` call itself fails, Centrale falls back to
  `defaultAgent` rather than failing the spawn.

For example, with this `agents` config:

```json
"agents": {
  "claude": ["claude"],
  "reviewer": {
    "cmd": ["claude", "--append-system-prompt", "You are a layout specialist."],
    "promptSuffix": "Prefer the layout-reviewer subagent for any layout changes."
  }
}
```

assigning a task to `@reviewer` spawns `claude --append-system-prompt "You
are a layout specialist." "<standard prompt>\n\nPrefer the layout-reviewer
subagent for any layout changes."` — i.e. the object form's `cmd` picks the
CLI and its flags, and `promptSuffix` steers what it's asked to do, both
purely through config.

The spawn button's label reflects the task's raw assignee before spawning
(e.g. "Spawn (codex)"); the actual resolution above happens server-side at
spawn time and the confirmed agent name comes back in the response and is
shown next to the spawned session.

The prompt given to the agent is, by default:

> Work on backlog task \<ID>. Follow the Backlog.md workflow: run `backlog
> instructions overview` first, claim the task, implement it, check off
> acceptance criteria as you meet them, add implementation notes, and update
> the status when done. Before designing anything, check the repo's standing
> decision records (`backlog decision list --plain`) and treat them as
> constraints. When you are done, commit all your work on this branch,
> including the backlog task updates. Do NOT merge this branch into the
> default branch or delete it — the dashboard's gated merge handles
> integration.

The standing-decisions sentence points the agent at Backlog.md's decision
records — the durable "why" layer behind a repo's constraints, which an
agent otherwise only stumbles on through lucky search-term overlap. In a
repo with no decisions the command prints "No decisions found." and that's
the whole cost.

That last sentence is the fix for a real coordination failure: a spawned
agent merging its own branch directly bypasses every merge gate ([docs/merging.md](merging.md#merging-finished-branches)), including
`checkCommand` running against the merged tree — it's a rule the *product*
enforces in the prompt every agent gets, not something left to per-repo or
per-user config (see "Merge ownership" in [docs/merging.md](merging.md#merging-finished-branches) for what happens if an agent
does it anyway).

The whole template is replaceable with the top-level `spawnPrompt` key in
`projects.json` (see "Setup / configuration" in [docs/configuration.md](configuration.md#setup--configuration) for the rules — it must
keep a `{task_id}` placeholder, and is validated at startup). An override is
used verbatim, so keep the no-self-merge rule in yours unless you have a
different way of enforcing it; a per-agent `promptSuffix` appends to
whichever template is active.

To attach to a running session from a terminal:

```bash
tmux attach -t centrale-my-app-task-2
```

The UI's success message and the sessions panel both show this exact command
with a copy button.

Sessions are created 220 columns by 200 rows (`spawn.SESSION_GEOMETRY`) and
hold that size for life, so the drawer pane and session theater have more
than a screenful to show — an agent TUI owns tmux's alternate screen and
keeps no scrollback, so what the pane can supply *is* its height. The row
count is not chosen independently: it is exactly the largest window
`GET /api/session-pane` will ever return, so the theater's request and the
pane's ability to answer it are one number rather than two that can drift
(see that endpoint in [docs/api.md](api.md#get-apisession-paneprojectnametasktaskidlinesn)).

Two tmux calls hold that size, and both matter. `window-size manual` pins
the session so a later attach can't renegotiate it away, and an explicit
`resize-window` behind the pin sets the size — because `window-size`
defaults to `latest` and is resolved when the session is *born*, so
whenever any client is attached to your tmux server (your own terminal
counts) the `-x/-y` the session was created with is overridden before the
pin can be set, and pinning alone would freeze the wrong size. Both are
best-effort: a tmux too old for either costs a smaller pane, never a
failed spawn.

Because the window is taller than any terminal, an attached session shows
only the part of the viewport that fits rather than resizing to you — you
move around the window instead of seeing it at once. That is the
deliberate trade: the theater is how Centrale is meant to watch a session,
and without the pin the first attach would shrink the session for good,
including after you detach. To get your terminal's size back for one
session:

```bash
tmux set-option -t centrale-my-app-task-2: window-size latest
```

Sessions that are already running are never resized — only newly created
ones get the geometry.

Re-spawning on the same project+task after its tmux session has been killed
reuses the existing worktree and branch rather than starting over — the
same mechanism the drawer's "Re-spawn agent" action (see "Merging finished
branches" in [docs/merging.md](merging.md#merging-finished-branches)) triggers deliberately, for sending an agent back into a
branch that's awaiting merge to fix a failed gate or finish up.

## Resuming an interrupted agent

If an agent's tmux session dies mid-work (a crash, an OOM kill, `tmux
kill-session`, ...) the work it was in the middle of is still on disk, and
so is its conversation. The drawer offers **"Resume agent"** for a task
with a `task/<id>` branch, no live session, and *either* of:

- **uncommitted changes in its worktree** — `git status --porcelain`
  reports something, which the board sends as `"worktreeDirty": true` on
  `GET /api/board` (checked once per branch, not per task — see
  [docs/api.md](api.md#get-apiboard)); or
- **a branch-side task in an active status** — neither the terminal `Done`
  nor the board's first, not-yet-started column, whatever those columns are
  named in that repo. This one is read from the *branch's* copy of the task
  (`branchTask` on `GET /api/task`), never main's: a spawn commits its claim
  to main and main then stays pinned at `In Progress` for the branch's whole
  life, so main's copy says nothing about whether the agent finished.

The second condition is what covers an agent killed a moment *after* it
committed: a clean worktree and a task still `In Progress`. Before it
existed, only a dirty worktree counted as interrupted, so that agent read as
finished and the drawer offered nothing but a fresh Re-spawn — the exact
shape of the 2026-09-03 tmux-server death, which stranded 22 spawns at once.
A branch whose own task *is* `Done` stays a merge candidate and is left
alone.

The card's amber **"interrupted"** badge is deliberately narrower: it still
means a dirty worktree and nothing else. The board carries no branch-side
copy of any task, and giving it one would cost a `backlog task view` (and
sometimes a detached snapshot) per branch-bearing task on every poll, where
the drawer pays that once per open. So a committed-then-killed task's card
reads "unmerged branch" — which is true — and the drawer, the surface that
has the branch status and the actions anyway, is where it becomes resumable.

Resume renders first in the drawer's row of secondary actions, with
**"Re-spawn agent"** beside it as the quieter alternative — both
confirm-armed, and each with its own confirm, so arming one never arms
the other. Where Re-spawn starts a fresh prompt in
the existing worktree, Resume starts a tmux session there running the
resolved agent's **resume command**, continuing that same dead
conversation instead:

1. that agent's configured `resumeCmd` (see "Setup / configuration"
   in [docs/configuration.md](configuration.md#setup--configuration)), if it has one;
2. else, the family default for an agent Centrale ships, decided by the
   `cmd`'s first argv element (so `claude-sonnet`/`claude-haiku`-style
   entries count too, while a wrapper script or an absolute path counts
   as neither): `["claude", "--continue"]` for `"claude"`, and
   `["codex", "resume", "--last"]` for `"codex"`. Both continue the most
   recent conversation **in that directory** — codex's picker and
   `--last` filter by working directory unless `--all` is passed — and
   since every task gets its own worktree, that is reliably this task's
   own conversation;
3. else, a fresh start after all: that agent's own `cmd` with the
   standard prompt, plus a note that prior work — committed and
   uncommitted — already exists in the worktree, so the agent knows to
   check `git status`/`git diff` before continuing rather than redoing it.

Neither case 1 nor case 2 passes a prompt argument at all — the resumed
conversation already has its own context. ("Resume to reconcile", below,
is the one exception: it goes through these same three cases with the
reconcile prompt appended at each, since there the job is new even though
the conversation is not.) Resume never re-claims or
re-commits the task (unlike a first spawn) since that already happened;
it only ever reuses the worktree that's already there. Session naming,
the existing-session 409 check, and the `CENTRALE_SPAWN_CMD` test override
all behave identically to a regular spawn (`POST /api/resume` is a spawn
variant built on the exact same validation and worktree-reuse code, not a
separate engine) — see [docs/api.md](api.md#post-apiresume).

### Resume to reconcile (a branch that fell behind `main`)

Two agents working in parallel can collide *semantically* without git ever
seeing a textual conflict: the first branch lands a test that asserts
something the second branch legitimately changed. Each branch is green in
isolation; only the combination fails — so the second branch passes gates
1–3, then fails **Merge clean** or **checkCommand** on the scratch-merged
tree, through no defect of its own. Reconciling that by hand (merge `main`
into the branch, fix, re-gate) is work an agent can do, so Centrale makes it
one informed click:

- **The hint.** When a Merge click comes back blocked at gate 4 or 5,
  Centrale also asks git how far the branch has fallen behind its base
  (`git rev-list --count <branch>..<base>`: commits on the base not
  reachable from the branch, via their merge-base). If that's non-zero,
  the report carries `"behindBase": {"baseBranch", "count"}` and a
  `"reconcileHint"`, and the drawer's blocked line says so next to the
  ordinary gate reason: *"this branch predates N newer commits on main;
  the failure may be a collision with newer work, not a defect in the
  branch."* An up-to-date failing branch, a branch failing an earlier
  gate (not Done, dirty worktree, live session), or a passing branch never
  gets the hint — those failures aren't the base's doing — and the
  behind-count isn't even computed for them. It's derived fresh from git
  on every evaluation; nothing is stored.
- **The action.** Under that line the drawer offers **"Resume to
  reconcile"**, a two-click informed confirm (same pattern as Re-spawn/
  Resume — the confirm text spells out exactly what will happen). It
  posts the ordinary `POST /api/resume` with one extra flag,
  `"reconcile": true`: the very same resume path — same worktree, same
  session name, same 409 if a session is already live, same Done refusal,
  same agent resolution, same hook and `CENTRALE_EVENT_URL` injection, and
  the same three resume cases above — with a **reconcile prompt** as the
  trailing argument at every one of them: `resumeCmd` plus the prompt;
  `claude --continue <prompt>` or `codex resume --last <prompt>` (both
  CLIs take an optional prompt alongside resume and deliver it into the
  continued conversation); or, when there is nothing to resume, the
  agent's own `cmd` with the prompt plus the same prior-work note a plain
  Resume's fresh start carries. The prompt says: first orient (read the
  task's description, plan, and implementation notes with `backlog task
  view`, and review the branch's own log and diff against `<base>`), then
  merge `<base>` into this branch (never rebase), resolve every conflict
  textual *and* semantic, run the project's `checkCommand` (spelled out in
  the prompt) until it passes, commit the result on the branch, and do
  **not** merge into `<base>`. When a conversation is being continued the
  prompt adds one sentence of framing — you built this branch, so you
  already know what it intends; `<base>` has moved on since and what you
  remember of it is stale, so read the repository, not your memory of it.
  That is the point of resuming rather than restarting: the conversation
  that built the branch holds the *why* of every change on it (which
  behaviours were intended, which tests pin them, which of two colliding
  changes is the one this task meant), and semantic conflicts are
  resolved from exactly that knowledge. Its stale view of `<base>` is the
  one thing it holds that is wrong, and the prompt's orient-then-merge
  instructions correct that by sending it to the real repository. (Before
  task-133 this variant always started a new conversation on the agent's
  plain `cmd` to avoid that stale view, and the pane after clicking it
  showed an agent re-learning from scratch work it had written minutes
  earlier.) The work itself is never redone: the branch's commits are the
  starting point either way, and on a fresh start the task record (plan,
  notes, summary) plus the branch's diff stand in for the lost context.
- **What Centrale does not do.** It performs no merge or rebase of its own
  here, ever. The agent does the judgment work in its worktree, and once
  its session ends the ordinary five gates re-verify the result exactly
  as they would any other branch. Plain Resume, Re-spawn, and Merge are
  unchanged; the reconcile offer is purely additive and only appears in
  the behind-and-failing state with no live session.

## Branches worked outside Centrale

A `task/<id>` branch doesn't have to stay in Centrale's worktree. Repos
often host other worktrees (`.worktrees/`, `.claude/worktrees/`, `/tmp`
checkouts), and a branch Centrale spawned can be adopted into one —
Centrale's worktree removed, the branch checked out over there, work
actively continuing. Before task-70 the board showed a confusing mosaic for
that: In Progress, an "unmerged branch" dot, and a Merge that failed gate 2
with "worktree not found (was it removed manually?)" — each piece true, the
combination hiding what was really going on.

Centrale now derives the truth from git alone (one `git worktree list
--porcelain` per project, no instrumentation of whatever tool is working
over there) and reports it as `branchCheckout` on `GET /api/board` — see
[docs/api.md](api.md#get-apiboard) for the field. Three kinds:

- **`centrale`** — checked out at Centrale's own worktree path: the ordinary
  case, nothing changes.
- **`external`** — checked out in a worktree Centrale doesn't manage. The
  card shows a **"worked externally"** badge and the drawer names the
  checkout path and the branch's last-commit age (the only freshness signal
  Centrale has for work it can't see). The drawer's branch-side task view
  (`branchTask`) is read from that checkout too, so it shows the external
  session's real status, checked ACs and notes rather than main's stale
  copy — see [docs/api.md](api.md#get-apitaskprojectnameidtaskid). Spawn and Resume are disabled with
  that reason — git refuses a second checkout of the same branch anyway,
  and `POST /api/spawn`/`/api/resume` refuse with a 409 naming the path
  instead of failing with the raw git error *after* the claim was already
  committed. Merge's gate 2 — **Task done**, which reads the branch's own
  task state and is therefore where a branch with no Centrale worktree is
  refused, ahead of gate 3 ("Worktree clean") — likewise says "checked out
  outside Centrale at `<path>`" rather than guessing about manual removal
  — and the card and drawer show **Merge disabled** with that reason as
  its tooltip until the foreign worktree is gone (task-81): a click could
  only refuse, so the button says so up front instead of costing a harvest
  round trip and a blocked events-log entry. It re-enables by itself on
  the next board refresh once the branch is no longer checked out
  externally. "Merge all ready" and auto mode are unaffected: they still
  evaluate every branch server-side, and an external one simply lands in
  *not ready* on gate 2.
  "Merged — clean up" (a branch merged out-of-band whose foreign checkout
  is still around) is rendered disabled the same way — `POST
  /api/cleanup-branch` refuses that state with the same 409 sentence
  before touching anything, since git won't delete a checked-out branch.
- **`none`** — a **parked** branch: it exists but is checked out nowhere
  (its worktree was removed, by hand or by the external session finishing).
  The card labels it "parked branch". Merge — and the drawer's `branchTask`
  — reads the committed branch-side task through the Backlog CLI in a
  disposable detached snapshot (one shared implementation, so the two can
  never disagree about what a parked branch says), then Merge lets
  **Worktree clean** pass with the explicit reason "no worktree -- nothing
  uncommitted to protect". The ordinary scratch merge, `checkCommand`, and
  main-checkout gate still run before either click or auto mode may land it.
  The snapshot uses the standard `TMPDIR`-overridable temporary directory
  and is removed immediately, including when the task read fails.

To hand a branch back to Centrale, finish the work, commit the branch-side
Backlog updates (including `Done` and checked acceptance criteria), and remove
the foreign worktree (`git worktree remove <path>`). The next board refresh
sees a parked branch that the same normal Merge action — clicked or automatic
— can adopt; no Centrale spawn or integration with the external tool is
required. Re-spawn remains available when more work is needed. An external
session can also opt in to live agent badges — see the end of "Agent lifecycle
events" below.

## Discarding a bad attempt

Sometimes a spawn produces nothing worth keeping. End session kills the
agent but leaves the worktree and branch in place, and the "Merged — clean
up" action only handles branches that are *already* merged (it deletes with
the safe `git branch -d`, which refuses unmerged commits by design). The
drawer carries two more actions for the rest — both secondary, both
drawer-only, and both taking two clicks:

- **Discard attempt.** Removes the worktree **and deletes the
  branch**, leaving the task with no branch at all: the ordinary Spawn
  button comes back and the next agent branches fresh from the base.
  The branch has to go, not just the worktree — spawning reuses an existing
  `task/<id>` branch, so parking it would hand every Re-spawn the same bad
  commits back.
- **Abandon worktree, keep branch.** Removes only the worktree. The branch
  is left [parked](#branches-worked-outside-centrale) and the ordinary
  gated merge can still merge it later, without a worktree. This is the
  other statement: not merging now, but not throwing the work away either.

**The confirming click names what goes.** The first click measures the
repository (`GET /api/discard-preview`) and relabels the button with the
result — "Discard 3 commits and 4 uncommitted files?", or for the milder
action "Remove the worktree and discard 4 uncommitted files?". The second
click is the one that acts. If a count cannot be measured, the confirm does
not arm at all rather than showing a number Centrale is not sure of.

**And it destroys that state or nothing.** The measurement travels with the
second click, so the two are one statement rather than two hopeful requests:
if the branch tip moved or the uncommitted files changed in between — an
agent committing, a file written, another window acting — the server refuses
with a 409 that says what moved, before it tags, removes or deletes
anything. The way on is another first click: a fresh measurement, whose
confirm names the numbers as they are now. Every operation that creates or
destroys a task's worktree or branch (spawn, resume, merge, clean up,
discard, abandon) also runs one at a time per task, so two of them can never
interleave their git operations on the same branch — while the same
operations on *other* tasks are unaffected.

**The irreversible part is recoverable.** Deleting an unmerged branch is the
only irreversible act in Centrale, so before anything is removed the branch
tip is tagged `abandoned/task-<id>-<timestamp>` and the response, the toast
and a copyable line in the drawer all carry the tip SHA and the exact
command that puts the branch back:

```bash
git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015
```

The tag is not decoration. After `git worktree remove --force` followed by
`git branch -D`, no reflog anywhere still references the tip — the
worktree's reflog goes with the worktree and the branch's with the branch —
so the commits are unreachable immediately and one `git gc --prune=now`
destroys them. Without the tag the recovery command would stop working at an
unpredictable moment. When you are sure you don't want the attempt back,
drop the tag: `git tag -d abandoned/task-9-20260904-163012`, or sweep them
with `git tag -l 'abandoned/*'`.

Until you do, the tag is also what lets the next Spawn on that task say
"previous attempt discarded" rather than "claimed elsewhere" — see "Spawn
guards" above.

**What neither action touches.** Not the backlog task's status: discarding
an attempt is not the same statement as changing where the task stands, and
that call is yours to make on the board. Not the main checkout. And not
anything at all while a live tmux session is running for the task (end it
first) or while the branch is checked out in a worktree Centrale doesn't
manage — both refuse with a 409 before any side effect, the second with the
same sentence Spawn, Resume and clean-up use. While a session is live the
drawer still *shows* Merge and both throwaway actions, disabled and naming
the session that blocks them — see "Ending a session" below.

Neither action is offered for an already-merged branch: the work is on the
base and "Merged — clean up" is the right action there.

## Ending a session

**End session** kills exactly one tmux session — the task's own, matched by
exact name — and touches nothing else: the worktree, the branch and the
task are all left as they are, and Merge becomes available again because
the session that was blocking it is gone. It sits in the task drawer and on
every row of the sidebar's session panel.

**It is offered for every live session, and it is always clickable.** The
one state that takes two clicks is `working`, where the agent's last event
positively says it is mid-turn and a kill would land in the middle of a
tool call: the first click *arms* the button, which re-labels itself
"Agent is mid-turn — end anyway?" (the drawer also spells the full reason
out underneath it), and a second click within the same few-second window
Merge, clean-up and Discard use ends the session. Let the window lapse and
the button quietly returns to "End session" with nothing sent. The drawer's
button and the sidebar row's share one arm, so arming either shows armed in
both, and the second click can land on either. Every other state — `waiting`,
`finished`, `idle`, and `unknown` — ends on one click.

This used to be a disabled button. A fresh spawn reports `working` within
seconds and a long task stays there for an hour, so most End session buttons
on a busy board were grey — and `working` is precisely the state in which
you need one: a spawn by mistake, the wrong task, an agent looping or stuck
in a long tool call. A control you cannot click is no better than one you
cannot see; the arm keeps the protection against a stray click without
taking the action away.

Before that it was an allowlist — `finished`, `idle`, `likely-finished`,
`waiting` — and a live session reading **"state unknown"** got no End
session at all. Combined with the harvest area, which renders no *clickable*
action while a session is live, that left a task with a branch, a dirty
worktree and nothing whatsoever to click: the only way out was
`tmux kill-session`, which is the escape hatch Centrale exists to remove.

**That state is common, not exotic.** Lifecycle badges are held in memory,
so restarting the server clears them (which
[docs/operations.md](operations.md) tells you to do after any merge
touching Python). A badge rebuilds from the *next* event a session sends —
and an agent sitting idle at a prompt sends nothing, possibly for hours. So
one restart can leave every running session reading "state unknown" until
somebody types into it. Centrale's server has never gated
`POST /api/end-session` on agent state; this rule only ever lived in the
frontend, and now it matches.

The same principle applies to what a live session blocks. Merge, "Abandon
worktree, keep branch" and "Discard attempt" cannot run while
an agent is working in the worktree — the server refuses all three with a
409 — so the drawer renders them disabled, carrying one line that names the
live session and points at End session above it, rather than rendering an
empty area that looks like a missing feature.

## Agent lifecycle events

Lifecycle badges aren't inferred from CPU or tmux pane activity — they are
driven by agent events. Every spawned/resumed session's environment carries
`CENTRALE_EVENT_URL`. A built-in codex session, for example, gets
`http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-2&agentKind=codex`;
custom/legacy agents keep the original URL without `agentKind`. A `POST`
to that URL with a JSON body `{"state": "working" | "waiting" | "finished"}`
updates the raw lifecycle event.

Centrale exposes the resulting `"agentState"` and `"agentKind"` on the
matching task in `GET /api/board` and row in `GET /api/sessions`.
`agentState` is `"unknown"` until an event arrives. Claude events retain
their `working`/`waiting`/`finished` meanings; a codex `finished` event
is exposed as `"idle"`, because codex uses the same turn-end signal when it
is genuinely done and when it has asked a plain chat question.

For the two built-in agent families, Centrale wires this up automatically,
with no config required:

- **claude**: spawn/resume appends `--settings <generated file>` to the
  launched command, pointing at a small Claude Code hooks settings file
  Centrale generates under its cache dir (`~/.cache/centrale/hooks-
  settings.json`, or `$XDG_CACHE_HOME/centrale/...` — never the user's own
  `~/.claude` config or the target repo). That file maps
  `UserPromptSubmit`/`PreToolUse` to `working`, `Notification` to
  `waiting`, and `Stop` to `finished`, each running `centrale_notify.py`
  (shipped at this repo's root) with the corresponding state. The file
  itself is agent-generic — task identity travels through
  `CENTRALE_EVENT_URL` in the session's own environment, not anything
  encoded in the settings file — so it's generated once and reused across
  every claude-family spawn/resume.
- **codex**: on a codex with the hooks engine (>= 0.150.0), spawn/resume
  passes the same four hook definitions claude's settings file uses —
  `UserPromptSubmit`/`PreToolUse` to `working`, `PermissionRequest` to
  `waiting`, `Stop` to `finished`, each running `centrale_notify.py` with
  the corresponding state — as **inline `-c` config overrides on the codex
  argv itself**, one per hook point:
  ```
  -c hooks.UserPromptSubmit=[{hooks=[{type="command",command="python3 <abs>/centrale_notify.py working"}]}]
  -c hooks.PreToolUse=[{hooks=[{type="command",command="python3 <abs>/centrale_notify.py working"}]}]
  -c hooks.PermissionRequest=[{hooks=[{type="command",command="python3 <abs>/centrale_notify.py waiting"}]}]
  -c hooks.Stop=[{hooks=[{type="command",command="python3 <abs>/centrale_notify.py finished"}]}]
  ```
  (`server.codex_hooks_overrides()` builds these; the value on each is a
  TOML inline array-of-inline-tables matching codex's hooks.json schema
  one level deep — full claude-parity fidelity, not just a single
  `finished` event.)

  The raw `finished` event is not presented as a confident finished badge
  for codex. Codex's `PermissionRequest` is only a tool-approval signal; a
  turn ending on a plain chat/design question fires the same `Stop`/notify
  sequence as a genuinely completed turn. Centrale therefore exposes both
  cases as the neutral `idle` badge, "turn ended · may need input", with a
  tooltip explaining the trade-off. Claude remains unchanged because its
  `Notification` hook distinguishes waiting from finished. A future
  codex-side awaiting-user-input event is the real way to recover that
  distinction; Centrale deliberately does not sniff message content or guess
  from timing.

  **This replaced an earlier approach (task-42) that instead wrote a
  generated `<worktree>/.codex/hooks.json` file, git-excluded from that
  worktree.** Live debugging of a stale `finished` badge on a real task,
  in another project using this dashboard, found why that file was
  silently never read: **codex resolves its
  *project* config layer by walking up through the worktree's `.git`
  FILE to the MAIN repo checkout it points at, not the worktree
  directory itself** — so a worktree-local `.codex/hooks.json` sits
  completely outside the config layer codex ever consults for a spawn
  running in that worktree. This was proven empirically, not just
  inferred: an identical hooks.json plus identical flags fired the full
  `working`/`waiting`/`finished` event stream when run from a plain
  directory under a trusted root, and fired *nothing* (silently
  degrading to notify-only) from a real linked worktree — same file,
  same flags, only the directory kind differed. Passing the same four
  hook definitions as inline `-c` overrides on the argv instead
  sidesteps that layer resolution entirely, since an override never
  touches the filesystem and so has no config *layer* for codex to
  resolve to the wrong root — proven, the same way, to fire the full
  event stream from a linked worktree. **If you're extending codex hook
  injection in the future: don't regress to a file-based approach for
  anything that needs to work from inside a spawned worktree — it
  silently won't be read.**

  Whether a given codex binary is new enough to safely receive any of
  this is never inferred from the overrides themselves — appending config
  overrides succeeds unconditionally regardless of codex version, so it
  feature-detects nothing. Instead, Centrale probes the binary directly:
  once per distinct binary path (cached in memory), it runs `<codex
  binary> --help` and checks whether the output advertises
  `--dangerously-bypass-hook-trust`. Only on a positive probe are the four
  overrides and the trust flag appended at all; on a negative probe, or a
  failed probe, neither is — this matters because an older codex doesn't
  recognize `--dangerously-bypass-hook-trust` (and may not recognize the
  `-c hooks.*` overrides either) and would reject the whole argv at
  startup, silently killing the spawned session, so this has to be known
  *before* ever appending them, not discovered by trying. On a positive
  probe, spawn/resume also appends `--dangerously-bypass-hook-trust` to
  the codex argv: codex prompts to trust a hook source the first time it
  sees one, which would otherwise hang a detached, unattended spawn
  forever; the flag's own documented intent is "automation that already
  vets its own hook sources", which is exactly this case — Centrale
  generates the override values itself, from a fixed template, with
  nothing user- or repo-supplied ever landing in them. No file is written
  and no git-exclude call is made for a codex spawn any more — there is
  nothing left to clean up or exclude. The belt-and-suspenders `-c
  notify=["python3", "<abs path>/centrale_notify.py", "finished"]` override
  (overriding codex's own `notify` setting for just this session) is
  always appended too, regardless of the probe result — on an older
  codex, or on a probe failure, this is the fallback that still gets at
  least the `finished` event (`agent-turn-complete`) through; the two
  overlapping `finished` reports on a hooks-capable codex are harmless
  since `centrale_notify.py` posting the same state twice is idempotent.

`centrale_notify.py` itself is deliberately tiny and paranoid: it reads
the state from its first argument, ignores anything past that (codex
always appends a JSON payload argument describing the completed turn),
POSTs `{"state": "<state>"}` to `$CENTRALE_EVENT_URL` with a 2-second
timeout, and swallows every possible failure — the env var
missing/unset, a network error, a timeout — always exiting 0 with no
output. A hook must never be able to break or hang the agent it's
attached to. Every spawn/resume points at `centrale_notify.py`
directly.

Any other agent — a fully custom `cmd`, or the `CENTRALE_SPAWN_CMD` test
override — gets
`CENTRALE_EVENT_URL` in its environment and nothing else; Centrale
doesn't know how to hook it. That is the whole contract for a custom
agent: if it (or a hook/wrapper you configure around it) chooses to
`POST` `{"state": "working" | "waiting" | "finished"}` to that URL at
the right moments, it participates in `agentState` reporting. With no
built-in `agentKind`, its raw states retain their legacy meanings. See
[docs/api.md](api.md#agent-lifecycle-events-the-centrale_event_url-contract) for the full request/response contract.

The same contract works for a session Centrale never spawned at all — a
Claude Code or codex session you started yourself in a foreign worktree
(see "Branches worked outside Centrale" above), a plain terminal, anything
(task-70). Nothing in it depends on tmux or on Centrale having created the
worktree: the event URL is just a query string naming the project and
task. To opt such a session in to live badges, export the URL yourself and
have the session call `centrale_notify.py` at the right moments — for
example from your own Claude Code hooks, or by hand:

```bash
export CENTRALE_EVENT_URL='http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-7'
python3 /path/to/centrale/centrale_notify.py working    # ... and later:
python3 /path/to/centrale/centrale_notify.py finished
```

Keep the URL's host as `127.0.0.1` (or `localhost`, or `[::1]`) at the port
Centrale bound. Since task-122 the server refuses any request whose `Host`
header is not one of those — that is what stops a rebound hostname
impersonating loopback — and your HTTP client derives `Host` from this URL,
so a LAN name or an `/etc/hosts` alias for the same machine gets a `403`.

Add `&agentKind=codex` for a codex session so its turn-end reads as `idle`
rather than `finished`, exactly as a Centrale-spawned codex does. The board
then shows the badge on that task like any other; with no event, an
external checkout still shows its "worked externally" state and last-commit
age, derived from git alone. No new mechanism is involved — this is the
existing custom-agent contract, used from outside.

Agent lifecycle metadata is ephemeral runtime cache, consistent with the rule
that Centrale stores no task data of its own: state and built-in kind live
entirely in memory, keyed by `(project, taskId)` — never by tmux session
name, so they survive a session being killed and re-spawned/resumed — and
are empty again after every server restart. Every task then reads back
`agentState: "unknown"` and `agentKind: "unknown"` until an event arrives;
the next built-in event restores both from that session's URL.

## Multi-agent workflow

For how the pieces below fit together internally, see
[docs/architecture.md](architecture.md).

Several agents can work in parallel without turning integration into a
guessing game. Keep the loop disciplined:

1. Create small Backlog tasks whose expected files do not overlap. Add task
   dependencies when one change must land before another can start.
2. Turn on **Ready to start** and spawn agents from that filtered view. This keeps
   blocked work out of flight and gives each agent its own branch and worktree.
3. Watch each session's touched files in the sidebar. If two agents begin
   changing the same file, stop and resolve the overlap before they diverge.
4. Review and merge one completed branch at a time. Run the relevant tests
   after every merge, against the newly updated target branch, before merging
   the next agent's work.
5. Once a branch is merged or abandoned, clean up after it. For an attempt
   you are throwing away, the drawer does it — see "Discarding a bad
   attempt" above; for anything else, follow "Cleanup" in
   [docs/operations.md](operations.md#cleanup): kill its tmux session,
   remove its worktree, and delete the task branch.

## `CENTRALE_SPAWN_CMD`

The command launched inside the tmux session normally comes from the
assignee-driven agent selection described in "Agent selection" above,
defaulting to `claude`. Set the `CENTRALE_SPAWN_CMD` environment variable
to override it unconditionally — when
set, it wins over agent selection entirely (Centrale won't even look up
the task's assignee), and no `CENTRALE_AGENT` environment variable is set
on the session. The value is `shlex`-split into an argv list before use.
For example:

```bash
CENTRALE_SPAWN_CMD='sh -c "sleep 300" centrale-probe' python3 server.py
```

launches a harmless sleeping process instead of a real coding agent — this is
how the [manual smoke test](operations.md#manual-smoke-test) avoids ever starting a real agent, and it is also
how you'd point Centrale at a different agent CLI entirely.

Whatever you set must **accept the prompt as a trailing argument**, because
Centrale always appends it: the session runs `<spawnCmd...> "<prompt>"`. That
is why the probe above wraps `sleep` in `sh -c` rather than using a bare
`CENTRALE_SPAWN_CMD="sleep 300"` — a bare `sleep` would receive the prompt as
a second time interval, exit immediately with `invalid time interval`, and
take the tmux session down with it.

The unit tests do not rely on this variable to stay safe: they mock the
subprocess boundaries and assert on the argv that *would* have been run, so
no agent process is ever started by the test suite.

## Without tmux

Centrale detects whether `tmux` is on `PATH` once at startup (`shutil.which`)
and exposes that as `{"capabilities": {"tmux": true|false}}` on every
`GET /api/board` response. When it's `false`:

- The board, drawer, "open board" button, filters, and search all work
  exactly as normal — none of that depends on tmux.
- Every "Spawn" button (inline on a card, and in the drawer) is disabled,
  with a tooltip explaining why: "Spawning requires tmux (e.g. sudo apt
  install tmux)".
- A dismissible banner says the same thing once, at the top of the page.
  Dismissing it is remembered in `localStorage`, so it won't reappear on
  every reload — only the individual button tooltips stay as a permanent
  reminder.
- The sidebar's Sessions section shows "tmux not available" instead of the
  ambiguous "No active sessions".
- `POST /api/spawn` itself also refuses with a 503 and the same message if
  called directly (e.g. via `curl`) while tmux is unavailable, so a stale
  page or a direct API call can't get further than the UI would.
- Merging is unaffected: gate 1 ("no live session") simply evaluates as
  passing — there's no tmux server to have a live session on — and the
  remaining gates don't touch tmux at all, in click or auto mode.

Install `tmux` (e.g. `sudo apt install tmux`, `brew install tmux`) and
restart Centrale to enable spawning.
