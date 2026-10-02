# Centrale API reference

Every endpoint here is served by `server.py`'s `Handler.do_GET`/`do_POST`
(routing at the bottom of that file), with `POST /api/spawn`, `/api/resume`,
and `/api/harvest`'s heavy lifting delegated to `spawn.py`/`harvest.py`
respectively, and `/api/browser`/`/api/settings` to `browser.py`/`settings.py`.
See [../docs/architecture.md](architecture.md) for how the modules fit
together, and the manual chapters — [board.md](board.md), [agents.md](agents.md),
[merging.md](merging.md), [configuration.md](configuration.md) — for the
product-level behavior (the board, spawning, merging, settings) each endpoint
backs; the root [README](../README.md) is the front door.

Responses are JSON except successful `GET /api/orchestrator-wait` answers,
which are one plain-text line. All examples below assume the shipped default port
(`7420`) and are copy-pasteable as-is against a running server — the `GET`
ones were captured from a real instance; the `POST` ones are shaped from the
code (see each section for how, since most of them mutate real state — a
worktree, a tmux session, a git branch, `projects.json` — and aren't safe to
fire blind against a repo you care about).

The server binds `127.0.0.1` only and has no authentication of any kind — see
"Running it" in [docs/operations.md](operations.md#running-it).

## Request requirements

Centrale has no authentication, but it does refuse requests that could not
have come from its own UI. Three rules are checked on **every** request,
`GET` and `POST` alike, before any routing — so they apply to every endpoint
below without exception, and a refused request reaches no `git`, `tmux`,
`backlog`, `checkCommand`, temporary worktree or session capture. A fourth
applies to `POST` only.

**If you are `curl`, a script or a hook, all four are already satisfied**
and there is nothing to do beyond addressing the server by a loopback name
and (on `POST`) sending JSON. See "Calling the API from a script" below.

**1. `Host` must be a loopback name at the port Centrale bound.**
`127.0.0.1`, `localhost` or `[::1]`, at the port the process is actually
listening on — the port comes from the bound socket, never from the header,
so the header cannot vouch for itself. A bracketed IPv6 literal is
understood; an omitted port means `80`. Anything else is `403`:

```json
{"error": "refused: request for a host this server does not serve (rebind.example:7420). Centrale accepts requests only from its own origin (see docs/api.md, \"Request requirements\")."}
```

This is the rule that stops **DNS rebinding**. If an attacker points
`evil.example` at `127.0.0.1`, your browser treats their page as
*same-origin* with Centrale: CORS stops nothing, the `Origin` they send is
genuinely their own and matches, and the response body is readable. The one
header that request cannot fake is `Host` — the browser fills it in with the
name the page was loaded from, and `evil.example` is not a loopback name.

**2. `Sec-Fetch-Site`, if the client sends one, must be `same-origin` or
`none`.** Anything else — `cross-site`, `same-site` — is `403`:

```json
{"error": "refused: cross-site request (Sec-Fetch-Site: cross-site). Centrale accepts requests only from its own origin (see docs/api.md, \"Request requirements\")."}
```

This is the rule that stops a **direct cross-site request**: an `<img>`,
`<script>`, `<iframe>`, form or `window.open` aimed straight at
`http://127.0.0.1:7420/...`. That request carries a perfectly valid `Host`,
sends no `Origin`, and under `<meta name="referrer" content="no-referrer">`
sends no `Referer` either. `Sec-Fetch-Site` is a *forbidden header name*:
page JavaScript cannot set it and no referrer policy suppresses it, so the
browser's own account of where the request came from survives. `none` is a
request you started yourself (address bar, bookmark); `same-origin` is
Centrale's own UI. Requests with no `Sec-Fetch-Site` at all are not browser
requests and pass — see rule 1, which is what covers them.

Rules 1 and 2 are not two spellings of one idea; both were measured against
a real Chromium. A **rebound** page sends no `Sec-Fetch-*` headers at all —
fetch metadata is only appended for a potentially-trustworthy URL, and
`http://rebind.example` is not one — so rule 2 never sees it. A **direct
cross-site** request sends a `Host` of `127.0.0.1:<our port>`, which is
genuinely ours, so rule 1 never sees it. Each catches exactly what the other
cannot.

One consequence worth knowing: a **link from another site** to
`http://127.0.0.1:7420` is `cross-site` too, and is refused. Open Centrale
by typing its address or from a bookmark.

**3. An `Origin` header, if you send one, must be this server's own.**
`http://127.0.0.1:<port>`, `http://localhost:<port>` and
`http://[::1]:<port>` are accepted, where `<port>` is the port the server is
actually listening on. When there is no `Origin`, a `Referer` is checked the
same way. Anything else — including the literal `null` a sandboxed iframe or
a `file://` page sends — is `403`:

```json
{"error": "refused: cross-site request (Origin: https://evil.example). Centrale accepts requests only from its own origin (see docs/api.md, \"Request requirements\")."}
```

A request carrying **neither** header is accepted, because `curl` and
`centrale_notify.py` send neither. That used to be the whole hole: a rebound
page produces exactly that shape, and until rules 1 and 2 existed it reached
`POST /api/settings` and `POST /api/spawn`. Rule 3 is now defence in depth
behind them, and the rule that catches a browser too old to send
`Sec-Fetch-Site`.

**4. `Content-Type: application/json` on every POST.** No exceptions, not
even for endpoints that take an empty-looking body. Parameters are ignored,
so `application/json; charset=utf-8` is fine. Anything else — `text/plain`,
`application/x-www-form-urlencoded`, `multipart/form-data`, or no
`Content-Type` header at all — is `415`:

```json
{"error": "refused: POST bodies must be Content-Type: application/json (got text/plain). Centrale accepts requests only from its own origin (see docs/api.md, \"Request requirements\")."}
```

Those three content types are the only ones a cross-origin HTML form can
produce; anything else makes the browser send a CORS preflight first, which
this server never answers (it emits no `Access-Control-*` headers at all, on
any route).

**Identity goes in the JSON body, not the query string.** Every
state-changing endpoint reads `project`/`taskId` from the body only. The one
exception is `POST /api/agent-event`, which keeps its query identity because
`CENTRALE_EVENT_URL` is the only thing a notify hook is handed — a URL is
its sole carrier, and a session's environment is fixed for that session's
lifetime. It still requires a JSON body for the state.

### Calling the API from a script

Every rule above is satisfied by default by any non-browser client:

- **`Host`** is set by your HTTP client from the URL you give it, so just
  address the server as `http://127.0.0.1:<port>` (or `localhost`, or
  `[::1]`). This is the one thing to get right: a URL naming your machine
  some other way — a LAN hostname, an `/etc/hosts` alias, a tunnel domain —
  is refused, including in `CENTRALE_EVENT_URL`.
- **`Sec-Fetch-Site`** is a browser-only header. `curl`, `urllib`,
  `requests`, `wget` and `centrale_notify.py` send none.
- **`Origin`/`Referer`**: send neither, or send Centrale's own.
- **`Content-Type: application/json`** on every `POST`.

`GET` routes are **not** exempt from any of this. Two reasons the earlier
"reads change nothing" carve-out was wrong: `GET /api/harvest` runs the full
merge gate for every task branch, which creates a scratch worktree, performs
a real merge in it and runs the project's own `checkCommand`; and
`GET /api/session-pane` arms `POST /api/session-input` for that session (see
its section below). Nor does CORS make reads safe on its own — it is exactly
what DNS rebinding defeats.

## Errors

Every failure is `{"error": "<message>"}` with a 4xx or 5xx status; the
server never leaks a traceback to the client. The status generally means:

| Status | Meaning here |
| --- | --- |
| 400 | Malformed input — a missing/invalid `project`, `taskId`, request body, or (for `/api/settings`) a field that failed validation. |
| 403 | A request refused by the trust boundary — a `Host` that isn't a loopback name at the bound port, a `Sec-Fetch-Site` other than `same-origin`/`none`, or an `Origin`/`Referer` that isn't this server's own. `GET` and `POST` alike; see "Request requirements" above. (Also `/api/session-pane` and `/api/session-input` when `sessionPreview.mode` disables them.) |
| 404 | The named project (or, for `/api/end-session`, the session) doesn't exist. Also: nothing to act on — no worktree *and* no branch for `/api/cleanup-branch`, `/api/discard-preview` and `/api/discard-attempt`, or no worktree for `/api/abandon-worktree`. |
| 409 | A conflicting state — a session already live for that project+task, a task already Done, a `task/<id>` branch checked out in a worktree Centrale doesn't manage (`/api/spawn`, `/api/resume`, `/api/cleanup-branch`, `/api/discard-attempt` and `/api/abandon-worktree` all refuse this with the same sentence naming that checkout), a repository that moved since the preview a destructive confirm was armed from (`/api/discard-attempt`, `/api/abandon-worktree`), a branch that isn't fully merged yet (`/api/cleanup-branch`), or (`/api/cleanup-branch`, without `force`) a live process still running with its cwd under the worktree. Also `/api/deliver` when a dialog occupies the agent's pane. |
| 504 | `POST /api/deliver` only: the text was sent to the agent's pane, but its echo never appeared (see that section). |
| 500 | An unexpected failure in a subprocess Centrale fully controls (`git`, `tmux`, launching `backlog browser`) — the operation was attempted and didn't work, not a validation problem. |
| 415 | A `POST` whose body isn't declared `Content-Type: application/json` — see "Request requirements" above. |
| 502 | A failure in the `backlog` CLI itself, or a project `path` that no longer exists — Centrale asked an external tool for data and didn't get a usable answer. |

`POST /api/settings`'s 400 is the one exception with extra structure: a
per-field validation failure adds `"fields": {"<field name>": "<reason>"}`
alongside `"error"` (see that section below).

---

## `GET /api/board`

The aggregated kanban board across every configured project.

**Params:** `force` (query, optional) — retained for compatibility with
manual refresh. Every request now validates board files and derives lifecycle
state afresh; even `force=1` reuses unchanged successful CLI inputs.

```bash
curl -s 'http://127.0.0.1:7420/api/board' | python3 -m json.tool
curl -s 'http://127.0.0.1:7420/api/board?force=1' | python3 -m json.tool
```

**Response** (real shape, captured against a running server and genericized —
one project/task trimmed for length):

```json
{
  "projects": [
    {
      "name": "my-app",
      "path": "/home/user/code/my-app",
      "maxAgents": null,
      "error": null,
      "statuses": ["To Do", "In Progress", "Done"],
      "tasks": [
        {
          "id": "TASK-2",
          "title": "Fix the login redirect loop on mobile Safari",
          "status": "Done",
          "priority": "high",
          "assignees": ["@codex"],
          "labels": [],
          "milestone": "m-1",
          "ordinal": 2000,
          "createdAt": "2026-08-29T20:18:00Z",
          "updatedAt": "2026-08-31T21:16:00Z",
          "ready": false,
          "hasSpawnBranch": false,
          "agentState": "unknown",
          "agentKind": "unknown",
          "worktreeDirty": false,
          "branchCheckout": null,
          "alreadyMerged": false,
          "milestoneTitle": "mobile-fixes",
          "lastDiscardedAt": null,
          "taskFileLocked": false
        }
      ]
    }
  ],
  "capabilities": {"tmux": true},
  "version": "v0.1.0-14-g4570911",
  "codeDrift": null,
  "harvestMode": "click",
  "spawnAgents": {"names": ["claude", "codex"], "defaultAgent": "claude", "required": true},
  "refreshIntervalSeconds": 10,
  "sessionPreviewMode": "interact"
}
```

Each project's `maxAgents` is its configured positive integer cap or `null`
for no cap. The frontend derives the live count from `/api/sessions`; the
server always checks tmux again when launching, independent of board cache.

Every task from `backlog task list --json` round-trips as-is (`id`, `title`,
`status`, `priority`, `assignees`, `labels`, `milestone`, `ordinal`,
timestamps, ...), plus ten fields Centrale adds:

- `ready` — from `isReady` in `backlog task list --json`: unblocked by its
  dependencies.
- `hasSpawnBranch` — a `task/<id>` branch exists, computed with one
  `git for-each-ref` call per project (never one per task). It says nothing
  about whether that branch is merged — see `alreadyMerged` below.
- `agentState` — `"working"` | `"waiting"` | `"finished"` | `"idle"` |
  `"unknown"`. A built-in codex `finished` event is exposed as `idle`
  because done and awaiting a chat reply are indistinguishable; Claude and
  legacy/custom agent states are unchanged.
- `agentKind` — `"claude"` | `"codex"` | `"unknown"`, retained from the
  built-in session's event URL; `"unknown"` before an event or for custom
  and legacy reporters.
- `worktreeDirty` — only meaningful (and only computed) when `hasSpawnBranch`
  is true: `git status --porcelain` reports something in that branch's
  worktree, i.e. an agent's session died mid-work. `false` for every task
  without a branch.
- `alreadyMerged` — `hasSpawnBranch` is true, but the branch is already fully
  merged into the base branch *and* the main-side task is Done — a genuine
  out-of-band merge (done by hand, or by an agent ignoring the "don't merge
  your own branch" instruction in its prompt), so the normal Merge button
  would be a stale offer.
- `branchCheckout` — `null` for a task without a branch; otherwise where its
  `task/<id>` branch is checked out, derived from one `git worktree list
  --porcelain` per project (task-70): `{"kind": "centrale" | "external" |
  "none", "path": "<worktree path>" | null}`. `centrale` is the ordinary
  case (checked out at Centrale's own worktree path). `external` means the
  branch was adopted into a worktree Centrale doesn't manage (`.worktrees/`,
  `.claude/worktrees/`, a `/tmp` checkout, ...) and work may be continuing
  there; that kind additionally carries `"lastCommitAt"` (ISO 8601 UTC) and
  `"lastCommitAgeSeconds"` — the branch tip's commit time, the only
  freshness signal Centrale has for work it can't see — and the card/drawer
  render it as "worked externally at `<path>`". `none` is a **parked**
  branch: it exists but is checked out nowhere (its worktree was removed),
  deliberately distinct from `external` so the two render differently.
  `POST /api/spawn` and `POST /api/resume` refuse an `external` branch with
  409 (see below).
- `milestoneTitle` — the title of the task's `milestone`, resolved once per
  project from `backlog milestone list --plain --show-completed` (task-91:
  backlog's task JSON carries a milestone's id and never its title, so the
  board could otherwise only ever label a milestone `m-0`). `null` whenever
  the title can't be resolved — no milestone, or an unreadable milestone
  list — which the frontend renders as the bare id. The id in `milestone` is
  untouched: it is what the frontend filters on, and it survives a rename.
- `lastDiscardedAt` — when this task's most recent attempt was discarded
  (ISO 8601 UTC), or `null` for a task that has never had one. Read from
  the `abandoned/task-<id>-<stamp>` recovery tags `POST
  /api/discard-attempt` leaves behind, with one `git for-each-ref
  refs/tags/abandoned/` per project (never one per task); the newest tag
  wins when a task has been discarded more than once. The **tag name** is
  the source, not the tag's date: these are lightweight tags, so git
  would report the discarded commit's date rather than the moment of the
  discard. Derived on every board load and never stored, so deleting the
  tag (`git tag -d abandoned/task-9-...`, which
  [Discarding](agents.md#discarding-a-bad-attempt) tells you to do once
  you're sure) takes the field back to `null`; a repository whose tag
  listing fails reads the same as one that has never had a discard. The
  frontend uses it for one thing: a task that is In Progress with no
  branch is indistinguishable from one claimed by a worker Centrale
  cannot see, so the spawn confirm names the discard when a tag says the
  user threw the last attempt away themselves.
- `taskFileLocked` — the task's own file in this checkout
  (`backlog/tasks/<id> - *.md`) is read-only: spawned, and locked so the
  agent on its branch is its only writer (task-172; see [Ruling on a
  spawned task](agents.md#ruling-on-a-spawned-task)). Read from the
  files' modes on every load — one directory listing per project, one
  `stat` per task file — never stored. The card shows "task file
  locked"; `backlog task edit` on that copy fails with `EACCES`, and a
  ruling goes through [`POST /api/rule`](#post-apirule) instead.

A project whose `path` doesn't exist, or whose `backlog` CLI call fails or
returns an unsupported schema, gets `"error": "<message>"` and empty
`"tasks"`/default `"statuses"` instead of failing the whole response — every
other configured project still loads normally.

**Top-level flags:** besides `projects`, every response carries
`capabilities` (`{"tmux": bool}`), `version`, `codeDrift`, `harvestMode` (`"click"` |
`"auto"`), `spawnAgents` (task-171: `names`, the configured agents in
`projects.json` order; `defaultAgent`, the effective default; `required`,
`requireAgentAssignment` — what the spawn control needs to name the agent
it will launch, and to offer a picker when the task's assignee names none),
`refreshIntervalSeconds`, and `sessionPreviewMode` (`"interact"` | `"view"` |
`"off"` — the tiered `sessionPreview.mode` knob behind `GET /api/session-pane`
/ `POST /api/session-input` and the drawer's live pane and reply row; the
frontend reads it here so it never has to fetch `/api/settings` just to know
which of those sections to render).

`version` is the build the **running server process** started from —
`version.py`'s constant, with `git describe --tags --always --dirty`
layered on when the checkout has a `.git` (see
[Versioning](operations.md#versioning)). Unlike every other top-level
flag it is resolved **once, at server start**, and only read back
afterwards: a value re-derived per request would report code this
process has never executed. It is what the sidebar footer shows, and it
cannot change without a restart.

`codeDrift` is the other half of that comparison (task-128): whether the
checkout this server runs from has moved past the commit `version` names.
`null` while they agree — or whenever the server cannot tell: no `.git`
next to `server.py` (a downloaded snapshot), no `git` on `PATH`, a git
failure, or a `version` with no recognisable commit in it. Otherwise an
object:

```json
"codeDrift": {"loaded": "055ff59", "current": "a088d15", "commitsBehind": 23}
```

`loaded` is the commit the process started from (the abbreviated hash out
of `version`, or the tag name when `version` was an exact tag); `current`
is `HEAD` of the checkout, abbreviated to that same width (seven
characters when `loaded` is a tag name); `commitsBehind`
is `git rev-list --count loaded..HEAD`, or `null` when git cannot count
(the loaded commit rewritten away). A `-dirty` suffix on `version` is not
drift: uncommitted edits on top of the same commit leave `codeDrift`
`null`. Derived from git on **every** request and never cached or stored,
so it is exactly as fresh as the request. It is what the banner across
the top of the board shows, and what `python3 server.py --check` relays
from a running server — see
[Restarting after a change](operations.md#restarting-after-a-change).

**Caching:** Successful task-list, ready-list and milestone-title CLI inputs
are cached in memory per canonical project path, shared across clients.
Every request checks recursive `backlog/` metadata (names, nanosecond
modification/change times, size, inode and mode), so task, draft, milestone,
archive and config edits, additions, renames and deletions invalidate the
inputs immediately. Concurrent requests for one project share a single load.
Metadata is checked again after the reads; a changing tree is not cached.
Reuse requires a local `backlog/config.yml` or `config.yaml`. Unreadable or
missing trees, symlinks, root `backlog.config.yml` or legacy `backlog.json`
overrides, and an inherited `BACKLOG_CWD` disable reuse: these discovery modes
can redirect the CLI to a different directory. CLI errors are retried on
the next request; failed task reads show the project error with no old tasks,
and failed milestone reads retain the bare-ID fallback without caching it.
No cache file or watcher is created. Restart drops all cached inputs.

Git, lifecycle enrichment, statuses, capabilities and runtime settings are
computed fresh; `version` remains the boot-time value and `codeDrift` checks
HEAD afresh. `force=1` follows the same file-validation path. Task details and
merge gates do not use this cache. Board lists use local CLI inputs; remote branches are not an input. Changing
the installed CLI requires a server restart to discard cached inputs.

## `GET /api/task?project=<name>&id=<taskId>`

One task's full detail (`backlog task view <id> --json`), read from the
project's main checkout.

**Params:** `project` (query, required), `id` (query, required — must match
`^[A-Za-z]+-[0-9]+(\.[0-9]+)*$`, e.g. `TASK-2` or `TASK-2.1`).

```bash
curl -s 'http://127.0.0.1:7420/api/task?project=my-app&id=TASK-2' | python3 -m json.tool
```

**Response** (real shape, trimmed — the same synthetic `my-app` board the
other examples on this page use):

```json
{
  "schemaVersion": 1,
  "kind": "task-view",
  "task": {
    "id": "TASK-2",
    "title": "Cache the widget index between requests",
    "status": "In Progress",
    "priority": "medium",
    "assignees": ["@claude"],
    "labels": ["demo"],
    "path": "backlog/tasks/task-2 - Cache-the-widget-index-between-requests.md",
    "description": "Keep the widget index in memory and invalidate it when...",
    "acceptanceCriteria": [
      {"index": 1, "text": "Index is served from cache on a repeat...", "checked": false}
    ],
    "implementationPlan": "1. Add the cache.\n2. Wire up invalidation...",
    "implementationNotes": null,
    "finalSummary": null
  }
}
```

If this task has a `task/<id>` branch, the response also gets a top-level
`"branchTask"`: the same shape as `"task"` above, but read from that branch
instead of the main checkout. This matters because an agent's Done status,
checked acceptance criteria, and implementation notes are committed only on
its `task/<id>` branch — the main checkout, and therefore the top-level
`"task"` in this response, won't see any of it until that branch is merged
(see "Merging finished branches" in
[docs/merging.md](merging.md#merging-finished-branches)).

Where that read happens follows the branch's own checkout state
(`spawn.checkout_state`, the same classification the board's
`branchCheckout` reports — see [GET /api/board](#get-apiboard)):

| checkout kind | where `branchTask` is read from |
| --- | --- |
| `centrale` | Centrale's own worktree for the task — a plain working-tree read, so an agent's *uncommitted* backlog edits show up too (its live progress). |
| `external` | the foreign worktree the branch was adopted into, read the same working-tree way. The path comes from `git worktree list --porcelain`, never from anything the caller supplied. |
| `none` (parked) | a throwaway *detached* worktree cut from the branch, so the parked branch itself is never checked out (and never reserved). The snapshot is created under `TMPDIR` and removed — registration and directory both — before the response is sent. This is the same read harvest's task-Done gate uses for a parked branch. |

Before that, a task whose branch is checked out in Centrale's worktree costs
no git call at all, and a task with no `task/<id>` branch costs exactly one
`git rev-parse --verify` and nothing else — no worktree listing, no snapshot,
no second `backlog` call.

`branchTask` is best-effort at every step: a worktree that's gone, a snapshot
git refuses, or a `backlog` failure or malformed response reading any of them
just omits the field — it never fails the request, since the main-checkout
`"task"` is still a valid response on its own. The drawer treats an absent
`branchTask` as "not read", never as an answer: its Resume-vs-Re-spawn
decision reads this status (see "Resuming an interrupted agent" in
[docs/agents.md](agents.md#resuming-an-interrupted-agent)) and falls back to
the worktree-dirty signal alone until the field arrives.

**Errors:** 400 for a missing `project` param or a missing/malformed `id`.
404 for an unknown project. 502 if the main-checkout `backlog task view` call
fails or returns an unsupported `schemaVersion`.

## `GET /api/sessions`

Live `centrale-*` tmux sessions Centrale is aware of.

```bash
curl -s 'http://127.0.0.1:7420/api/sessions' | python3 -m json.tool
```

**Response** (real shape, captured against a running server and genericized):

```json
{
  "sessions": [
    {
      "name": "centrale-my-app-task-17",
      "created": "1788228829",
      "attached": false,
      "project": "my-app",
      "agentState": "working",
      "agentKind": "claude",
      "files": [
        "src/auth/session.py",
        "backlog/tasks/task-17 - Fix-session-expiry-race.md"
      ],
      "filesTotal": 4
    }
  ]
}
```

A session whose agent is `finished`, `idle` or `unknown` also carries
`parked`: `null`, or `{"reason", "since", "lastLine", "dialog"}` when it has
been idle for `parkedAfterSeconds` with a dialog on its pane or an
undelivered message (see `needsYou` under `GET /api/fleet`); the sidebar
shows it as PARKED with the pane's last line.

`created` is the tmux session-creation timestamp as a Unix-epoch string
(straight from `#{session_created}`), not milliseconds and not ISO-8601.
`project`/`agentState`/`agentKind`/`files`/`filesTotal` are omitted for
a session whose name cannot be resolved back to a configured project (see
`_parse_session_project_and_task`). Both lifecycle fields remain included
even if the worktree is gone, while `files`/`filesTotal` do not — those
need the worktree to exist. `files` is capped at 20 (`TOUCHED_FILES_CAP`),
with `filesTotal` giving the true count (tracked changes vs. `HEAD` plus
untracked files, deduped). An absent tmux server yields `{"sessions": []}`,
never an error.

**Errors:** 502 if `tmux list-sessions` itself fails unexpectedly (not "no
server running", which is treated as zero sessions).

## `GET /api/session-pane?project=<name>&task=<taskId>[&lines=N]`

The rendered text currently on screen in one live agent session's tmux pane
— what you'd see if you attached — for the task drawer's read-only live
preview. tmux stays the source of truth; this is a thin capture, not a
terminal emulator.

**Params:** `project`, `task` (query, required — same names as
`/api/end-session`). `lines` (query, optional) — how many lines to return,
default 40, clamped to 1–200; anything unparseable falls back to the
default.

```bash
curl -s 'http://127.0.0.1:7420/api/session-pane?project=my-app&task=TASK-2' | python3 -m json.tool
```

**Response** (real shape, captured against a running server and genericized):

```json
{
  "session": "centrale-my-app-task-2",
  "lines": [
    "● I'll start by reading the task and the module it touches.",
    "",
    "  Should I also update the README for the new flag? (y/n)",
    "> "
  ],
  "lineCount": 4,
  "capturedAt": "2026-09-01T21:40:12Z"
}
```

- `lines` — the pane's rendered rows as plain text, oldest first. tmux has
  already resolved every escape sequence into the screen grid, so there is
  no ANSI to strip; trailing spaces on each row are trimmed by tmux, and the
  blank rows tmux pads the visible screen with are dropped from the end
  (interior blank lines are real output and stay). Only the last `lines`
  rows are kept.
- `capturedAt` — server-side UTC timestamp (ISO-8601, second precision) of
  the capture. The UI shows this plus an age counter so a stale capture is
  never mistaken for live output.

**Behavioral notes:**

- **One tmux call per request**, `tmux capture-pane -p -t
  '=<session>:' -S -<lines>` through the injectable `run_tmux` boundary —
  no `list-sessions` preflight. The frontend polls
  this for the one task whose drawer is open, and only while that task
  has a live session, so the feature costs at most one subprocess per
  tick no matter how many agents are running. Its cadence is 2 seconds
  by default, 1 second while the badge says the agent is working, and
  300ms for the 4 seconds after a reply is accepted (task-114); every
  tick asks for 200 lines, of which the drawer renders the last 40 and
  the session theater the whole window.
- The target's leading `=` plus trailing `:` force an **exact
  session-name** match (a bare `=name` is read by tmux as an exact *pane*
  name and fails), so `centrale-app-task-1` can never resolve to
  `centrale-app-task-10`.
- The session name is computed by `spawn.candidate_session_names` — the
  same tmux-safe encoding spawn/resume/end-session use — so a dotted
  subtask id (`TASK-11.2` → `centrale-my-app-task-11_2`) resolves
  correctly.
- **Disable switch:** when `sessionPreview.mode` is `"off"` (see
  `projects.json` / `POST /api/settings`'s `sessionPreviewMode`), this
  endpoint refuses with **403** *before* running anything — turning the
  feature off disables the server side, not just the drawer section. Both
  `"view"` and `"interact"` allow it.
- A successful capture also **arms `POST /api/session-input`** for that
  session for the next 10 seconds (see that endpoint) — a reply is only
  ever allowed against a pane this server has just captured.
- **`lines` is a ceiling, not a promise — the pane's height is the real
  limit.** Agent TUIs (Claude Code, Codex) run on tmux's *alternate
  screen*: they redraw a fixed viewport instead of letting output scroll
  off the top, so nothing ever accumulates in the pane's scrollback.
  Measured on a live spawned session: `history_size=0` against a
  50,000-line `history-limit`. `-S -200` prepends scrollback that does
  not exist, so the capture is the visible screen and nothing more, and
  `lineCount` comes back as the pane's height however large `lines` was.
  (An ordinary shell session in the same tmux scrolls and accumulates
  history normally — the same call against one returns the full 200.
  It is alternate-screen behaviour, not a Centrale bug.) Raising the
  ceiling therefore means making the pane taller, which is what
  `spawn.SESSION_GEOMETRY` does at session-creation time — see "How a
  spawn flows" in [docs/architecture.md](architecture.md).
- **The maximum is reachable, deliberately.** A session Centrale spawns
  is created `spawn.SESSION_GEOMETRY` = 220 x `MAX_SESSION_PANE_LINES`
  rows, so the theater's full-window request is one an alternate-screen
  agent can actually answer. The two are the same constant on purpose
  (task-152): a pane shorter than this endpoint's ceiling is a UI asking
  for lines that can never arrive, and that is exactly what a 50-row
  pane against a 200-line request was.

**Errors:** 400 for a missing `project` or an invalid/missing `task`. 403
when `sessionPreview.mode` is `"off"`. 404 for an unknown project, or when
there is no live session for that project+task (including no tmux server
running at all). 500 if `tmux capture-pane` fails for any other reason.

## `POST /api/session-input`

Types a single-line reply — or presses one of two keys — in one live agent
session's tmux pane: the drawer and session theater's "reply to a waiting
agent" row. Single-line text plus the keys `Escape`, `Enter`, `Up` and
`Down`; multi-line input stays out of scope (that road ends at an
embedded terminal, which this is not).

A menu is answered like a terminal (task-184): with the session view
focused in the browser, Up/Down/Enter/Escape are sent as real keypresses,
and clicking a numbered option line sends one `Up`/`Down` per step from the
highlighted option (Claude's `❯`, Codex's `›`) to the clicked line, then one
`Enter` — as separate requests, in order, stopping at the first failure. A
click outside a menu sends nothing. Never send `Enter` after a key the menu
already consumed: it would answer the next prompt.

**Body:** a JSON object carrying the identity and **exactly one** of the
two inputs — `{"project", "taskId", "text"}` or `{"project", "taskId",
"key"}`. `taskId` is also accepted as `task`. A query string is ignored
entirely (see "Request requirements"). The endpoint never accepts a session
*name*: the session is always derived from project+task.

`"text"` is one line of printable text, 1–1000 characters, with no newline
or other control characters. It is delivered literally, then followed by
Enter.

`"key"` is one of exactly `"Escape"`, `"Enter"`, `"Up"` or `"Down"`, sent as a tmux key
*name*. Allowlisted rather than free-form because tmux silently sends an
unknown key name as literal text — an open-ended `"key"` would be a second
text channel with none of the text validation above.

Beyond these fields every other one is rejected as unknown, and a body
carrying **both** `"text"` and `"key"` is a 400 rather than a guess about
which was meant.

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/session-input' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "text": "yes, and update the README too"}'

# ...and the same session, dismissing a dialog that is blocking it:
curl -s -X POST 'http://127.0.0.1:7420/api/session-input' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "key": "Escape"}'
```

**Response** (shaped from the code):

```json
{
  "ok": true,
  "session": "centrale-my-app-task-2",
  "sent": {"text": "yes, and update the README too"},
  "captureAgeSeconds": 1.8
}
```

`sent` echoes what was delivered — `{"text": ...}` or `{"key": ...}`.
`captureAgeSeconds` is how old this server's latest pane capture of that
session was when the reply went in — the same number the drawer shows next
to the input.

**Behavioral notes:**

- **tmux does the typing**, through the injectable `run_tmux` boundary, with
  the same exact-match target as `/api/session-pane` (`-t '=<session>:'`).
  Text goes by tmux's **bracketed-paste** path: `tmux load-buffer -b <buf> -`
  (the text on stdin, into a fresh uniquely named buffer — so a leading
  dash, a `;`, or a word that happens to be a key name like `Enter` never
  meet an argv), then `tmux paste-buffer -d -p -b <buf> -t '=<session>:'`
  (`-p` wraps the text in the bracketed-paste control codes when the
  application has requested that mode — Claude Code and codex both do;
  `-d` deletes the buffer once pasted), then a separate
  `tmux send-keys -t '=<session>:' Enter`. Each reply uses these three
  subprocesses; sends are user actions, not a poll.
- **Why paste, not `send-keys -l`:** typing the text as an unmarked
  keystroke burst let agent TUIs *guess* it was a paste from inter-keystroke
  timing, and the Enter that arrived inside that guess window was absorbed
  as pasted input instead of submitting — the reply landed in the composer
  and the user had to attach and press Enter by hand (reproduced against
  codex 0.151.0, and a race on Claude Code). An explicit paste terminator
  makes the TUI process paste-then-Enter sequentially off the pty: no
  heuristic window, no timing dependence, and no fixed sleeps anywhere in
  the path. An application that never asked for bracketed paste gets the
  raw bytes (tmux does not skip the paste), i.e. exactly what `send-keys
  -l` sent before — no worse, and no capture-pane-driven Enter retry is
  layered on top, since a plain capture can't tell typed text from Claude
  Code's dim ghost-text prompt suggestions on the same composer line.
- **Keys go by name, never by paste.** A key is one
  `tmux send-keys -t '=<session>:' Escape` (or `Enter`) — never the
  `load-buffer`/`paste-buffer` path above, where the key name would arrive
  as the literal letters `E-s-c-a-p-e`. Bracketed paste is for text.
- **Why these two keys exist, and why only two.** task-77 removed all
  three original quick keys (`y`, `Enter`, `Escape`) end to end, and
  recorded the terms of their return: *if a real itch develops later, one
  button is cheap to bring back with evidence behind it.* task-135 is that
  evidence. A `claude` session can be stopped dead by a startup dialog — an
  onboarding modal offering to scan your shell history, say — and the text
  path cannot answer one: the reply lands where there is no prompt, and the
  `Enter` this endpoint appends to it would confirm whatever option the
  dialog happens to have focused. A session parked that way queues every
  reply behind the modal and needs exactly one keystroke to move, which
  until now meant attaching to tmux by hand. `y` did **not** come back —
  task-77's reason for removing it (bare `y` answers a prompt style the
  current TUIs barely use) still holds — and arrow keys and `Ctrl-C` are
  absent for the same discipline: ship what usage justified, not the whole
  keyboard.
- **The keys do not editorialise.** `Enter` confirms whatever the pane has
  focused, which on some dialogs is a default nobody would choose blind.
  That is the pane's business: the live terminal view directly above the
  buttons is how the user sees what they are about to confirm, and there is
  deliberately no confirm step, warning, or pane preflight layered on top
  of the click.
- **Fresh-capture gate (staleness honesty).** Replies land in a TUI seen
  through a polled capture, so the prompt can change between the capture
  and the keystroke. That is mitigated, not solved: the server refuses
  (**409**) unless *it* captured this session's pane via
  `GET /api/session-pane` within the last **10 seconds**
  (`SESSION_INPUT_MAX_CAPTURE_AGE_SECONDS`). Capture times are process
  memory only — a server restart forgets them, and the reply refuses until
  the drawer has previewed again. Nothing about this makes a reply atomic
  with what was on screen, and the UI copy says so.
- **Ownership check.** The computed session name must round-trip through
  the shared TASK-59-safe reverse lookup (`_parse_session_project_and_task`,
  the same helper the sessions panel uses) back to this same known project
  and task; anything else is refused (404). Combined with never accepting a
  session name from the client, only `centrale-*` sessions attributable to a
  board task can ever be targeted.
- **Disable switch:** refuses with **403** *before* anything else unless
  `sessionPreview.mode` is `"interact"`. `"view"` keeps the read-only pane
  working and refuses replies; `"off"` refuses both. Turning the reply off
  disables the endpoint, not just the drawer row.
- Same trust class as `POST /api/spawn` and `/api/end-session`: localhost,
  single user, no authentication — see "Running it" in
  [docs/operations.md](operations.md#running-it).

**Errors:** 403 when `sessionPreview.mode` is not `"interact"`. 400 for a
missing `project`, an invalid/missing `task`, a missing/malformed body,
neither or both of `text` and `key`, an unknown field, a `key` outside
`["Escape", "Enter", "Up", "Down"]`, or an empty/multi-line/over-long/control-character
`text`. 404 for an unknown
project, a session name that doesn't resolve to a known board task, or no
live session for that project+task (the session ended between the capture
and the click; this includes no tmux server running). 409 when there is no
capture of this session within the last 10 seconds — the body also carries
`"captureAgeSeconds"` (`null` if never captured). 500 if any of the tmux
calls fails for another reason (for text, this can happen after the text
was pasted but before Enter went in; the message names the tmux command —
`load-buffer`, `paste-buffer` or `send-keys` — that failed. A failed paste
first deletes the buffer it would have consumed, so nothing lingers in the
tmux buffer list).

## `POST /api/deliver`

Delivers one line of text to a project/task's live agent **and confirms it
arrived**: the one supported way for a program — an orchestrating session,
a script, another agent — to message a spawned agent. Use it instead of
hand-rolling `tmux load-buffer`/`paste-buffer`/`send-keys`: "sent" is a
hope, and this endpoint returns either the echo that proves the text landed
or an explicit failure saying why it did not. Every attempt is recorded in
the delivery log (`GET /api/deliveries` below).

**Body:** `{"project", "taskId", "sender", "text"}` (`taskId` is also
accepted as `task`); every other field is rejected. `text` follows exactly
the rules of `POST /api/session-input`'s `"text"`: one line of printable
text, 1–1000 characters. `sender` is required — a short name for who is
speaking (1–100 characters, one line), recorded in the log with the text.

Unlike `POST /api/session-input`, this needs **no prior pane capture**: it
does its own, before and after. Nor is it switched off by
`sessionPreview.mode`, which governs the drawer's pane and reply row; this
is the orchestration channel, and every use of it is logged.

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/deliver' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "sender": "orchestrator", "text": "Ruling: keep the old flag, deprecate it in the next release"}'
```

**Response** — delivered (`200`), shaped from the code:

```json
{
  "id": "5f0c3c2b9d6e4f5aa1d2c3b4e5f60718",
  "time": "2026-09-26T19:04:11Z",
  "project": "my-app",
  "task": "TASK-2",
  "sender": "orchestrator",
  "text": "Ruling: keep the old flag, deprecate it in the next release",
  "session": "centrale-my-app-task-2",
  "outcome": "delivered",
  "echo": "❯ Ruling: keep the old flag, deprecate it in the next release",
  "echoAt": "2026-09-26T19:04:12Z",
  "dialog": null,
  "reason": null,
  "ok": true,
  "logged": true
}
```

The body is the log entry (below) plus `ok`, `logged` and, when the log
could not be written, `logError` — the delivery result stands, but you are
told it went unrecorded. `echo` is the pane line that showed the text;
`echoAt` is when it was seen. A failure has `ok: false`, the same fields,
and `error` (equal to `reason`):

| `outcome` | Status | Meaning |
| --- | --- | --- |
| `delivered` | 200 | The text was pasted and a capture afterwards showed it echoed. |
| `no-session` | 404 | No live session for that project/task (including no tmux server), or the session ended mid-delivery. |
| `dialog` | 409 | A dialog occupies the pane. Checked **before** anything is pasted — `dialog` carries the line that gave it away and `error` says nothing was sent. Also reported if the echo never came and a dialog is on the pane afterwards (then `error` says the text *was* sent). |
| `no-echo` | 504 | The text was sent, but no echo of it appeared within 5 seconds. |
| `tmux-error` | 500 | A tmux call failed for another reason; `error` names it. |

```json
{
  "outcome": "dialog",
  "dialog": "Enter to confirm · Esc to cancel",
  "reason": "a dialog is occupying the pane ('Enter to confirm · Esc to cancel'); nothing was sent",
  "error": "a dialog is occupying the pane ('Enter to confirm · Esc to cancel'); nothing was sent",
  "ok": false,
  "...": "the rest of the log entry"
}
```

**Behavioral notes:**

- **One paste path.** The send is `POST /api/session-input`'s own
  bracketed paste — `load-buffer` from stdin, `paste-buffer -d -p`,
  `send-keys Enter`, all against the exact-match `=<session>:` target —
  through the same function. Only the checks around it are new.
- **Dialog check first.** Pasted text would land *in* a dialog, and the
  Enter after it would confirm whatever option the dialog has focused. So
  the pane is captured first and a dialog refuses the delivery. A dialog is
  recognised from the rendered text, among the bottom 12 non-blank lines: a
  key-hint footer naming Enter then Esc (Claude Code's `Enter to confirm ·
  Esc to cancel`, codex's `enter continue · esc back` and `Press enter to
  confirm or esc to cancel`), or a cursor on a numbered menu (`❯ 1. Yes`
  next to `2. …`). These were taken from real Claude Code 2.1.283 and codex
  0.157.1 panes (`tests/fixtures/panes/`); it is a heuristic, and a dialog
  that looks like neither will not be caught before the send — it then
  surfaces as `no-echo`, not success. Clear a dialog with
  `POST /api/session-input`'s `{"key": "Escape"}` after looking at the pane.
- **What counts as the echo.** A pane line with a prompt glyph (`❯` Claude
  Code, `›` codex, `>` generic) followed by the message — all of it, or,
  when the TUI wrapped it, a leading run of at least 20 characters of it.
  Whitespace is collapsed. Codex's `↳ <text>` rows under “Messages to be
  submitted after next tool call” also count. An arrow outside that queue
  region does not count, nor does an agent reply quoting the words.
- **An earlier identical message cannot pass for this one.** Echo lines for
  this text are counted before the send (over the last 200 lines,
  scrollback included); delivery needs *more* of them afterwards. The pane
  is re-captured every 0.25 s for up to 5 s. Both measured agents echo a
  submitted line within a second.
- **What the echo does not prove.** That the agent has read or acted on the
  text: it shows the text reached the pane as a prompt or queued message. Sent to a
  *busy* Claude Code agent, the message is shown queued (`❯ <text>` with
  `ctrl+enter to send now` under it) and counts as delivered; the agent
  takes it at its next turn — verified against 2.1.283. Busy Codex 0.157.1
  can show “Messages to be submitted after next tool call” and `↳ <text>`;
  that is delivered too, with the queued row returned as `echo`. A 504
  `no-echo` means confirmation failed, not proof the text was lost: inspect
  the screen before retrying. A plain capture
  also cannot tell a submitted prompt from text still in the composer, nor
  from Claude Code's dim ghost-text suggestion there, which renders behind
  the same glyph and is sometimes a verbatim earlier prompt. So resending
  an *identical* message is the one case where a lost delivery could still
  read as delivered: the baseline count covers the transcript, not a ghost
  that appears afterwards. Vary the wording of a repeat (a counter or a
  time is enough).
- **One delivery per session at a time**, so two concurrent ones cannot
  each count the other's echo.
- The ownership rule is `POST /api/session-input`'s: the session name is
  derived from project+task, never accepted from the client, and must
  resolve back to this board task (else `no-session`).
- **No task file is written.** The record of what was said is the delivery
  log, which is Centrale's, not the board's.

**Errors that are not attempts** — 400 for a missing/malformed body, a
missing `project`, an invalid/missing `taskId`, a missing/over-long/multi-
line `sender`, a missing/empty/multi-line/over-long `text`, or an unknown
field; 404 for an unknown project. These touch no tmux and are **not**
logged; everything else is.

## `GET /api/fleet[?window=<seconds>]`

One snapshot across all configured projects for fleet/timeline views. `window`
is finite seconds greater than zero and at most 172800 (48 hours), default
7200 (2 hours). Invalid, empty or out-of-range values return **400**.

```sh
curl -s 'http://127.0.0.1:7420/api/fleet?window=7200'
```

```json
{
  "timestamp": 1790784000.0,
  "window": 7200.0,
  "retention": 172800,
  "projects": [{"name": "my-app", "maxAgents": 3, "agentCount": 1}],
  "agents": [{
    "project": "my-app", "taskId": "TASK-2", "agent": "codex",
    "agentKind": "codex", "session": "centrale-my-app-task-2",
    "created": "1790783000", "attached": false,
    "state": "working", "stateSince": 1790783100.0
  }],
  "history": [{
    "project": "my-app", "taskId": "TASK-2", "agent": "codex",
    "state": "working", "timestamp": 1790783100.0
  }],
  "merges": [],
  "messages": [],
  "skippedLines": 0,
  "historyError": null,
  "deliverySkippedLines": 0,
  "deliveryError": null,
  "sessionPreviewMode": "interact",
  "needsYou": [],
  "needsYouErrors": []
}
```

- `projects` includes empty projects; `maxAgents: null` means no configured
  limit. `agentCount` counts fresh live tmux sessions, independent of badges.
- `agents` includes configured-project sessions only. `state` and `stateSince`
  come from this process's hook observations; duplicate hooks do not reset
  the start time. After restart a live session's settled state (`finished`,
  `idle`, `waiting`) and its original `stateSince` are restored from the
  journal when the row belongs to the same tmux session instance; anything
  else is `unknown` and `null` until a hook arrives (task-201). Agent names are configured names, distinct from `agentKind`;
  an unavailable name is `unknown`. New spawn/resume hook URLs include the
  optional `agentName` query parameter so custom names survive restart.
- `history` is oldest first within the requested window, with Unix-second
  timestamps, uppercase task ids and states `spawn`, `working`, `waiting`,
  `idle`, `finished`, `unknown`, `merge blocked`, `merged`, `session ended`.
  Codex turn-end stays `idle`; it is not a claim that the task is complete.
  Hook-state rows may also carry `agentKind` and `created` (the tmux
  session's creation time, when a survey had seen it), which is what a
  restart's restore matches on.
  Spawn records cover resume too. End records come from successful explicit
  end requests or the first successful fleet/sessions survey that notices
  disappearance; their timestamp is observation time, not an inferred exit
  time. Events while the server is stopped cannot be reconstructed.
- `merges` selects the `merged` and `merge blocked` history rows, including
  their `harvest` attempt details (`time`, `project`, `taskId`, `branch`,
  `trigger`, `merged`, and optional `reason`, `error`, `baseBranch` or
  `alreadyMerged`). These are historical outcomes, never current merge gates.
- `needsYou` contains derived inbox items with `kind` (`permission`, `parked`, `idle`,
  `merge`, `message`, `owner`), `project`, `taskId`, `agent`, `since`
  (Unix seconds or null when unknown), and a human-readable `signal`.
  `needsYouErrors` lists unavailable checks; a partial inbox is not an all-clear.
  Master sessions are not worker sessions and are excluded.
  - Permission items require a live waiting hook and a freshly captured dialog.
    They include `lines` and `capturedAt` (Unix seconds). Capture arms the existing
    ten-second input gate. Preview `off` disables these captures; `view` displays
    them without input. `sessionPreviewMode` reports the tier. The view refreshes
    and compares the screen before sending ordered arrows followed by Enter;
    a failed arrow aborts the chain. Items disappear when a fresh capture no
    longer shows the dialog, without changing the agent's hook state.
  - Parked items (task-170.3) are agents whose hook state is `finished`, `idle`
    or `unknown` (never `working`/`waiting`) for at least `parkedAfterSeconds`
    (an `unknown` agent ages from its session start) **and** that either show a
    dialog on the pane or whose latest delivery attempt, made during this
    session, was not `delivered`. They carry the pane's last non-blank line as
    `text`/`lastLine`, and replace the agent's idle item. A finished agent with a
    clean pane and nothing pending is never parked. Each `agents[]` row also
    carries `parked` (`null`, or `{reason, since, lastLine, dialog}`).
  - Idle items require a live idle hook and a readable worker-branch task with
    neither Done status nor a nonempty `finalSummary`. An unreadable report is
    an error, not evidence that the worker needs attention. Finished hooks and
    finished reports do not become review items.
  - Merge items require the latest retained observation (up to 48 hours,
    independently of the requested timeline window) to be `merge blocked`.
    Later activity or merge success supersedes it.
    The signal names the **last attempt's** failing gate; the current verdict is
    explicitly unknown. Open task to rerun the existing gates.
  - Message items select only the latest attempt per project and case-insensitive
    task id, within **two hours**, independent of the requested timeline window.
    A later delivered attempt to that target clears earlier failures regardless
    of text or sender. At most one item is shown per target; invalid timestamps,
    old attempts and unconfigured targets are excluded. Includes message `text`.
  - Owner items select open tasks carrying the exact `needs-owner-approval`
    label in every configured project, from `get_board(config)`'s cached task
    lists, with no separate task-list command on each fleet poll. Labelled tasks and tasks with a spawn branch
    need detail reads: prefer the branch report, including after a worker
    ends, otherwise read the project task through Backlog. Branch reads also
    discover labels added by workers before main carries them; a finished hook
    alone does not suppress an unanswered owner question. The latest comment's
    body is returned as `text`, falling back to the title; `title` is also included.
    A removed label, Done status or nonempty `finalSummary` in that detail
    excludes the item. Unreadable details are reported in `needsYouErrors`.
    This convention is optional; Centrale never writes labels or enforces approval.
    Whoever records the owner decision removes the label. `since` is the board
    task update time (creation time as fallback), explicitly identified as such
    because label-added time is unavailable.
- Needs you is an optional registered view (`static/view-needs-you.js`); removing
  its script tag leaves Board and other views working. Its badge counts items
  (multiple signals for a task can produce multiple items), adds `+` when checks
  are incomplete, and shows `?` on a failed poll. No new external resource is used.
- `messages` contains non-`delivered` delivery attempts for configured
  projects, in log order, with the same shape as `/api/deliveries`. It is
  independent of `window` and preserves raw historical failures; the inbox uses
  the filtered `needsYou` items instead.
  The delivery log has no acknowledgement or retry linkage; a later success
  does not erase an earlier attempt. `dialog` means delivery was held.
- Corrupt history lines are skipped (`skippedLines` counts them since
  startup). Journal failures appear as `historyError`; the process continues
  recording in memory and attempts to persist it on the next append.
  Delivery read failures appear as `deliveryError`, with an empty `messages`
  array, without hiding the rest of the fleet. `deliverySkippedLines` has
  the same meaning as `/api/deliveries`'s `skippedLines`.

The journal lives at `$CENTRALE_FLEET_LOG` if set, else
`$XDG_STATE_HOME/centrale/fleet.jsonl`, else
`~/.local/state/centrale/fleet.jsonl`. Startup reads it back; startup, writes
and snapshots prune observations older than 48 hours. Compaction uses a
temporary file in the journal directory and atomic replacement. Separate
server instances must use separate paths. The journal never restores task
status, current badges or live-session counts.

Each request performs one existing-style tmux session listing, no git or
Backlog calls and no per-agent subprocess work. History is already in memory;
delivery attempts are read through the existing delivery-log reader. A failed
session survey returns **502** instead of declaring sessions ended. The usual
loopback request checks apply (**403**).

## `GET /api/deliveries[?project=<name>&task=<taskId>&limit=N]`

The delivery log: every `POST /api/deliver` attempt that got past
validation, oldest first, the last `limit` matching entries (default 100,
1–1000). `project` and `task` narrow it (the task id matches
case-insensitively); both optional.

```bash
curl -s 'http://127.0.0.1:7420/api/deliveries?project=my-app&task=TASK-2&limit=20'
```

**Response** (shaped from the code):

```json
{
  "log": "/home/user/.local/state/centrale/deliveries.jsonl",
  "deliveries": [
    {
      "id": "5f0c3c2b9d6e4f5aa1d2c3b4e5f60718",
      "time": "2026-09-26T19:04:11Z",
      "project": "my-app",
      "task": "TASK-2",
      "sender": "orchestrator",
      "text": "Ruling: keep the old flag, deprecate it in the next release",
      "session": "centrale-my-app-task-2",
      "outcome": "delivered",
      "echo": "❯ Ruling: keep the old flag, deprecate it in the next release",
      "echoAt": "2026-09-26T19:04:12Z",
      "dialog": null,
      "reason": null
    }
  ],
  "skippedLines": 0
}
```

Each entry: `id`, `time` (when the attempt began, UTC), `project`, `task`,
`sender`, `text`, `session`, `outcome` (see the table above), and `echo`,
`echoAt`, `dialog`, `reason` — each `null` where it does not apply.
`skippedLines` counts lines in the file that were not a JSON object: never
silently dropped.

**The file.** JSON Lines at `$CENTRALE_DELIVERY_LOG` if set, else
`$XDG_STATE_HOME/centrale/deliveries.jsonl`, else
`~/.local/state/centrale/deliveries.jsonl`. State rather than cache,
because it is the record of what agents were told. It is append-only and
never rotated by Centrale; deleting it loses history and nothing else, since
nothing on the board is derived from it. A missing file reads as an empty
log.

**Errors:** 400 for a `limit` outside 1–1000 or a malformed `task`. 500 if
the file exists but cannot be read.

## `POST /api/rule`

Rules on a spawned task (task-172): **one call, whichever case the task is
in**, so a caller never has to know whether an agent is running. While a
task is spawned its task file has one writer — the agent — and the main
checkout's copy is locked (`GET /api/board`'s `taskFileLocked`), so a ruling
reaches the file through the agent rather than around it.

**Body:** `{"project", "taskId", "sender", "text"}` — exactly `POST
/api/deliver`'s body and rules (`taskId` also accepted as `task`; `sender`
required, one line, 1–100 characters; `text` one line). The delivered line,
`[ruling from <sender>] <text>`, must itself fit the 1000-character limit.

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/rule' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "sender": "orchestrator", "text": "Keep the old flag; deprecate it next release"}'
```

**With a live agent** — the ruling is delivered as `[ruling from
orchestrator] Keep the old flag; deprecate it next release` through `POST
/api/deliver`'s own deliver-confirm-and-log path, and the answer is that
delivery's: the same status and body (a delivery-log entry plus `ok`,
`logged`, ...), with `"mode": "delivered"`. A `dialog`, `no-echo` or
`tmux-error` outcome is returned as it is — the agent is live, so nothing
is written around it. The spawn prompt tells the agent to record a message
of that shape on its task as a comment authored by the sender, then act on
it; the delivery log records what was sent even if the agent fails to
write it down.

**With no live agent** (the session ended, the branch is not yet merged —
or the session ended mid-delivery, a `no-session` outcome): nobody to
message and nobody to race with. Under the task's lifecycle lock Centrale
runs `backlog task edit <id> --comment <text> --comment-author <sender>` in
the task's Centrale worktree and commits that one file on the task branch
(`git commit --only -m "backlog: ruling on <id> from <sender>" -- <path>`):

```json
{
  "branch": "task/task-2",
  "worktree": "/home/user/code/my-app/.centrale-worktrees/my-app-task-2",
  "path": "backlog/tasks/task-2 - Fix-the-thing.md",
  "commit": "4b1d0c7e9a2f3e6d5c8b7a6f5e4d3c2b1a0f9e8d",
  "mode": "committed"
}
```

The next Resume starts on a branch whose task file carries the ruling.

**Errors:** 400/404 as `POST /api/deliver` (nothing touched). **409** when
there is nothing to write into: no `task/<id>` branch (the task is not
spawned, so its file is not locked — edit it directly), a parked branch (no
worktree; its main-checkout copy is unlocked), a branch checked out outside
Centrale, or a worktree whose copy of the task file already has uncommitted
edits (a commit labelled as a ruling must carry only the ruling); also 409
when a session started while the ruling was being written — send it again,
and it is delivered. **502** when listing sessions, the `backlog` edit, or
the `git` stage/commit fails.

## `GET /api/harvest?project=<name>`

Evaluates every `task/<id>` branch in a project against the five merge safety
gates, without acting on any of them — read-only, never merges, never
touches the project's own checkout. A parked branch's task read and gates 4/5
use detached scratch worktrees, each cleaned up immediately after evaluation.

**Params:** `project` (query, required).

```bash
curl -s 'http://127.0.0.1:7420/api/harvest?project=my-app' | python3 -m json.tool
```

**Response** (real shape, captured against a running server and genericized —
one branch, blocked at gate 3):

```json
{
  "branches": [
    {
      "taskId": "TASK-1",
      "branch": "task/task-1",
      "harvestable": false,
      "taskTitle": "Fix the login redirect loop on mobile Safari",
      "taskPath": "backlog/tasks/task-1 - Fix the login redirect loop on mobile Safari.md",
      "gates": [
        {"name": "noLiveSession", "passed": true, "reason": null},
        {"name": "taskDone", "passed": true, "reason": null},
        {"name": "worktreeClean", "passed": false, "reason": "worktree has uncommitted changes"},
        {"name": "mergeClean", "passed": null, "reason": "not evaluated (an earlier gate failed)"},
        {"name": "checkCommand", "passed": null, "reason": "not evaluated (an earlier gate failed)"}
      ]
    }
  ],
  "events": []
}
```

`gates` is always all five entries, in fixed order (`noLiveSession`,
`taskDone`, `worktreeClean`, `mergeClean`, `checkCommand`) — evaluation stops
at the first failure, and every gate after it reports `"passed": null,
"reason": "not evaluated (an earlier gate failed)"` rather than being run at
all (there's no point dry-running a merge for a task that isn't even Done). A
branch that's already gone by the time it's evaluated (merged by a click
racing this read, or removed by hand) reports `"alreadyMerged": true` and an
empty `"gates": []` instead of a confusing gate-2 "worktree not found". A
project with no `task/*` branches at all responds `{"branches": [],
"events": [...]}`.

When the Centrale worktree isn't on disk, the report carries
`"branchCheckout"` (the same `{"kind", "path", ...}` shape as
`GET /api/board`'s field) so callers can distinguish the safe parked case from
a checkout that may still hold recoverable work:

- `kind: "external"` — `worktree not found -- task/task-7 is checked out
  outside Centrale at /repo/.worktrees/task-7-integrated (worked externally);
  merge once that worktree is finished and removed`. Never the
  "removed manually?" guess: the branch was adopted elsewhere and work may be
  continuing there, so `taskDone` refuses without reading from that worktree.
- `kind: "none"` — the branch is parked. `taskDone` reads its committed state
  through `backlog task view <id> --json` in a disposable detached snapshot of
  that branch. A non-Done status still refuses with
  `task status is '<status>', not Done`; a Done task with checked acceptance
  criteria proceeds. The next
  `worktreeClean` gate re-enumerates git worktrees and, only if the branch is
  still parked, passes with the explicit reason `no worktree -- nothing
  uncommitted to protect`. Failure to enumerate worktrees refuses rather than
  assuming this case.
- `kind: "centrale"` (git still lists the worktree at Centrale's path but the
  directory is gone, i.e. `rm -rf` without `git worktree remove`) —
  `worktree not found (was it removed manually?) -- git still lists
  task/task-7 at <path>`. This stale registration remains a refusal.

The parked task snapshot is placed under Python's standard temporary
directory (`TMPDIR` overrides it), is detached so it never claims the task
branch, and is removed on success or failure. Backlog resolves the branch's
task file itself, so spaces and git quoting in normal Backlog.md filenames do
not enter the lookup.

When `taskDone` rejects a non-Done branch but the main-checkout task says
`Done`, the branch report also includes
`"doneDivergence": {"branchStatus", "boardStatus", "branchTaskPath",
"mainTaskPath", "mainTaskUncommitted", "lastBranchCommitSubject"}`. This
does not change the gate result: the branch remains authoritative. It gives
the drawer enough context to explain why the visible board and merge gate
disagree and to offer the explicit adopt flow. Without that exact
main-Done/branch-non-Done condition, the field is absent and the original
gate reason is unchanged.

When the first failing gate is `mergeClean` or `checkCommand` — the two
whose outcome depends on what's on the base branch — and the branch is
behind that base, the report additionally carries
`"behindBase": {"baseBranch": "main", "count": 3}` and
`"reconcileHint": "this branch predates 3 newer commits on main; the failure
may be a collision with newer work, not a defect in the branch"`. `count` is
`git rev-list --count <branch>..<base>` (commits on the base not reachable
from the branch, via their merge-base), derived fresh on every evaluation and
never stored. Both keys are absent — not `0`/`null` — for an up-to-date
branch, for a failure at gates 1–3, for a passing branch (where the count
isn't even computed), and when git can't determine it. The drawer keys its
"Resume to reconcile" offer (see `POST /api/resume`) off `behindBase` alone;
the gate results themselves are unchanged by these fields.

`events` is that project's slice of a bounded (50 most recent, in-memory,
process-lifetime) log of harvest *attempts* — clicked or automatic — each
`{"time", "project", "taskId", "branch", "trigger": "click"|"auto", "merged",
"baseBranch"?, "alreadyMerged"?, "reason"?, "error"?}`. It's how the frontend
polls for auto-mode results without a websocket; see "Auto mode" in
[docs/merging.md](merging.md#merging-finished-branches).

**Errors:** 400 for a missing `project` param. 404 for an unknown project.
502 if the project's `path` doesn't exist.

## `GET /api/orchestrator-wait?project=NAME[&after=CURSOR][&timeout=SECONDS]`

Optional, project-scoped long wait for an orchestrating agent. Uses the
existing lifecycle hooks and harvest attempt results; it performs no board
polling, detection, spawning or merging. Nothing calls it unless a client
opts in. See the [Claude loop](agents.md#waiting-for-orchestrator-events-optional).

```bash
curl --fail-with-body --silent --show-error --max-time 70 --get \
  'http://127.0.0.1:7420/api/orchestrator-wait' \
  --data-urlencode 'project=my-app' --data-urlencode 'timeout=60'
```

A 200 response is `text/plain; charset=utf-8`, `Cache-Control: no-store`,
exactly one newline-terminated line. The first space separates an opaque
cursor (no whitespace) from the event. Example responses (cursor ids vary):

```text
73549c8fd3094359aeb1345345bc1bb6:1 TASK-2 finished (ready to review)
73549c8fd3094359aeb1345345bc1bb6:2 TASK-3 waiting for input
73549c8fd3094359aeb1345345bc1bb6:3 TASK-2 merged
73549c8fd3094359aeb1345345bc1bb6:4 TASK-4 merge blocked: checkCommand: tests failed
73549c8fd3094359aeb1345345bc1bb6:5 TASK-5 idle (turn ended, may need input)
```

These illustrate five separate calls, not five lines from one call.
`finished` is a hook observation inviting review, never proof of Done or
permission to merge. Duplicate consecutive hooks for the same public state
produce one notification; a new turn can produce another. Codex turn-end
produces an `idle (turn ended, may need input)` notification after pane
confirmation (one-second checks while its working footer persists), preserving
its ambiguity. It can even arrive during ongoing work; read the current
screen before acting on a Codex idle event. A Codex `waiting` hook only
produces a notification after persisting for 30 seconds and a pane capture
confirming an input dialog. Automatic reviews and approved tools still
running do not publish waiting. A still-unconfirmed candidate is checked
again every 30 seconds until activity/turn-end cancels it, the session ends,
or a dialog is confirmed. Confirmation sets the badge and publishes exactly
one event together. Claude notifications are unchanged.
Harvest emits for actual attempts (click or auto), never an evaluate-only
GET. Blocked attempts name the first failed gate and its reason;
request/merge errors carry their error text. An already-merged attempt
returns `TASK-2 merged (already merged)`. Multiline reasons are flattened
to one line.

An unchanged harvest line is suppressed per project/task, comparing with
the last harvest line published for that task. This includes blocked,
already-merged and error outcomes. A changed line is delivered, including
a return to an earlier outcome after a different one. Hook events and
other tasks do not reset that comparison. Only a real merge (`merged: true`)
always publishes, even when repeated. Every published harvest line becomes
the comparison for the next attempt. This prevents each auto-harvest cycle
waking the master about the same unfinished branch, already-merged branch
or recurring error; the dashboard's attempt log still records every attempt.

Pass the last returned cursor as `after` to get **the next** project event,
including events received while nobody was waiting. Omit `after` on the
first call to start at the oldest event from this server run. Retrying the
same cursor returns the same event, allowing recovery if a response is
lost; clients advance only after handling that event. Independent callers
have independent cursors. This is replayable delivery, not an exactly-once
guarantee for a client's actions: reconcile an interrupted merge/spawn
before retrying it.

`timeout` is finite seconds from 0 through 300 (fractional values allowed),
default 60; 0 reads without waiting. Expiry returns 200 with the unchanged
cursor and the plain message `nothing yet`:

```text
73549c8fd3094359aeb1345345bc1bb6:5 nothing yet
```

Call again with that cursor. Use a client timeout longer than the server's
wait. An abandoned connection holds at most its bounded request wait and
consumes no events. Other HTTP requests continue while a call waits.

History is in memory for the lifetime of the server process, with no size
or age eviction; memory grows with notification count. Server restart
clears it. A cursor from before restart, from another project, or ahead
of the stream returns **409 JSON**, explicitly refusing to hide a gap;
pre-restart cursors are deliberately not made valid across a restart.

Instead, before serving its first request a restarted server publishes one
line per live session in each project (task-201), so a wait **without
`after`** first hears every worker's current state:

```text
<cursor> TASK-2 finished (ready to review) (state before the server restart)
<cursor> TASK-3 state unknown after the server restart (no hook event since; check the session)
```

The state is the one restored from the fleet journal (`finished`, `idle`,
`waiting`) or `unknown`. Events fired during the restart follow as ordinary
lines once their hook's retry lands. So the 409 recovery is: drop the
cursor, wait without `after`, handle each announced session (read a
`state unknown` worker's pane or task), then continue with the cursors as
usual. Reconcile merges or spawns you had in flight through the ordinary
APIs. Notifications are historical observations; act
on freshly read task/session/gate state. No disk log or new external
resource is introduced.

Errors use the ordinary JSON `{"error": ...}` shape: **400** missing
project, malformed cursor or invalid timeout; **404** unknown project;
**409** unavailable cursor; **403** the shared request trust boundary.

## `GET /api/harvest-progress`

What the one in-flight merge is doing *right now*, or `null` when nothing is
being merged. No params, no subprocess, no config walk — a single in-memory
read, because the frontend polls it once a second while its own
`POST /api/harvest` is still outstanding (see "Merging finished branches" in
[docs/merging.md](merging.md#merging-finished-branches)).

```bash
curl -s http://127.0.0.1:7420/api/harvest-progress | python3 -m json.tool
```

**Response** — during a merge:

```json
{
  "progress": {
    "project": "my-app",
    "taskId": "TASK-1",
    "branch": "task/task-1",
    "trigger": "click",
    "gate": "checkCommand"
  }
}
```

and, the rest of the time:

```json
{"progress": null}
```

`gate` is the stage being evaluated at the moment of the read: one of the five
gate names above, then `"mainCheckoutClean"` (the immediate pre-merge re-check
of the project's own checkout, the sixth gate described under
`POST /api/harvest`), then `"merge"` for the real merge and its cleanup. It is
`null` in the brief window after an attempt starts but before its first gate
runs. `trigger` is `"click"` or `"auto"`, exactly as in the events log —
`POST /api/harvest` is the only thing that ever publishes here, so a clicked
merge, a `{"all": true}` pass and an auto-harvest cycle all report through this
one record; a read-only `GET /api/harvest` evaluation publishes nothing at all.

There is only ever one record because harvest.py's merge lock already
serializes every real attempt system-wide. It is live state, never history: it
exists only while an attempt is running and is cleared on every terminal
outcome — merged, gate-blocked, or an exception — so nothing about a finished
merge is readable afterwards (the events log in `GET /api/harvest` is the
durable record). Nothing timing-shaped is published: no start time, elapsed
count, estimate or percentage, deliberately — which gate is running is the
useful fact, how long it has taken is not one Centrale can state usefully.

Polling this during a merge works because the server is a
`ThreadingHTTPServer`: the blocking `POST /api/harvest` holds the merge lock,
not the server's only thread.

**Errors:** none — it takes no input and reads no external state.

## `GET /api/settings`

The current value of every setting `POST /api/settings` can change.

```bash
curl -s 'http://127.0.0.1:7420/api/settings' | python3 -m json.tool
```

**Response** (real shape, captured against a running server and genericized):

```json
{
  "harvestMode": "click",
  "sessionPreviewMode": "interact",
  "refreshIntervalSeconds": 10,
  "checkCommands": {
    "my-app": null,
    "centrale": "python3 -m unittest discover tests",
    "my-lib": "python3 -m unittest discover tests"
  },
  "maxAgents": {"my-app": 4, "centrale": null, "my-lib": null},
  "worktreeLinks": {"my-app": [".venv"], "centrale": [], "my-lib": []},
  "defaultAgent": "claude",
  "agents": ["claude", "claude-haiku", "claude-sonnet", "codex"],
  "requireAgentAssignment": true,
  "lockSpawnedTaskFiles": true,
  "agentEntries": [
    {"name": "claude", "cmd": ["claude"], "cmdText": "claude",
     "promptSuffix": null, "builtin": true, "onPath": true},
    {"name": "codex", "cmd": ["codex"], "cmdText": "codex",
     "promptSuffix": null, "builtin": true, "onPath": true},
    {"name": "claude-sonnet", "cmd": ["claude", "--model", "sonnet"],
     "cmdText": "claude --model sonnet",
     "promptSuffix": null, "builtin": false, "onPath": true}
  ],
  "projects": [
    {"name": "my-app", "path": "/home/user/code/my-app"},
    {"name": "centrale", "path": "/home/user/code/centrale"},
    {"name": "my-lib", "path": "/home/user/code/my-lib"}
  ]
}
```

`checkCommands` maps every configured project name to its `checkCommand`, or
`null` if it has none (no test gate). `maxAgents` maps each project name to
its positive integer cap or `null`; `worktreeLinks` maps each project name
to its array of repo-relative paths (empty when unset).
`agents` is every key in the configured
`agents` map (for a default-agent dropdown), not each entry's full command.
`requireAgentAssignment` is `true` unless `projects.json` sets it to `false` —
see `POST /api/spawn`'s 409 below. `lockSpawnedTaskFiles` follows the same
rule — see `POST /api/spawn`'s task-file lock below.

`agentEntries` is always present and is the same map as a list, in
`projects.json`'s own order, with each entry's full definition — what the
Settings modal's Agents editor renders and posts back. `cmdText` is the
one-line, shell-quoted form of `cmd` (`shlex.join`/`shlex.split` are exact
inverses, so an argument containing spaces or quotes round-trips through the
text field unchanged). `builtin` flags the two names Centrale ships (shown as
quiet, non-deletable, overridable rows). `onPath` is a `PATH` lookup of
`cmd[0]` — an informational "this command isn't installed" hint, never a
reason to refuse anything.

`projects` is name/path only — everything else about a project (`checkCommand`
aside, exposed separately above) is out of scope for this endpoint.

---

## `GET /api/agent-guide`

The concise operating guide for an agent in any project, served as
`text/plain; charset=utf-8` with `Cache-Control: no-store`. Covers spawn and
agent selection, rulings, task-file locks, session/merge gates, badge meanings
and orchestrator waits. Its curl examples use this server's bound loopback
port. Loaded from `static/agent-guide.md` with the Python code at startup,
so a checkout update cannot give an old process a newer contract. Restart
after guide changes, as after Python changes.

```bash
curl --fail --silent --show-error http://127.0.0.1:7420/api/agent-guide
```

No parameters or side effects. 200 returns the guide; 403 is the shared
request trust boundary. Refusals use the normal JSON error format.

## `POST /api/setup-project`

Explicitly set up an already-configured project's Backlog.md and Centrale
instructions. Body: `{"project":"my-app"}`. No settings entry is rewritten.
The add-project checkbox calls this same setup operation.

```bash
curl --fail-with-body --silent --show-error http://127.0.0.1:7420/api/setup-project \
  -H 'Content-Type: application/json' -d '{"project":"my-app"}'
```

Setup runs `backlog init` if `backlog/config.yml` is missing, then installs
or refreshes a pointer to `/api/agent-guide` in both `CLAUDE.md` and `AGENTS.md`,
between `<!-- CENTRALE GUIDELINES START -->` and
`<!-- CENTRALE GUIDELINES END -->`. Replacement preserves every byte outside
the markers. Missing blocks are appended; malformed or duplicate markers
refuse. A correct block is untouched. Backlog initialization may also install
its own instructions when creating a board.

Changed files are staged by exact path and committed using `git commit
--only`; candidates are `CLAUDE.md`, `AGENTS.md`, and (only on init)
`backlog/config.yml`. Unchanged files are excluded. Unrelated staged/unstaged
work is preserved. A dirty target that setup would change refuses first, so
the commit cannot absorb existing edits. Instruction-file symlinks also
refuse. A successful response, with an illustrative SHA:

```json
{"project":"my-app","writtenFiles":["CLAUDE.md","AGENTS.md"],"commit":"0123456789abcdef0123456789abcdef01234567"}
```

When both are already set up, `writtenFiles` is `[]`, `commit` is `null`, and
there is no write or commit. Re-run after a port change to refresh the URL.
`--check` prints this command for each project with missing/stale pointers.
There are no new ports, caches or persistent setup records; the existing
`port` config controls the URL.

Errors: **400** invalid/missing project or missing directory; **404** unknown
project; **409** dirty target files, symlinks or invalid markers; **502** git
inspection, initialization, staging or commit failure; **500** file I/O
failure. Initializer/write/commit failures can leave setup files in the repo;
inspect them before retrying. A commit failure names the exact files to commit
and never reports success. **403/415** are the shared request trust/JSON gates.

## `POST /api/spawn`

Claims a task, cuts (or reuses) a git worktree and branch for it, and starts
a detached tmux session running the resolved coding agent. See "Spawning an
agent" in [docs/agents.md](agents.md#spawning-an-agent) for the full six-step flow this triggers in `spawn.py`.

**Body:** `{"project": "<name>", "taskId": "<id>", "agent": "<name>"}` —
`agent` is optional (task-171).

```bash
curl -s -X POST http://127.0.0.1:7420/api/spawn \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "agent": "codex"}'
```

**Which agent runs.** Centrale does not guess (task-171):

1. `agent`, when given, names the agent — matched case-insensitively
   against `projects.json`'s `agents` map, a leading `@` allowed. It wins
   over the task's assignee: an explicit choice is not a guess. A name that
   matches no configured agent (or a non-string) is a **400**
   (`unknown agent: 'gpt' -- configured agents are claude, codex`), before
   any side effect.
2. Otherwise the task's **first** assignee, when it names a configured agent.
3. Otherwise — no assignee, an assignee that is a person or a typo, or a
   task whose assignee could not be read — the spawn is **refused with
   409** while `requireAgentAssignment` is on (the default), before anything
   is claimed, committed or created. The message is written for a program
   as much as a person: it names the task, what its assignee was (or that it
   had none, or could not be read), and the configured agents, e.g.

   ```text
   task TASK-2 is assigned to @dana, which is not a configured agent, and
   requireAgentAssignment is on, so Centrale will not guess which agent to
   launch. Configured agents: claude, codex. Retry with "agent" set to one
   of them, or assign the task to one first (backlog task edit TASK-2 -a
   @<agent>). Nothing was claimed or created.
   ```

   A caller retries the same request with `"agent"` set.
4. With `requireAgentAssignment` off, step 3 instead launches
   `defaultAgent` and says so in `warnings`
   (`task TASK-2 has no assignee; launched the default agent 'claude'
   instead (requireAgentAssignment is off)`).

**Recording the choice.** When the launched agent did not come from the
task's first assignee (steps 1 and 4 on a task whose assignee names no
agent), the claim below also sets the assignees — the agent **first**, then
every assignee the task already had. A person's name is never dropped; it
moves behind the agent because the first assignee is what resolves an agent,
so the next Re-spawn or Resume launches the same one. A task whose first
assignee already names an agent is left alone even when `agent` overrides it
for this launch. When the task could not be read at all, nothing is
recorded (the edit replaces the list, and the list is unknown) and a
`warnings` line says so.

**Response shape** (derived from `spawn.spawn`; not fired against the live
server for this doc — it starts a real tmux session and creates a real
worktree/branch):

```json
{
  "session": "centrale-my-app-task-2",
  "attach": "tmux attach -t centrale-my-app-task-2",
  "agent": "codex",
  "warnings": ["failed to claim TASK-2 before spawn: ..."]
}
```

`warnings` is present when the claim/commit or task-file lock step hit a
problem, the default agent was fallen back to, or a configured worktree link
was skipped. These warnings do not block the spawn.

**Behavioral notes:**

- **Per-project cap.** `maxAgents` limits live Centrale tmux sessions for
  this project, counted afresh on every spawn and resume. At the cap the
  response is 409 naming the sessions, before any claim or worktree side
  effect. Count-through-launch is serialized per project in this server
  process. Omitted or `null` means no cap; an idle or finished session still
  counts until it ends.
- **Worktree links.** Each newly created worktree receives the project's
  `worktreeLinks` as symlinks to paths in the main checkout. Root-anchored
  entries in the shared `.git/info/exclude` keep them out of ordinary
  staging; git's effective ignore rule is checked before linking. Missing
  sources, tracked/existing destinations, symlinked destination parents or
  failed exclusions warn and skip the link. Reused worktrees stay unchanged.
  Omitted or empty settings add no links. Destroying a worktree removes its
  links and preserves the source paths. See [Configuration](configuration.md#setup--configuration)
  for path validation rules.
- **Claim-commit side effect, before the worktree exists.** Step 2 of a spawn
  runs `backlog task edit <id> -s "In Progress"` (plus `-a @<agent> -a
  <existing>...` when the choice is being recorded — see above; otherwise
  the assignee is untouched) *directly against the project's main checkout*, then `git add
  backlog && git commit -m "backlog: claim <id> for spawn"` on whatever branch
  the repo's `HEAD` currently points at — all of this **before** the worktree
  is cut in step 3, and even when `CENTRALE_SPAWN_CMD` is set (only the
  launched command is overridden by that variable, never this step). This is
  why the worktree's branch point already contains the claim: cutting the
  branch second means it never disagrees with main about who owns the task.
  Both the claim and the commit are best-effort — a failure at either step
  becomes a `warnings` string in the response instead of blocking the spawn.
- **Task-file lock, right after the claim commit** (task-172). With
  `lockSpawnedTaskFiles` on (the default), the main checkout's copy of the
  task file is then made read-only (`chmod a-w`), so the agent on its
  branch is its only writer: `backlog task edit` on that copy fails with
  `EACCES` and changes nothing, and `git status` shows nothing. A respawn
  unlocks it for its own claim and locks it again. A lock that could not
  be taken is a `warnings` line (`did not lock TASK-2's task file: ...`),
  never a refusal. The lock is released by the merge, by `POST
  /api/discard-attempt`, `/api/abandon-worktree` and `/api/cleanup-branch`,
  and — for one with no task branch behind it — at startup and by
  `--check`; `POST /api/resume` and `/api/end-session` leave it. With the
  setting off, nothing is locked. See [Ruling on a spawned
  task](agents.md#ruling-on-a-spawned-task), and
  [`POST /api/rule`](#post-apirule) for what to do instead of editing.
- A task whose status is already `Done` is refused outright (409), before any
  side effect — including the claim commit above.
- A duplicate spawn (a `centrale-<project>-<taskid>` session already live) is
  refused with 409, also before any side effect.
- A task whose `task/<id>` branch is checked out in a worktree Centrale
  doesn't manage (`GET /api/board`'s `branchCheckout.kind == "external"`,
  task-70) is refused with 409 naming that checkout — `task/task-7 is
  checked out outside Centrale at /repo/.worktrees/task-7-integrated -- git
  refuses a second checkout of the same branch; finish or remove that
  worktree first` — also before any side effect. Without this, the claim
  would already be committed on the base branch by the time `git worktree
  add` failed with its raw "is already checked out at" error.
- `agent` reflects the agent actually launched (see "Which agent runs"
  above) — `"custom"` when `CENTRALE_SPAWN_CMD` overrode the launched
  command entirely, in which case no agent is chosen, refused or recorded.

**Errors:** 503 if tmux isn't on `PATH`. 409 for a project at `maxAgents`, an already-live session, a
Done task, a branch checked out outside Centrale, or (with
`requireAgentAssignment` on) a task with no `agent` given and no assignee
naming a configured agent. 400/404 for a missing/unknown project, an invalid
task ID, or an unknown `agent`. 500
for a `git worktree add` or `tmux new-session` failure. 502 if checking for
an existing session (`tmux list-sessions`) fails unexpectedly.

## `POST /api/resume`

Same validation, session naming, project cap and 409-on-duplicate as `/api/spawn`, but
never claims/commits the task or creates a new branch — it only reuses the
worktree that's already there, starting a fresh tmux session running the
resolved agent's *resume* command (its configured `resumeCmd`, else `claude
--continue` / `codex resume <UUID>` for a claude-/codex-family agent, followed
by the agent's own `cmd` arguments such as `--model`, else a
fresh prompt noting prior work already exists). See "Resuming an interrupted agent" in
[docs/agents.md](agents.md#resuming-an-interrupted-agent).

**Body:** identical shape to `/api/spawn` — `{"project": "<name>", "taskId": "<id>"}`.

```bash
curl -s -X POST http://127.0.0.1:7420/api/resume \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2"}'
```

**Response shape** (derived from `spawn.resume`):

```json
{
  "session": "centrale-my-app-task-2",
  "attach": "tmux attach -t centrale-my-app-task-2",
  "agent": "codex",
  "resumed": true,
  "conversationId": "11111111-1111-4111-8111-111111111111",
  "conversationStatus": "resumed"
}
```

For Codex, the response additionally carries `conversationId` (the selected
UUID, or `null` for a fresh start) and `conversationStatus`:

| `conversationStatus` | `resumed` | Meaning |
|---|---|---|
| `resumed` | `true` | A verified worktree conversation was selected by UUID and its normal composer appeared. |
| `fresh` | `false` | No eligible conversation existed; a fresh Codex composer appeared with the prior-work fallback prompt. |
| `dialog` | `false` | Startup is blocked by a dialog or picker; inspect the named pane. |
| `unconfirmed` | `false` | Startup did not establish a normal composer within five seconds, or capture failed. |

`conversationId` identifies the **selected** conversation; it is not proof of
successful startup when `resumed` is false. For a fresh start, this endpoint
does not predict the new UUID. A dialog/unconfirmed result includes a warning
and leaves the pane available for inspection. Callers must check these fields
before sending a ruling; delivery also performs its own fresh dialog check,
including the transitional “Resuming session…” screen that already shows a
composer before the selected conversation has loaded.
These Codex fields are absent for other agent families, whose existing
`resumed` semantics remain unchanged.

Selection reads only eligible interactive records for the exact worktree under
`$CODEX_HOME/sessions` (default `~/.codex/sessions`), with no persistent Centrale
mapping. Custom Codex `resumeCmd` and session/directory overrides refuse with
409; remove `resumeCmd` and keep ordinary model/config options on `cmd` to use
the safe built-in resume. See [the agent guide](agents.md).

When resume recreates a missing worktree, its response may also contain
`warnings` for skipped `worktreeLinks`, as on spawn.

**Errors:** same as `/api/spawn`. A Done task is refused with 409 here too,
before the worktree is touched — a Done task's worktree may still exist, and
resuming an agent onto already-finished work is exactly as wrong as spawning
fresh onto it. A branch checked out outside Centrale gets the same 409 and
reason as `/api/spawn` (there is no Centrale worktree to resume into, and git
would refuse the second checkout anyway).

### Reconcile variant: `{"reconcile": true}`

`{"project": "<name>", "taskId": "<id>", "reconcile": true}` (a literal JSON
`true`; anything else means an ordinary resume) is the drawer's **"Resume to
reconcile"** — offered when a `POST /api/harvest` report came back blocked
at `mergeClean`/`checkCommand` with `behindBase` set (see `GET /api/harvest`).
It is the same resume path, not a separate engine: same validation, same
session name and 409 on a live session, same Done refusal, same worktree
reuse (never a new branch, never a re-claim), same agent resolution, same
hook/`CENTRALE_EVENT_URL` injection, and the same resume-command tiers. The
one difference is the trailing argument: the **reconcile prompt** (plus the
agent's `promptSuffix`) is appended at every tier — `resumeCmd` plus the
prompt, `claude --continue <prompt>` or `codex resume <UUID> <prompt>`, or
the agent's own `cmd` with the prompt plus the fresh-start prior-work note
when there is nothing to resume — so the conversation that built the branch
is continued and handed the new job, rather than replaced by an agent that
must re-learn the branch. When a conversation is continued the prompt adds
one sentence saying so (you built this branch; the base has moved on; read
the repository, not your memory of it). That
prompt tells the agent to orient first — read the task's description, plan,
and implementation notes via `backlog task view`, and review the branch's own
log and diff against the base — then merge the repo's current base branch
into the task branch (`git merge <base>`, never rebase), resolve every conflict textual and
semantic, run the project's configured `checkCommand` (spelled out verbatim,
or "see the project's AGENTS.md/README" when none is configured) until it
passes, commit on the branch, and never merge into the base.

Centrale itself performs **no merge or rebase** on this call — the only git
it runs is the worktree lookup a plain resume runs. The ordinary gates
re-verify the branch afterward like any other.

```bash
curl -s -X POST http://127.0.0.1:7420/api/resume \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2", "reconcile": true}'
```

**Response:** the ordinary resume shape plus `"reconcile": true`. **Errors:**
identical to an ordinary resume.

## `POST /api/harvest`

Runs the five safety gates for one branch (or every ready branch in a
project) and merges whichever pass. See "Merging finished branches" in
[docs/merging.md](merging.md#merging-finished-branches) for the full gate list and merge mechanics. A blocked one-branch
response is the `GET /api/harvest` report for that branch (including
`behindBase`/`reconcileHint` when applicable) plus `"merged": false`.

**Body, one branch:** `{"project": "<name>", "taskId": "<id>"}`.
**Body, all ready branches in a project:** `{"project": "<name>", "all": true}`.
After an ordinary one-branch response exposes the corresponding affordance,
the drawer may send one of two explicit action forms:
`{"project": "<name>", "taskId": "<id>", "adoptDone": true}` or
`{"project": "<name>", "taskId": "<id>", "discardMainTaskEdit": true}`.
Action flags must be booleans, are mutually exclusive, and cannot accompany
`all: true`.

```bash
curl -s -X POST http://127.0.0.1:7420/api/harvest \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2"}'

curl -s -X POST http://127.0.0.1:7420/api/harvest \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "all": true}'
```

**Response shape, one branch** (derived from `harvest.harvest_branch` — the
same report shape `GET /api/harvest` returns, plus `merged` and, on success,
more):

```json
{
  "taskId": "TASK-2",
  "branch": "task/task-2",
  "harvestable": true,
  "taskTitle": "Run full-horizon flash ADC global-parameter Monte Carlo campaign",
  "gates": [
    {"name": "noLiveSession", "passed": true, "reason": null},
    {"name": "taskDone", "passed": true, "reason": null},
    {"name": "worktreeClean", "passed": true, "reason": null},
    {"name": "mergeClean", "passed": true, "reason": null},
    {"name": "checkCommand", "passed": true, "reason": null},
    {"name": "mainCheckoutClean", "passed": true, "reason": null}
  ],
  "merged": true,
  "baseBranch": "main",
  "unrelatedDirtyCount": 2
}
```

A gate-blocked attempt responds the same way but with `"merged": false` and
the same `gates`/`harvestable: false` shape `GET /api/harvest` would show for
that branch (200, not an error — a blocked merge is a normal outcome, not a
request failure). A branch already gone by the time this runs responds
`{"taskId", "branch", "merged": false, "alreadyMerged": true, "gates": [],
"taskTitle": null}`.

The action forms are intentionally stateless and re-verified under the same
harvest lock:

- `adoptDone: true` is accepted only while main says Done and the agent
  branch does not. The agent worktree must be clean. Centrale runs
  `backlog task edit <id> -s Done` in that worktree, requires the task file
  to be the only resulting change, stages and commits only that exact path,
  then re-runs every gate and merges only if all are green. The response
  includes `"adoptedDone": true`; it can still be a normal blocked response,
  for example when an acceptance criterion remains unchecked.
- When `mainCheckoutClean` is blocked only by an unstaged tracked edit to
  the same task file, the report includes
  `"discardableMainTaskEdit": {"path": "<exact repo-relative path>"}`.
  `discardMainTaskEdit: true` re-evaluates that condition, restores only
  that named worktree path, re-runs every gate, and merges only if green.
  A completed discard is reported as `"discardedPaths": ["<path>"]`.
  Staged, untracked, renamed, or additional blocking paths never receive
  this affordance and remain ordinary `mainCheckoutClean` refusals.

**Response shape, `all: true`** (derived from `harvest.harvest_all_ready`):

```json
{"merged": [ /* one-branch reports, as above, each "merged": true */ ],
 "notReady": [ /* one-branch reports for everything still blocked */ ]}
```

Each branch in the `all` form is re-evaluated fresh immediately before it's
merged (via the same one-branch path above), since merging one branch moves
the project's base branch forward and can change whether a *later* branch's
merge is still clean.

**Behavioral notes:**

- **A sixth gate you won't see on `GET /api/harvest`.** The five gates above
  are what `GET`/the board show as "the gates" — but immediately before the
  real merge, `POST` re-checks whether the project's own checkout has picked
  up anything that would actually conflict with this merge since gates 4/5
  ran (they only ever look at an isolated scratch worktree, never the real
  checkout). That extra check appears as a sixth gate entry named
  `"mainCheckoutClean"`, only on a `POST` response, only once every original
  five have already passed. It tolerates **unrelated** uncommitted dirt
  (untracked or unstaged-modified files at paths the merge itself doesn't
  touch) — only a **staged** change (anywhere) or an unstaged/untracked
  change that overlaps with what the merge would touch actually blocks it,
  matching git's own real merge tolerance. When the merge proceeds with
  unrelated dirt still present, `"unrelatedDirtyCount"` on a successful
  response says how many files were left untouched (omitted entirely, not
  `0`, when there were none).
- `"warnings"` appears on a successful-merge response if cleaning up the
  worktree or deleting the branch failed afterward — the merge itself is
  never undone for that; it's a non-fatal cleanup problem, not a failed
  merge. A parked merge skips removal of the nonexistent Centrale worktree
  and still deletes the branch with `git branch -d`; Centrale never uses
  `-D`, so git leaves a branch intact if it cannot prove the branch is merged.
- Every attempt through this endpoint — successful, gate-blocked, or an
  unexpected merge failure — appends one entry to the same bounded in-memory
  events log `GET /api/harvest` exposes, tagged `"trigger": "click"`
  (`"auto"` is only ever written by the background auto-harvest thread, never
  a click).
- A single process-wide lock serializes every real harvest attempt (click or
  auto), so two can never interleave their git operations against the same
  or different projects.

**Errors:** 400/404 for a missing/unknown project, invalid task ID, or invalid
action flags. An action whose authoritative precondition changed, or an
adopt attempt against a dirty agent worktree, returns 409 without mutation.
500 covers an action subprocess failure or unexpected merge failure; any
edit/staging left by a failed adopt is reported as recoverable. An ordinary
gate-blocked result is *not* an error.

## `POST /api/agent-event?project=<name>&task=<taskId>`

The endpoint every spawned/resumed session's `CENTRALE_EVENT_URL` environment
variable points at — see "Agent lifecycle events" below for the full
custom-agent contract this backs.

**Params:** `project`, `task` (query, both required), plus optional
`agentKind=claude|codex`. Centrale includes the validated kind in built-in
spawn/resume URLs; custom and legacy reporters may omit it. This is the one
endpoint that still takes its identity from the query string, because
`CENTRALE_EVENT_URL` is the only thing a notify hook receives — see
"Request requirements".

**Body:** `{"state": "working" | "waiting" | "finished"}`, required, plus
optional `"firedAt"`: when the hook fired, in epoch seconds (task-201).
`centrale_notify.py` sends it, and retries in the background for a few
seconds when the server is unreachable; an event whose `firedAt` is older
than the last applied one for the task (or than the spawn/resume that
replaced its session) is accepted with `{"ok": true}` but ignored, so a
retried event cannot overwrite a later state. (There
was once a `?state=` query fallback for a reporter that could not send a
body; it is gone, because it was also the one way a bodyless cross-origin
form POST could drive this endpoint.)

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-2' \
  -H 'Content-Type: application/json' \
  -d '{"state": "working"}'
```

**Response:** `{"ok": true}`.

**Errors:** 400 for a missing `project`, a missing/invalid `task`, a
missing/invalid `state` (must be exactly one of `working`/`waiting`/
`finished`), an invalid `agentKind`, or a `firedAt` that is not a finite
number. 404 for an unknown project.

## `POST /api/end-session`

Kills the live tmux session for one project+task.

**Body:** `{"project", "taskId"}` — the same shape as `/api/spawn`.
`taskId` is also accepted as `task`. A query string is ignored entirely
(see "Request requirements").

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/end-session' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2"}'
```

**Response:** `{"ok": true, "session": "centrale-my-app-task-2", "killedOrphans": []}`.

**Behavioral notes:** the exact session name is computed the same way
spawn/resume compute it (`spawn.session_name`), confirmed live via
`list_sessions()` first (a 404 if it isn't, never assumed), then killed with
`tmux kill-session -t =<name>` — the leading `=` forces **exact-name**
matching. Without it, `tmux -t` prefix-matches by default, which could kill
an unrelated session whose name happens to start with this one's (e.g.
`centrale-my-app-task-1` vs. `centrale-my-app-task-10`).

Killing the tmux session does not reliably kill everything it started: a
vite dev server or a vitest worker pool the agent launched survives its
parent pane and keeps running, cwd'd in the worktree, for as long as the
machine stays up. So once the session itself is confirmed killed, this also
finds every remaining pid whose cwd is under this task's worktree and kills
it (SIGTERM, best-effort) — matched by resolved cwd, never by scanning
command lines. `killedOrphans` lists the pids it acted on (empty when there
were none). Nothing outside that worktree directory is touched.

There is **no agent-state gate**: a session reporting `working` is killed
just like one reporting `finished`, and a session with no lifecycle event
at all is not a special case. The one place a state is consulted is the UI,
which asks for a second click on its End session button for a `working`
agent (the first click arms it, and the label says why) — see "Ending a
session" in [docs/agents.md](agents.md#ending-a-session).

**Errors:** 400 for a missing `project`. 404 for an unknown project, or no
live session for that project+task. 500 if `tmux kill-session` itself fails.
502 if checking for the session (`tmux list-sessions`) fails unexpectedly.

## `POST /api/cleanup-branch`

Removes a fully-merged `task/<id>` branch's worktree and deletes the branch
— for a branch merged out-of-band (by hand, or by an agent that ignored its
prompt's "don't merge your own branch" instruction), which Centrale's own
`GET /api/board` would otherwise keep showing as `alreadyMerged: true` with
nothing left to actually clean it up.

**Body:** `{"project", "taskId"}` — the same shape as `/api/end-session`, plus
an optional `"force": true` (see below).

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/cleanup-branch' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-2"}'
```

**Response:**

```json
{
  "ok": true,
  "branch": "task/task-2",
  "worktreeRemoved": true,
  "discardedPaths": ["some/untracked-file.txt"],
  "killedOrphans": [],
  "taskFileUnlocked": "backlog/tasks/task-2 - Fix-the-thing.md"
}
```

`taskFileUnlocked` (task-172) is the main checkout's task file this call
made writable again — nothing is spawned on the task any more — or `null`
when it was not locked.

`discardedPaths` lists every uncommitted/untracked path found in the
worktree (dequoted — see `server.dequote_git_path` — so a spaced Backlog.md
filename shows up as itself, not wrapped in stray quotes) **before** it's
force-removed, so the response names exactly what a forced removal is about
to discard. `worktreeRemoved` is `false` if there was no worktree left to
remove at all (only the branch still existed) — that's not an error, just
nothing to do on that side.

**Behavioral notes:**

- **Never trusts the client's board-derived `alreadyMerged` flag.** This
  endpoint re-verifies "actually fully merged" itself, server-side, via the
  exact same combined check (`_branch_already_merged`: branch tip is an
  ancestor of the base branch **and** the main-side task status is Done) that
  `GET /api/board`'s own `alreadyMerged` field uses — so the two can never
  disagree. Ancestry alone isn't enough: a freshly spawned, still-in-progress
  branch's only commit is the spawn's own claim commit, which already exists
  on main too — trivially an ancestor the instant it's created — so the Done
  check is what rules that case out.
- Refuses with 409 if a live tmux session is still running for this task, or
  if a branch exists but genuinely isn't fully merged yet.
- **Refuses with 409 if the branch is checked out in a worktree Centrale
  doesn't manage** (task-80) — `GET /api/board`'s `branchCheckout.kind ==
  "external"`: a branch merged out-of-band whose foreign checkout is still
  around. git won't delete a checked-out branch, so this is classified up
  front with the same `git worktree list --porcelain` helper the board uses
  (`spawn.checkout_state`), never by parsing `git branch -d`'s refusal, and
  the error is the exact sentence `/api/spawn` and `/api/resume` use for the
  same state: `task/task-2 is checked out outside Centrale at <path> -- git
  refuses a second checkout of the same branch; finish or remove that
  worktree first`. This check runs after the live-session check and
  **before** the not-fully-merged check and any side effect — nothing is
  removed or deleted first. The `centrale` and `none` (parked) kinds are
  unaffected. The frontend mirrors it: the card and drawer render a disabled
  "Worked externally" button with that reason as tooltip instead of
  "Merged — clean up" for such a task.
- **Refuses with 409, naming the pids, if a live process still has its cwd
  under the worktree** (task-201) — removing the directory does not stop a
  vite dev server or vitest worker pool the agent left running there; it
  survives the removal outright and leaks for as long as the machine is up.
  Checked (via the same cwd-under-directory scan `/api/end-session` uses)
  once the worktree is confirmed to still exist, before anything is touched.
  Pass `"force": true` in the body to kill those processes here and proceed
  with the removal anyway; either way, `killedOrphans` in the response lists
  the pids that were actually killed (empty when there was nothing to kill).
- Removal order: `git worktree remove --force` first (if a worktree exists),
  then `git branch -d` (never `-D` — ancestry was just re-verified server-side,
  so a safe delete should always succeed; a failure surfaces as a 500 with
  git's own stderr rather than being silently forced through).
- Neither a worktree nor the branch existing at all is a 404 ("nothing here
  to clean up"), not a 409 or 500.
- Never touches the project's main checkout beyond the branch delete itself.

**Errors:** 400 for a missing `project`. 404 for an unknown project, or
neither a worktree nor a branch found for that project+task. 409 for a live
session, a branch checked out outside Centrale (the external-checkout sentence
above), or a branch that isn't fully merged. 500 for a `git status`,
`git worktree remove`, or `git branch -d` failure. 502 if the project's
`path` doesn't exist.

## `GET /api/discard-preview?project=<name>&task=<taskId>`

What the two throwaway routes below would destroy, without destroying any of
it. Read-only: no tag, no removal, no branch delete, and no `backlog` call at
all.

The UI fetches this on the FIRST (arming) click of either action and puts the
numbers straight into the confirming button's own label, so the click that
actually destroys something names it — "Discard 3 commits and 4 uncommitted
files?" rather than a generic "are you sure".

```bash
curl -s 'http://127.0.0.1:7420/api/discard-preview?project=my-app&task=TASK-9'
```

**Response:**

```json
{
  "branch": "task/task-9",
  "branchExists": true,
  "branchTip": "1fb0261627d96454a5d478560631e4bbdc9bd015",
  "baseBranch": "main",
  "commitCount": 3,
  "worktreePath": "/home/user/code/my-app/.centrale-worktrees/my-app-task-9",
  "worktreeExists": true,
  "dirtyPaths": ["src/half-done.py", "notes.md"],
  "dirtyFileCount": 2,
  "liveSession": null,
  "externalCheckout": null,
  "recoveryCommand": "git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015"
}
```

**Behavioral notes:**

- `commitCount` is `git rev-list --count <base>..<branch>` — what the branch
  carries that the base does not, so a spawn branch whose only commit is
  Centrale's own claim commit reads as `0` rather than `1`.
- `dirtyFileCount` counts the same dequoted paths `/api/cleanup-branch`
  reports: everything uncommitted *and* untracked, which is exactly what a
  forced worktree removal discards. A missing worktree is `0`, not `null` —
  that is a measured zero.
- **A number that could not be measured is `null`, never `0`.** A failed
  `git status` or `rev-list` leaves `dirtyFileCount`/`commitCount` (and
  `dirtyPaths`) null; the UI then refuses to arm its confirm at all rather
  than understating what is about to be destroyed.
- `liveSession` and `externalCheckout` report the two refusals the POSTs
  below would make, as fields rather than as an error status — the UI needs
  to say *why* an action is unavailable, and a bare 409 cannot carry both
  facts at once.
- `branchTip` and `dirtyPaths` are also the two values the destructive POSTs
  below **require** back as `expectedBranchTip` and `expectedDirtyPaths`: a
  confirm acts on the state it described or on nothing at all. Fetch this
  first, then send those two fields with the POST.
- Read-only, and deliberately not serialized against the lifecycle lock the
  POSTs take: a preview that blocked behind a running merge would be a worse
  answer than a fresh unlocked one, and the POST re-measures under the lock
  anyway.
- Measured at request time on purpose, rather than added to `GET /api/board`:
  a board field would be up to a refresh interval stale, and this is the
  sentence a user reads immediately before an irreversible click. It also
  costs nothing until someone reaches for the action, instead of adding a git
  call per branch-bearing task to every board read.

**Errors:** 400 for a missing `project` or an invalid/missing `task`. 404 for
an unknown project, or neither a worktree nor a branch for that project+task.
502 if the project's `path` doesn't exist or `tmux` couldn't be asked.

## `POST /api/discard-attempt`

Throws a bad attempt away: removes the task's worktree **and deletes its
branch**, so the task is left with no branch at all, the ordinary Spawn button
comes back, and the next spawn branches fresh from the base.

This is the answer to "this attempt is bad, throw it away and let me start
over". `/api/end-session` only kills the agent; `/api/cleanup-branch` only
handles branches that are already merged and deletes with the safe
`git branch -d`, which refuses unmerged commits by design — exactly the case
this route exists for.

**Body:** `{"project", "taskId", "expectedBranchTip", "expectedDirtyPaths"}` —
the identity `/api/end-session` uses, plus the state the confirm described,
taken straight from `GET /api/discard-preview`'s `branchTip` and
`dirtyPaths`. Both expectation fields are **required**: a POST that names no
state has reviewed none. `expectedBranchTip` is `null` for a task the preview
found no branch for. These two routes accept a 1 MiB body rather than the
usual 64 KiB, because the expectation is as long as the preview's own path
list — a worktree with thousands of untracked files must not be the one thing
that cannot be thrown away.

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/discard-attempt' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-9",
       "expectedBranchTip": "1fb0261627d96454a5d478560631e4bbdc9bd015",
       "expectedDirtyPaths": ["src/half-done.py", "notes.md"]}'
```

**Response:**

```json
{
  "ok": true,
  "branch": "task/task-9",
  "branchDeleted": true,
  "branchTip": "1fb0261627d96454a5d478560631e4bbdc9bd015",
  "baseBranch": "main",
  "commitCount": 3,
  "worktreeRemoved": true,
  "discardedPaths": ["src/half-done.py", "notes.md"],
  "recoveryTag": "abandoned/task-9-20260904-163012",
  "recoveryCommand": "git branch task/task-9 1fb0261627d96454a5d478560631e4bbdc9bd015",
  "taskFileUnlocked": "backlog/tasks/task-9 - Rework-the-parser.md"
}
```

`taskFileUnlocked` (task-172): the main checkout's task file this discard
made writable again, or `null` when it was not locked.

**Behavioral notes:**

- **Why the branch has to go, not just the worktree.** `spawn._ensure_worktree`
  REUSES an existing `task/<id>` branch (`git worktree add <dir> <branch>`) and
  only creates one from the base when there is none. Parking the branch would
  make every subsequent Re-spawn start from the same bad commits; a genuine
  redo needs the branch gone.
- **The confirm is bound to the state it described.** The handler re-measures
  the branch tip and the uncommitted paths and refuses with **409** if either
  has moved since the preview, naming what changed — before any tag, removal
  or delete. Nothing is destroyed by that refusal; the way on is a fresh
  preview and a confirm that names the new numbers. This is what keeps a
  commit that landed while the confirm sat armed from being destroyed without
  ever being named.
- **Never the safe `git branch -d`.** The whole point is that the commits are
  unmerged, so the safe delete would refuse exactly the case this route is
  for. That makes this the only irreversible act in Centrale. The delete is
  `git update-ref -d refs/heads/task/<id> <tip>` — a compare-and-delete
  against the tip that was just tagged, so the recovery anchor can never name
  a different commit from the one that was destroyed; a tip moved by a
  non-Centrale git process fails here with the branch left intact.
- **A recovery tag makes it recoverable anyway, and it is not optional.**
  Before anything is removed, the branch tip is tagged
  `abandoned/task-<id>-<YYYYMMDD-HHMMSS UTC>`. This is measured, not
  decorative: after `git worktree remove --force` followed by `git branch -D`,
  no reflog anywhere still references the tip — the worktree's reflog
  (`.git/worktrees/<n>/logs/HEAD`) goes with the worktree and the branch's
  (`.git/logs/refs/heads/task/<id>`) goes with the branch — so the commits are
  unreachable immediately and a single `git gc --prune=now` destroys them.
  Without the tag, `recoveryCommand` would be a promise git does not keep.
  **A failure to create the tag aborts the whole request with nothing
  removed**: no anchor, no delete.
  The tags share one prefix so they group in `git tag` and sweep easily —
  `git tag -d abandoned/task-9-20260904-163012` once you are sure.
- Order of operations, all of it under this task's lifecycle lock: refusals →
  survey (tip, commit count, uncommitted paths) → the stale-state refusal →
  the refusals again → tag → `git worktree remove --force` →
  `git update-ref -d`. The survey happens before anything is touched, so the
  response can name what it destroyed even though it is gone. The live-session
  and foreign-checkout refusals run **twice** on purpose — once up front and
  once immediately before the tag — so one that appears in between still costs
  a refusal rather than an agent's worktree.
- **One lifecycle operation at a time per task.** `/api/spawn`, `/api/resume`,
  `/api/cleanup-branch`, `/api/harvest` and both routes here take one lock per
  (project, task), so two of them can never interleave their git operations on
  the same branch. Operations on *different* tasks are unaffected — the lock is
  per task, not global.
- **The backlog task's status is never read and never written.** "This attempt
  was bad" is not "this task is back to To Do" — the board is the source of
  truth for where a task stands, and that call is the user's.
- Refuses with 409 while a live tmux session is running for this task (End
  session first), and with the same 409 sentence `/api/spawn`, `/api/resume`
  and `/api/cleanup-branch` use when the branch is checked out in a worktree
  Centrale doesn't manage. Both checks run **before any side effect**.
- Neither a worktree nor the branch existing at all is a 404 — nothing here to
  discard. A worktree with no branch, or a parked branch with no worktree,
  each does the half that applies.
- `commitCount` may be `null` when git could not measure it; that does not
  block the discard (the user has already confirmed) but the response never
  claims a number it does not have.
- Never touches the project's main checkout beyond the branch delete and the
  tag.

**Errors:** 400 for a missing `project`, an invalid `taskId`, a malformed
body, or a missing/malformed `expectedBranchTip`/`expectedDirtyPaths`. 404 for
an unknown project, or neither a worktree nor a branch. 409 for a live
session, a branch checked out outside Centrale, or a repository that moved
since the preview. 500 for a failed `git status`, tag, `worktree remove` or
branch delete. 502 if the project's `path` doesn't exist or `tmux` couldn't be
asked.

## `POST /api/abandon-worktree`

The milder half: removes the task's worktree and leaves the branch exactly
where it is. Not merging now, but not throwing the work away either.

**Body:** `{"project", "taskId", "expectedBranchTip", "expectedDirtyPaths"}` —
the same shape, and the same requirement, as `/api/discard-attempt` above.

```bash
curl -s -X POST 'http://127.0.0.1:7420/api/abandon-worktree' \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app", "taskId": "TASK-9",
       "expectedBranchTip": "1fb0261627d96454a5d478560631e4bbdc9bd015",
       "expectedDirtyPaths": ["src/half-done.py", "notes.md"]}'
```

**Response:**

```json
{
  "ok": true,
  "branch": "task/task-9",
  "branchKept": true,
  "branchTip": "1fb0261627d96454a5d478560631e4bbdc9bd015",
  "baseBranch": "main",
  "commitCount": 3,
  "worktreeRemoved": true,
  "discardedPaths": ["src/half-done.py", "notes.md"],
  "taskFileUnlocked": "backlog/tasks/task-9 - Rework-the-parser.md"
}
```

`taskFileUnlocked` (task-172): the main checkout's task file this call made
writable again, or `null` when it was not locked. With no worktree, no agent
can write the branch's copy, so the lock has no reason left; the parked
branch still merges as it is.

**Behavioral notes:**

- What is left behind is a **parked** branch — one that exists and is checked
  out nowhere (`GET /api/board`'s `branchCheckout.kind == "none"`), which the
  ordinary gated merge can still merge later without a worktree.
- Nothing committed is destroyed, so there is no recovery tag: only the
  worktree's uncommitted and untracked files go, and `discardedPaths` names
  them the same way `/api/cleanup-branch` does.
- **404 when there is no worktree to remove.** Unlike the discard, this route
  has nothing to do without one, and a silent 200 would read as "abandoned"
  for a task whose worktree someone had already taken away.
- Same refusals, in the same order, as `/api/discard-attempt` — including the
  409 for a repository that moved since the preview, and the same per-task
  lifecycle lock — and the same silence about the backlog task's status.

**Errors:** 400 for a missing `project`, an invalid `taskId`, a malformed
body, or a missing/malformed `expectedBranchTip`/`expectedDirtyPaths`. 404 for
an unknown project or no worktree to remove. 409 for a live session, a branch
checked out outside Centrale, or a repository that moved since the preview.
500 for a failed `git status` or `worktree remove`. 502 if the project's
`path` doesn't exist or `tmux` couldn't be asked.

## `POST /api/browser`

Launches (or reuses) a project's `backlog browser` process. See "Opening a
project's Backlog.md board" in [docs/board.md](board.md#opening-a-projects-backlogmd-board) for the full port-selection and
launch-verification behavior this triggers in `browser.py`.

**Body:** `{"project": "<name>"}`.

```bash
curl -s -X POST http://127.0.0.1:7420/api/browser \
  -H 'Content-Type: application/json' \
  -d '{"project": "my-app"}'
```

**Response shape** (derived from `browser.launch_or_reuse`; not fired
against the live server for this doc — it launches a real, long-running
`backlog browser` process):

```json
{"url": "http://127.0.0.1:6421", "versionDrift": null}
```

The URL is the board's base URL only — this endpoint knows nothing about
tasks. A caller that wants a specific task appends Backlog.md's own route
to it (`/board/<TASK-ID>`, URL-encoded), which is what the task drawer's
"Open task" button does.

| Field | Type | Notes |
| --- | --- | --- |
| `url` | string | The board's base URL, `http://127.0.0.1:<port>`. |
| `versionDrift` | object \| `null` | `null` while the board and the `backlog` on `PATH` agree — the normal case. Otherwise `{"running": "1.50.1", "cli": "1.51.0"}`: the version the board answers with on its own `/api/version`, and the version `backlog --version` prints. A running board keeps the version it started with, so upgrading the package on disk leaves it serving the old one. Derived per request, never cached, and never acted on: nothing is killed or restarted on a difference. Also `null` when either side can't be asked (no answer from the board, no `backlog` on `PATH`) — a version difference nobody can establish is silence, not a warning. |

The UI raises the difference as a toast beside the board it just opened,
naming both versions and the fix (stop that board, open it again). See
"Opening a project's Backlog.md board" in [docs/board.md](board.md#opening-a-projects-backlogmd-board).

**Behavioral notes:** a `backlog browser` process this server already
launched and confirmed alive on the project's assigned port is reused as-is
— no new process, same URL. Otherwise, the assigned port
(`browserPortBase` + project index, or that project's own `browserPort`) is
bind-checked first: if something Centrale doesn't already track is squatting
it, Centrale walks forward (up to 50 ports) to a verified-free one instead of
letting `backlog` silently rebind itself somewhere unannounced. Either way,
the URL isn't returned until the launched child is confirmed both alive and
actually listening (polled for up to 2 seconds).

**Errors:** 400 for a missing `project`. 404 for an unknown project. 500 if
the process fails to launch, no free port can be found within the walk
limit, or launch verification fails (the child died immediately, or never
started listening in time).

## `GET`/`POST /api/settings`

Reads or writes a whitelisted subset of `projects.json`. See "Settings" in
[docs/configuration.md](configuration.md#settings) for the operator-facing view (the gear icon) this backs.

`GET /api/settings`'s response shape is documented above. `POST` accepts a
**partial** version of that same shape — any field may be omitted, leaving
that setting untouched — plus three fields with no read counterpart in that
shape: `agents` (accepted in a richer form than `GET` returns — the whole
map, not just its keys) and the two write-only editing operations
`removeProject` and `addProject`. The two read-only fields, `agentEntries`
and `projects`, are not accepted back.

| Field | Type | Notes |
| --- | --- | --- |
| `harvestMode` | `"click"` \| `"auto"` | Same values as `projects.json`'s `harvest.mode`. |
| `sessionPreviewMode` | `"interact"` \| `"view"` \| `"off"` | Stored as `projects.json`'s `sessionPreview.mode`. `"interact"` (default): live pane plus the reply row. `"view"`: read-only pane only — the reply row is absent *and* `POST /api/session-input` refuses (403). `"off"`: no drawer section, and `GET /api/session-pane` refuses too. One tiered key so the reply can be switched off independently of the read-only pane. The settings modal shows it as two toggles. |
| `refreshIntervalSeconds` | integer ≥ 5 | |
| `checkCommands` | `{"<project>": "<command>" \| null, ...}` | `null` (or an empty/whitespace string) clears that project's check gate. Only named projects are touched. |
| `maxAgents` | `{"<project>": positive integer \| null, ...}` | Sets a project's live-session cap. `null` clears it. Booleans, zero, negative, fractional and string values are invalid. Applies to the next spawn/resume; existing sessions remain. |
| `worktreeLinks` | `{"<project>": ["relative/path", ...] \| null, ...}` | Sets links for new worktrees; `null` or `[]` clears. Paths must be canonical, literal, repo-relative and non-overlapping; see [Configuration](configuration.md#setup--configuration). Existing worktrees stay unchanged. |
| `requireAgentAssignment` | boolean | Whether `POST /api/spawn` refuses (409) a task whose first assignee names no configured agent, rather than launching `defaultAgent` with a warning. Anything but a JSON boolean is a 400. |
| `lockSpawnedTaskFiles` | boolean | Whether `POST /api/spawn` makes the main checkout's copy of the task file read-only after its claim commit (task-172). Takes effect on the next spawn; existing locks are kept. Anything but a JSON boolean is a 400. |
| `defaultAgent` | string | Must name a key in the configured `agents` map — or, when `agents` is in the same request, in the map that request installs. |
| `agents` | `[{"name", "cmd", "promptSuffix"}, ...]` | The **whole** agents map, in the order it should be written — not a patch. `cmd` is an argv list of strings or one shell-quoted string (split with `shlex`, the exact inverse of `agentEntries`' `cmdText`), and must be non-empty. `promptSuffix` is optional (`null`/blank means none). Names must be unique case-insensitively (agent resolution lowercases both sides). The two built-in names (`claude`, `codex`) can be edited but not dropped, the map can't end up empty, and the current default agent must survive — or the same request must name a replacement. Whether `cmd[0]` exists on `PATH` is deliberately *not* validated; it becomes a `warnings` line instead (below). |
| `removeProject` | string | A project name. Deletes only its `projects.json` entry — never the repo itself, never its worktrees/branches. Removing the *only* configured project is allowed: a board with zero projects is a supported state, the same one a first run shows. Refused (400) only while the removal would strand work Centrale is managing — a live agent session in that project, or a `task/*` branch not yet merged into the branch its checkout is on — and the reason names the session or branches in the way. |
| `addProject` | `{"name", "path", "initBacklog": bool}` | Defaults to `false`; `true` opts into Backlog.md and Centrale setup, even if Backlog already exists. |

```bash
curl -s -X POST http://127.0.0.1:7420/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"refreshIntervalSeconds": 15, "harvestMode": "auto"}'
```

**Response, success (200):** the same shape as `GET /api/settings`, updated,
plus a `warnings` key — a list of strings, present on every successful save.
It is empty unless the request carried `agents`, in which case it holds one
line per saved agent whose `cmd[0]` wasn't found on `PATH`:

```json
{"warnings": ["agent 'my-cli': 'my-cli' was not found on PATH -- a spawn with it will fail until it is installed or the command is fixed"]}
```

A warning never means the save was refused: the agent is written either way
(you may be about to install it, or be editing a config for another machine).

**Response, validation failure (400):**

```json
{
  "error": "invalid settings: refreshIntervalSeconds: must be at least 5",
  "fields": {"refreshIntervalSeconds": "must be at least 5"}
}
```

`fields` keys are dotted for a per-project, per-agent or per-add field, e.g.
`"checkCommands.my-app"`, `"maxAgents.my-app"`, `"worktreeLinks.my-app"`,
`"agents.0.name"`, `"agents.1.cmd"`,
`"addProject.name"`, `"addProject.path"`. A structurally malformed body
(`checkCommands`, `maxAgents`, `worktreeLinks` or `addProject` present but not an object, `agents` present
but not a list) responds 400 with just `{"error"}`, no `fields`.

**Behavioral notes:**

- **All-or-nothing.** Every *provided* field must pass validation before
  anything is written — `projects.json` and the live in-memory config either
  both update fully, or neither changes at all. A live config update means
  most settings take effect immediately, no restart (`harvestMode` on the
  auto-harvest thread's next ~30s cycle; `refreshIntervalSeconds` and
  `sessionPreviewMode` on the frontend's next board poll, the latter also
  on the very next `/api/session-pane` or `/api/session-input` request;
  `checkCommands`/`maxAgents`/`worktreeLinks`/`defaultAgent`/`agents`/`requireAgentAssignment` on the
  next spawn or merge attempt).
- `addProject` setup (when `initBacklog: true`) runs only after every request
  field validates and before the config write. It uses the same pointer
  installation and scoped commit as `POST /api/setup-project`, including its
  409/500/502 errors. On failure no project is added, but setup may leave
  files requiring attention. If the later `projects.json` write fails, the
  already-completed repository setup commit remains.
- `projects.json` is rewritten atomically (temp file + `os.replace`), read
  fresh from disk each time — every key outside the whitelist, and every
  other field on an existing project entry (`path`, `browserPort`, ...),
  round-trips through untouched.

---

## Agent lifecycle events (the `CENTRALE_EVENT_URL` contract)

Every spawned or resumed session's environment carries a
`CENTRALE_EVENT_URL`, e.g.

```
http://127.0.0.1:7420/api/agent-event?project=my-app&task=TASK-2
```

This is the entire contract for a custom agent to participate in
`agentState` reporting (the `"agentState"` field on `GET /api/board`'s tasks
and `GET /api/sessions`' sessions):

> If the agent (or a hook/wrapper you configure around it) `POST`s
> `{"state": "working" | "waiting" | "finished"}` to that URL at the right
> moments, it shows up as that task's `agentState` — nothing else is
> required, and nothing else is read.

The `POST` must carry `Content-Type: application/json` and a real body, like
every other `POST` here — see "Request requirements". A reporter that isn't
a browser needs nothing else beyond keeping the URL's loopback host: it
sends no `Origin`/`Referer` and no `Sec-Fetch-Site`, and its HTTP client
derives the required `Host` header from the URL itself. Rewriting
`CENTRALE_EVENT_URL` to reach Centrale under some other name for the same
machine is refused with `403`.

A custom agent/hook you wire up yourself reads `CENTRALE_EVENT_URL`; it
is the only name the notify helper accepts.

Centrale wires this up automatically, with no config required, for the two
built-in agent families:

- **claude** — Claude Code hooks settings
  (`UserPromptSubmit`/`PreToolUse` → `working`, input-request `Notification` → `waiting`,
  `Stop` → `finished`) passed inline as JSON via `--settings '<json>'` on
  the launched command, never a shared file. Each hook shells out to `centrale_notify.py`.
- **codex** (≥ 0.150.0, feature-probed) — equivalent raw transitions as
  inline `-c hooks.<Point>=...` config overrides on the codex argv itself
  (never a worktree-local file — see "Agent lifecycle events" in
  [docs/agents.md](agents.md#agent-lifecycle-events) for why). Its built-in URL
  carries `agentKind=codex`, so a raw `finished` event is exposed as
  `agentState: "idle"`: codex emits the same turn-end sequence when done
  and when awaiting a plain chat reply. The UI says "turn ended · may need
  input" and explains this honest trade-off in its tooltip. Claude's
  trustworthy `waiting`/`finished` distinction remains unchanged.

  Codex `PermissionRequest` also fires for automatic approval review.
  `waiting` therefore starts a 30-second candidate, cancelled by `working`
  or `finished`; `PostToolUse` now reports `working` too. The badge and wait
  stream only expose waiting after a pane capture confirms a dialog, using
  delivery's menu/footer signatures. Without a dialog, confirmation retries
  every 30 seconds. Capture failure exposes unknown and retries, except a
  gone session ends the candidate. Codex `finished` similarly becomes a
  stop candidate: after one second, the current screen must no longer show
  an interruptible working footer before idle is published. A dialog
  publishes waiting; working/empty/failed captures retry each second
  (empty/failure exposes unknown). New activity cancels the candidate.
  Claude idle reminders are filtered by notification type in the injected
  helper and never publish waiting; actual prompts retain their timing.
  These checks feed both Needs-you and orchestrator-wait through the same
  lifecycle store. No persistent state is added.

Any other agent — a fully custom `cmd` in `projects.json`, or the
`CENTRALE_SPAWN_CMD` test override — gets `CENTRALE_EVENT_URL` in its
environment (a configured agent
also gets `CENTRALE_AGENT=<name>`; the override gets the URL alone) and no
hook injection at all; Centrale doesn't know how to hook it, and never
tries.

**Lifecycle metadata is ephemeral, in-memory, and keyed by
`(project, taskId)` — not by tmux session name**, so state and built-in kind
survive a session being killed and re-spawned/resumed, but reset on every
server restart. At startup a live session whose fleet-journal row shows a
settled state of the same tmux session instance gets it back (task-201);
every other task then reads `agentState: "unknown"` and
`agentKind: "unknown"` until an event arrives. A built-in session's next
event restores both because its URL carries `agentKind`. This remains a live
status hint, not Centrale-owned task data.

See `POST /api/agent-event` above for the exact request/response shape this
resolves to.

## Static files (not JSON, listed for completeness)

Three more `GET` routes exist alongside the JSON API above, all serving files
from `static/` rather than returning JSON:

- `GET /` — serves `static/index.html` (the frontend document shell; its JS is
  one `static/*.js` file per concern, its CSS `static/styles.css` and its tab
  icon `static/favicon.svg`, all fetched via `/static/`), fresh from disk on
  every request.
- `GET /static/<path>` — serves any other file under `static/` by relative
  path, refusing (400) any path that would resolve outside that directory. A
  missing file is a 404; both responses use the same `{"error": ...}` shape
  the JSON API uses on failure, even though a success response here is the
  raw file, not JSON.
- `GET /favicon.ico` — serves `static/favicon.svg` (as `image/svg+xml`;
  browsers go by the content type, not the extension). index.html declares
  the icon, so browsers don't fall back to this conventional path, but a
  direct hit gets the icon rather than a 404.

Any path matching none of these three nor one of the `/api/*` routes above
responds 404 `{"error": "not found"}`.
