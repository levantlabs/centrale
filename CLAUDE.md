# Centrale

**Read `AGENTS.md` first, and `MANIFESTO.md`, which it sends you to.** The
first is the canonical orientation for this repo (manifesto and architecture
pointers + ground rules); the second is why the project refuses the obvious
alternatives, and it constrains design decisions here. Identical guidance for
Claude Code, Codex, or any other agent working here. This file intentionally
doesn't duplicate either of them; if they ever need to change, update them
together.

**The Backlog.md section below is for the development repo.** Centrale's
own board lives in a `backlog/` directory that is deliberately excluded from
the published snapshot (see `docs/operations.md`, "Releasing"), so a public
clone has no board here — those instructions then describe how to work in a
repo that does have one, not this one.

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
