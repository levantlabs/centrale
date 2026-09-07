# Centrale

A thin local dashboard aggregating Backlog.md boards across repos, with
agent spawning and gated merging. **Read `MANIFESTO.md` before designing
anything** -- it is the six positions this project actually holds and the
incident behind each one, and the ground rules below are what they cost in
practice. **Then read `docs/architecture.md`** for the module layout and how
a spawn flows end to end before writing code.

The manifesto is a constraint, not colour: a change that contradicts one of
its positions may still be right, but it is a change to the constitution and
has to be argued as one -- make the case, file a decision record, amend the
document -- rather than arriving as an implementation detail.

Ground rules the architecture doc expands on:
- Python 3.12 stdlib only, except the maintainer-only tooling documented at
  docs/operations.md#regenerating-the-documentation-screenshots; frontend is
  a vanilla static/index.html document
  shell plus one plain `<script src>` per concern under static/ (state, dom,
  api, tasks, feedback, board, spawn, harvest, sessions, drawer, pane, shell,
  settings, main -- loaded in that order) and static/styles.css. No build
  step, no bundler, no modules; the seam between the JS files is the single
  window.Centrale namespace object documented at the top of static/state.js.
- All task data access goes through the `backlog` CLI; Centrale stores no
  task state of its own — lifecycle states are derived from backlog + git
  + tmux on every read.
- Every subprocess call goes through an injectable boundary (`run_git`,
  `run_tmux`, `run_backlog`, ...); tests are hermetic — never launch real
  agents, tmux sessions, or backlog browsers in tests.
- Frontend tests come in two tiers (see docs/architecture.md, "Testing"):
  source-SHAPE contracts that grep `static/` through
  `tests/source_contract.py`, and BEHAVIOURAL tests that drive the real
  sources under `node` over `tests/js_harness.py`. A claim about what the
  frontend does belongs in the second; `node` is optional in development
  and that tier skips without it, leaving the suite green. A RELEASE is
  the exception: `scripts/release.sh` requires `node` on the release
  machine and runs the staged snapshot's suite with
  `CENTRALE_REQUIRE_NODE=1`, so a tier that would skip fails instead --
  nothing else greps or parses the JavaScript being published. The same
  release also runs `tests_integration/` (real git, tmux and server
  processes, sandboxed; opt-in everywhere else) against the staged
  snapshot, with `tmux` a release-machine prerequisite and
  `CENTRALE_REQUIRE_INTEGRATION=1` making a tool-gated skip a failure.
- Run `python3 -m unittest discover tests` before calling work done.
- Any new resource outside the repo (cache file, port, socket, temp path)
  must be overridable via env or config, documented, and explicitly
  flagged in your report — parallel agents' isolation assumptions break
  the moment an undeclared shared resource appears. Tests that launch
  real processes must namespace every such axis (XDG_CACHE_HOME, tmux
  -L, ephemeral ports) and assert their global footprint is zero.
- Report anything in your environment you did not cause.

This file (`AGENTS.md`) is the canonical orientation for anyone working in
this repo, human or agent, regardless of which coding tool they're using.
`CLAUDE.md` points here rather than duplicating it — if the two ever
disagree, this file wins; update both together.

## The Backlog.md workflow section below is for the development repo

Centrale's own tasks live in a `backlog/` directory that is deliberately
excluded from the published snapshot — see
[Releasing](docs/operations.md#releasing) for the decision and how it is
enforced. The Backlog.md instructions below are written as though this repo
has a board; in a public clone it does not. Read them as: *when you work in
a repo that has a board, this is how to use it* — including whatever repo
Centrale is pointed at. Everything above this line applies to every clone.

## `task-NN` in a comment cites that same unpublished board

Comments and docs throughout this tree tag an explanation with the task
that produced it -- `# task-78: an agent name is matched case-insensitively
against a task's assignee`, `(task-65/task-67)`, `(task-89, split out of
task-83's single app.js)`. Those ids name entries on the `backlog/` board
described above, so a public clone has nothing to open them with.

Nothing is missing when you can't. Each citation is a provenance tag on a
sentence that already carries its whole reason inline -- read the comment,
not the id -- and the ids stay resolvable for anyone working in the
development repo, which is why they are kept rather than stripped. Add them
the same way in new code: state the reason, then tag it.

<!-- BACKLOG.MD GUIDELINES START -->
<!-- backlog.md-instructions-version: 1.51.0 -->
<CRITICAL_INSTRUCTION>

## Backlog.md Workflow

This project uses Backlog.md for task and project management.

**At the beginning of each conversation in this project, run `backlog instructions overview` before answering or taking action. Re-read it only if you have not read it yet in the current conversation.**

Use the overview to decide whether to search, read, create, or update Backlog tasks.

Before task lifecycle actions, read the matching detailed guide:
- `backlog instructions task-creation` before creating or splitting tasks
- `backlog instructions task-execution` before planning, changing status or assignee, adding a plan or implementation notes, or implementing task work
- `backlog instructions task-finalization` before checking acceptance criteria, writing final summaries, or moving tasks to terminal statuses

Use `backlog <command> --help` before running unfamiliar commands. Help shows options, fields, and examples.

Do not edit Backlog task, draft, document, decision, or milestone markdown files directly. Use the `backlog` CLI so metadata, relationships, and history stay consistent.

</CRITICAL_INSTRUCTION>
<!-- BACKLOG.MD GUIDELINES END -->
