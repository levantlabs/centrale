# Manifesto

Centrale is a thin local dashboard over
[Backlog.md](https://github.com/MrLesk/Backlog.md): one board across every
repo you point it at, coding agents spawned from that board, their branches
merged back through explicit gates. That is the *what*, and
[README.md](README.md) covers it.

This is the *why*. Centrale refuses a number of obvious, convenient things,
and the refusals are the design. Every position below was paid for, so each
carries the incident that taught it: the incident is the argument, and a list
of adjectives would not be worth your time.

It is short on purpose, because it is meant to be read *first*, at the start of
a session, before the code. A constitution nobody rereads is decoration.

---

## Derive, do not remember

Centrale stores no task state of its own. The board, the live sessions, the
branch states and the merge verdicts are re-derived on every request from the
`backlog` CLI, from `git` and from `tmux`. No database, no synced copy of a
task, no cache of what a branch looked like. Delete Centrale and no task data
is lost, because none of it was ever here.

The reason is not purity. A remembered fact about a moving system goes wrong
*quietly* — it keeps rendering, confidently, long after it stopped being true.

The one place the product let itself remember an answer proved it.
Click Merge on a branch that has fallen behind; the gate fails and the drawer
shows a red conflict message. Resume the agent; it resolves the conflict. End
the session — and the same red message is back, describing a conflict that no
longer exists. Nothing recomputed it: it was the stored response to a POST
from minutes earlier. Every gate had been correct on every run; the bug lived
entirely in the memory.

The corollary is what matters when you write new code here: when Centrale does
not know something, it must show that it does not know, rather than the last
thing that was true. The fix was not a smarter cache but dropping the stale
verdict and falling back to a plain Merge button — Centrale does not know the
gate outcome until it asks again, and your next click asks.

## Gates over trust

Nothing merges because an agent said it was finished. "Done" is a claim, and a
gate is the test of the claim.

A branch merges only after five checks pass — no live session on the task, the
task actually `Done` with every acceptance criterion ticked, a clean worktree,
a conflict-free dry-run merge, and an optional per-project test command run
against the *merged* tree rather than the branch — plus a sixth re-check of the
destination immediately before the real merge, because a tree can move between
the decision and the act.

Two details are load-bearing and easy to erode by accident. **Centrale never
decides that a task is done** — the gate reads a status somebody else set, and
a Centrale that could set it would be checking its own homework. And **the
agent is told, in the prompt it is spawned with, not to merge its own branch**,
because an agent that integrates its own work is an agent whose
self-assessment is the last word.

The reason to distrust the claim is not suspicion of agents; it is that the
claim is often *structurally* unable to be true. A codex session reported
"finished" while it sat blocked asking the user to approve a design.
Probing the hook stream showed why, and the finding was worse than the bug:
codex emits the identical sequence for "I am done" and "I ended my turn to ask
you a question", so the two are not distinguishable at all from outside. The
fix was not a cleverer inference — it was to stop claiming the distinction, and
label that state "turn ended · may need input". An agent's self-report is a
data point about the agent, never evidence about the work.

Gates also have to be legible, not merely correct. A failure names which gate
failed and what it measured — "4 uncommitted files now, not the 3" — because a
refusal you cannot act on is indistinguishable from a bug.

## A thin veneer over Backlog.md, not a replacement

The board is the source of truth. Centrale is a companion to Backlog.md, not a
layer that gradually absorbs it, and the test of that is who may change a
task's status.

Centrale writes to a board in exactly two places, and in neither is it forming
an opinion. Spawning claims the task (`In Progress`) and commits that claim —
you clicked Spawn, and the claim records it. A merge can carry a `Done` you
already set on the board across onto the agent's branch, so the gate can see
the status you already chose. That is all.

What it deliberately does *not* do is the sharper illustration. Discard and
abandon both destroy real work, and both leave the task's status exactly where
it was. Sending the task back to `To Do` on a discard would have
been trivial and superficially helpful. Discarding an attempt is not the same
statement as deciding where the task now stands, and the second statement is
yours to make. Convenience is not reason enough to put words in the board's
mouth.

The same restraint binds the people and agents working here, which is where it
was learned. A task was filed, spawned, implemented and merged while a
refinement to it was still being thought about; treating the refinement as an
amendment rewrote a finished record, leaving a task marked Done whose stated
scope nobody had delivered. It was caught and restored from git, and the rule
became a standing decision: new work on a finished task is a new task. A board
is only a source of truth for as long as nobody edits history into it.

## Remove what usage has not justified

Controls are removed as readily as they are added, and "it already exists" is
not an argument for keeping one.

The reply row once had three quick keys — `y`, `Enter`, `Escape` — for
answering an agent in one click. Nobody used them. Bare `y` answered
a prompt style the current CLIs barely produce; `Enter`'s only real job had
been working around a delivery bug that was since fixed; and interrupting an
agent, in practice, means attaching to the session anyway. So they went — and
not by hiding the buttons. The whole path went, including the `{"key":...}`
body the API accepted, which now returns a plain 400. A feature that is
disabled but still reachable is still a feature you maintain.

The bet is the same every time: one button is cheap to build later with
evidence behind it, while three nobody uses are not cheap, only invisible.

## Destruction must be recoverable, and bound to what was reviewed

Discarding an attempt — worktree removed, unmerged branch deleted — is the one
irreversible act in the product, and it is held to two rules nothing else is.

**The recovery has to be real, not a gesture.** The obvious implementation
hands back the branch tip SHA with a `git branch <name> <sha>` command and
calls that recoverable. A probe run before shipping showed it is not: after `git worktree remove --force` and `git branch -D`, no reflog
anywhere still referenced the tip, and `git gc --prune=now` destroyed the
commits outright. So Centrale cuts a lightweight `abandoned/task-<id>-<stamp>`
tag *before* removing anything, and surfaces the tag, the SHA and the restore
command where a human will see them. A recovery promise git does not keep is
worse than no promise.

**The confirmation has to describe the state actually destroyed.** The
confirming click names real counts — "3 commits and 4 uncommitted files", never
a generic *are you sure* — and the request carries those counts back, where the
server re-measures under a per-task lock and refuses if anything moved. It was reproducible against a real repo: preview 2 commits, another
lands, and the armed action destroys 3 while reporting 2. An informed
confirmation not bound to the state it described is not informed consent; it is
a countdown.

## An escape hatch is the product admitting failure

If the honest answer to "how do I get out of this state" is `tmux
kill-session`, that is a bug, and it gets filed as one.

The dead end was real. A live session whose agent state read
"unknown" offered *nothing*: no End session, because "unknown" sat outside the
allowlist of states safe to kill; and no Merge, Discard or Abandon, because a
session was live. Each rule was defensible alone. Together they left a task
with a branch, a dirty worktree and not one available action — and not
exotically, since lifecycle badges live in memory and one server restart could
strand every running session in exactly that state.

Both lessons generalise past that bug. **Prefer the cheap wrong action to the
absent one**: every live session now gets an End session, "unknown" included,
and the one state that is a positive signal the agent is mid-turn costs a
second click to confirm rather than a missing button. The worry that an agent
might be busy is far cheaper than the user reaching for `tmux`. And **a
withheld action is rendered disabled with its reason, never omitted** — Merge,
Discard and Abandon still refuse while a session is live, but they say so and
name the session, because a control you cannot see gives you no way to tell
whether it is missing, broken, or deliberately absent.

---

## Using this document

These are constraints on new work, not history. A change that contradicts one
is not automatically wrong, but it is a change to the constitution and has to
be argued as one rather than arrive as an implementation detail: make the
case, file it as a decision record, then amend this document. If the code and
this document disagree, one of them is a bug.

Everything above describes a product that has shipped — what Centrale does as
of v0.1.0, see [CHANGELOG.md](CHANGELOG.md) — not what it is hoped to become.
The `task-NN` tags cite this project's own board, which does not ship;
[AGENTS.md](AGENTS.md) explains why nothing is missing when you cannot open
one.
