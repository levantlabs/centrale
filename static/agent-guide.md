# Operating Centrale as an agent

Centrale is a local dashboard over Backlog.md, git worktrees and tmux. Backlog
is the task source of truth. Centrale spawns workers and gates integration;
it never decides a task is Done. This guide comes from the running server.

```sh
CENTRALE_URL='{{CENTRALE_URL}}'
curl --fail --silent --show-error "$CENTRALE_URL/api/board"
curl --fail --silent --show-error "$CENTRALE_URL/api/settings"
```

Find your repository's configured project name on the board (it need not
equal the directory name). Settings lists configured agents and the default.
POST bodies must be JSON with `Content-Type: application/json`. Read both the
HTTP status and response body. Keep task work on the Backlog CLI: start with
`backlog instructions overview`, read the task and standing decisions, and
follow the execution/finalization guides.

## Owner questions (optional convention)

Projects may use the fixed `needs-owner-approval` label when a task needs an
owner decision, including approval before work starts. Add the label through
Backlog and state the question in a task comment; the worker stops until the
owner answers. The latest comment is the question shown in Needs you, with the
task title as fallback when there are no comments.

Whoever records the owner's decision writes it on the task and removes
`needs-owner-approval` through Backlog. For a spawned task, send the decision
and label-removal instruction through `/api/rule` so the worker remains its
sole writer. Label removal ends the question; Done status or a final summary
also excludes it. Centrale only reads this convention across configured
projects: it never adds or removes the label and enforces no approval gate.

## Spawn and choose an agent

After the owner authorizes work, substitute the actual project, task ID and
configured agent name:

```sh
curl --fail-with-body --silent --show-error "$CENTRALE_URL/api/spawn" \
  -H 'Content-Type: application/json' \
  -d '{"project":"my-app","taskId":"TASK-2","agent":"codex"}'
```

Explicit `agent` wins; otherwise the task's first assignee must name a
configured agent. Names match case-insensitively, with an optional `@`.
With default `requireAgentAssignment: true`, an unassigned task or human
assignee produces 409: retry with an explicit configured agent. With that
setting off, Centrale may use the default and returns a warning.

Success returns `session`, `attach`, `agent`, and optional `warnings`. It
claims and commits the task, creates/reuses `task/<task-id>` in a managed
worktree, and launches the worker there. Inspect warnings: a failed claim
does not prevent launch. After a lost response, reconcile through
`GET /api/sessions` and `GET /api/board` before retrying.

409 also means an existing live session, a Done task, or a branch checked out
outside Centrale. Read the reason; do not create a competing worker. For
existing managed work without a live session, `POST /api/resume` with
`{"project":"my-app","taskId":"TASK-2"}` continues the attempt. An external
checkout must be handed back by its owner. 400 means invalid input/agent;
404 means unknown project; 503 means tmux is unavailable.

### Project launch settings

Use Centrale's spawn API for workers. Each project's `maxAgents` and
`worktreeLinks` live on its entry in Centrale's `projects.json`, editable
in Settings next to its test command. For example, `maxAgents: 4` and
`worktreeLinks: [".venv"]` provide a four-session cap and a shared environment
without a separate launch script. `GET /api/settings` exposes both as maps
keyed by project name; `POST /api/settings` accepts partial maps:

```json
{"maxAgents":{"my-app":4},"worktreeLinks":{"my-app":[".venv"]}}
```

`maxAgents` must be a positive integer; omitted or `null` means no cap.
Spawn and resume count live tmux sessions on each request. At the cap,
409 names the sessions: wait for work to finish, review and end an eligible
session, then retry. A finished/idle badge alone does not free a slot.

`worktreeLinks` is an array of canonical repo-relative paths; omitted,
`null` or `[]` means none. New worktrees link to the main checkout's paths,
sharing their contents. Reused worktrees stay unchanged. Centrale adds
the paths to `.git/info/exclude` and verifies they are ignored before
linking; do not force-add a link to git. Missing sources, destination
conflicts or failed exclusions produce warnings and skip that link.
Discard, abandon and cleanup remove the links, leaving their targets intact.
Paths cannot be absolute, overlapping, contain `.`/`..`/`.git` components,
empty components, leading/trailing whitespace, backslashes, ASCII control
characters, or `*?[]!#` pattern characters.

Put task requirements on the task before spawning, as description or
acceptance criteria; after spawning send changes through `/api/rule`.

## Rule on a spawned task

The spawned task file has one writer: its worker, on the task branch. Never
edit the main checkout's copy while that task is spawned. Never `chmod` a
locked task file to bypass EACCES. Its read-only bit is intentional.

```sh
curl --fail-with-body --silent --show-error "$CENTRALE_URL/api/rule" \
  -H 'Content-Type: application/json' \
  -d '{"project":"my-app","taskId":"TASK-2","sender":"orchestrator","text":"Keep the existing API compatible"}'
```

Use your sender name and a one-line ruling; the delivered line must fit 1000
characters. With a live worker, Centrale delivers `[ruling from <sender>]
<text>` and returns `mode: delivered`. Verify `ok` and `outcome`. Codex can
accept a mid-turn message in its “Messages to be submitted after next tool
call” queue (`↳ <text>`); that counts as delivered, not yet acted on.
A dialog refuses the send; `no-echo` means delivery was not confirmed, not
that the worker did not receive it. Read the pane before retrying to avoid
duplicates. Without a live worker, it commits
the comment on the managed task branch and returns `mode: committed`. 409
explains an unavailable/dirty worktree; resolve that state without editing
main's task. The worker receiving a ruling must FIRST record it with
`backlog task edit TASK-2 --comment "<text>" --comment-author <sender>`, then
act on it.

## Review, end the session, then harvest

Workers check acceptance criteria with evidence, add notes, mark Done through
Backlog, and commit all work on their task branch. Workers must not merge
their own branch into the default branch or delete it.

Merging is the orchestrator's job. **The recommended way to merge is through
the API: end the session, then harvest** (below). Harvest merges only when
every gate passes, on the merged tree, and re-checks the destination right
before merging, so a merge never rests on the worker's own claim that it is
done. A project may still use its own merge flow; that is for its lead to
decide, and this guide does not forbid it.

Read `GET /api/task?project=my-app&id=TASK-2`: `branchTask` is the worker's
task; main's task may still say In Progress. Check sessions. When review and
conversation are complete, end a live session, then harvest:

```sh
curl --fail-with-body --silent --show-error "$CENTRALE_URL/api/end-session" \
  -H 'Content-Type: application/json' -d '{"project":"my-app","taskId":"TASK-2"}'
curl --fail-with-body --silent --show-error "$CENTRALE_URL/api/harvest" \
  -H 'Content-Type: application/json' -d '{"project":"my-app","taskId":"TASK-2"}'
```

End-session kills the session and processes left in its worktree; it does not
complete or merge the task. It can kill a working agent, so review first.
A 404 means no live session; reconcile before continuing. Harvest can return
HTTP 200 with `merged: false`: inspect failed gates. Only `merged: true`
confirms this attempt merged; `alreadyMerged: true` means already integrated
or gone. `GET /api/harvest?project=my-app` evaluates gates, including tests,
without merging.

| Gate | Meaning and next step |
|---|---|
| `noLiveSession` | A session exists. Finish the conversation and use end-session. |
| `taskDone` | Branch task is not Done, criteria remain unchecked, or the task cannot be read. Have the worker complete/record work. External checkouts also refuse here. |
| `worktreeClean` | Uncommitted work or unverifiable checkout. Have the worker commit its work. |
| `mergeClean` | Trial merge conflicts. Resume to reconcile the base INTO the task branch, test and commit there. |
| `checkCommand` | Configured check failed on the merged tree or timed out. Read the reason/output and send the worker back to fix it. |
| `mainCheckoutClean` | Destination has staged changes anywhere, or unstaged/untracked files overlapping the merge. Have their owner resolve them; never discard someone else's edits. Unrelated unstaged files are allowed. |

For Codex, inspect `/api/resume`'s `conversationId` and `conversationStatus`
before ruling: `resumed` means a verified worktree UUID with a normal composer;
`fresh` means no eligible conversation existed and the prior-work prompt was
used. `dialog` and `unconfirmed` both mean `resumed: false`; inspect the pane.
Rulings refuse resume pickers, working-directory dialogs, and the transitional
“Resuming session…” screen. A custom Codex
`resumeCmd` refuses; remove it to enable verified UUID selection.

`POST /api/resume` accepts `reconcile: true` for a branch behind its base;
this asks the worker to reconcile on its branch. Re-run harvest after the
worker finishes and its session is ended. When a gate fails, fix what it
names and harvest again; don't merge by hand to get past it.

## States and card badges are observations

- `working`: the live agent reported activity; `waiting`: it requested input.
  Codex permission requests wait 30 seconds and need a visible dialog before
  the waiting badge or wait event appears; automatic approvals stay quiet.
- `finished`: a Claude turn ended, inviting review, not proof of Done.
- `idle` / "turn ended · may need input": Codex emits the same event for done
  and asking a question, and an idle event can arrive while it is still
  working. Read the current screen before acting on a Codex idle event.
- `unknown`: no usable hook signal, e.g. a session that was `working` when the
  server restarted. A restart restores only settled states (`finished`, `idle`,
  `waiting`) of the same session. Inspect the session.
- "likely finished": a live unknown-state agent whose branch task says Done,
  shown in the drawer. This is an inference, not a hook or passed merge gate.
- "ready": Backlog dependency readiness, not merge readiness. "interrupted":
  unfinished work remains in a managed worktree without a live session.
- "worked externally": branch checked out elsewhere; its owner must hand it
  back. A parked branch has no checkout.
- "merged — clean up": integration happened but artifacts remain; use
  `POST /api/cleanup-branch`, never force-delete the branch yourself.
- A locked task indicator means main's task copy is read-only while spawned;
  use `/api/rule`. Main status, hook badges and branch completion can differ.

## Wait for the next event instead of polling

```sh
curl --fail-with-body --silent --show-error --max-time 70 --get \
  "$CENTRALE_URL/api/orchestrator-wait" \
  --data-urlencode 'project=my-app' --data-urlencode 'timeout=60'
```

The response is one plain-text line: `CURSOR TASK-2 waiting for input`,
`CURSOR TASK-2 finished (ready to review)`, `CURSOR TASK-2 merged`,
`CURSOR TASK-2 merge blocked: GATE: REASON`, or Codex's ambiguous idle line.
Split at the first space. Handle the event, then send its opaque cursor as
`--data-urlencode 'after=CURSOR'` on the next call. An initial call without
`after` starts at the oldest event in this server run. Each caller owns its
cursor; retrying it replays the event. Act on freshly read state: notifications
describe the past and do not authorize actions.

`CURSOR nothing yet` is normal timeout: call again with that cursor.
Timeout is 0–300 seconds (default 60); use a longer client timeout.
History is in memory. 409 means unavailable cursor (restart, wrong project,
future position): drop the cursor and wait without `after`. After a restart
the first lines name every live session once, e.g. `TASK-2 finished (ready to
review) (state before the server restart)` or `TASK-3 state unknown after the
server restart (...)`: handle each (inspect unknown ones), reconcile actions you
had in flight, then continue with cursors as usual.
Waiting consumes no events and blocks no other API requests.
