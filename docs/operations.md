# Centrale operations

Running Centrale (`--check`, restarting after a change, systemd), cleanup,
troubleshooting, the tests, the manual smoke test, regenerating the
documentation screenshots, known limitations, and releasing. Part of the Centrale manual; the [README](../README.md) is the
front door and lists the other chapters.

## Running it

First, check that everything Centrale needs is actually in place:

```bash
python3 server.py --check
```

This reports one `[PASS]`/`[WARN]`/`[FAIL]` line per prerequisite. First
the build itself — `[PASS] Centrale v0.1.0`, the one line worth quoting in
a bug report (see [Versioning](#versioning)) — then Python
version, `git` and `backlog` on `PATH` (with the version each reports),
`tmux` on `PATH` (a `[WARN]`, not a `[FAIL]`, if it's missing — see "Without
tmux" in [docs/agents.md](agents.md#without-tmux)), whether a codex agent's own `bwrap`-based sandbox will
actually work if `bwrap` is installed (Linux only; see "Troubleshooting"
below if it's `[WARN]`ed), whether `projects.json` parses (a `[PASS]` with
first-run guidance if there's no `projects.json` at all — see "Quickstart"
in the [README](../README.md#quickstart)), for each configured project, whether its `path` exists and has a
`backlog/config.yml`, and — when a Centrale is already serving the
configured `port` — whether that process is behind the checkout it runs
from (see [Restarting after a change](#restarting-after-a-change): a
`[WARN]` naming the commit it loaded and the one now at `HEAD`, or a
`[PASS]` that it is running its checkout's current code; nothing at all
when nothing is listening). It never starts a server or binds a port, so it's
safe to run anytime, including while a real Centrale instance is already up.
Exits `0` if nothing `[FAIL]`ed (a `[WARN]` alone — no tmux, a restricted
codex sandbox, or one project not set up yet — doesn't fail it, matching
how Centrale itself degrades: everything else still works), `1` otherwise.
Fix anything it flags before continuing.

Then start the server itself:

```bash
python3 server.py
```

This prints `Centrale serving on http://127.0.0.1:<port>` and then serves
until you interrupt it (Ctrl-C). Open the printed address in a browser, e.g.
`http://127.0.0.1:7420` for the default port (a zero-config or freshly
copied `projects.example.json` start both use it). If that `port` is
already taken — most likely another Centrale instance still running —
startup fails with one clear line naming the port and the likely cause,
instead of a raw traceback.

The server binds to `127.0.0.1` only and has no authentication of any kind.
Do not expose it on a network interface, port-forward it, or run it anywhere
other than your own machine.

Binding `127.0.0.1` keeps the *network* out, but it does not keep *web pages*
out: every site you visit while Centrale runs can send it requests from your
own browser. So Centrale refuses any request — `GET` as much as `POST` —
that could not have come from its own UI:

- the `Host` header has to be `127.0.0.1`, `localhost` or `[::1]` at the
  port this process actually bound. This is what refuses **DNS rebinding**:
  once an attacker points their own hostname at `127.0.0.1`, your browser
  treats their page as same-origin with Centrale and CORS stops nothing —
  but the `Host` their page sends still says *their* name;
- `Sec-Fetch-Site`, which a browser fills in and a page cannot forge or
  suppress, has to say `same-origin` or `none`. This is what refuses a
  direct cross-site request — an `<img>`, `<script>` or form aimed straight
  at `http://127.0.0.1:7420/...`, which otherwise carries a valid `Host`
  and no `Origin` or `Referer` at all. One side effect: a **link from
  another site** to Centrale is refused too; open it from a bookmark or by
  typing the address;
- any `Origin`/`Referer` it carries has to be Centrale's own;
- and a `POST` has to declare `Content-Type: application/json`.

Reads are covered because reads are not harmless here: `GET /api/harvest`
runs the whole merge gate for every task branch — a scratch worktree, a real
merge, and your project's own `checkCommand` — and the board, settings and
live pane contents are worth reading on their own.

That is a cross-site check, not a login: there are still no accounts,
passwords or tokens, and anything running as you on this machine (`curl`, a
script, an agent's notify hook) can drive it freely — none of them send
`Sec-Fetch-Site`, and their `Host` comes from the `http://127.0.0.1:<port>`
URL you already give them. See "Request requirements" in
[docs/api.md](api.md#request-requirements) for the exact rules a script
needs to satisfy.

### Restarting after a change

`static/index.html`, every `static/*.js` file and `static/styles.css` are
served fresh from disk on every request — editing them takes effect on your very next
browser reload, no restart needed. Every
`.py` file (`server.py`, `spawn.py`, `browser.py`, `harvest.py`,
`settings.py`, `version.py`) is loaded once at process start, though: editing any of them
(or `projects.json` — see "Setup / configuration" in [docs/configuration.md](configuration.md#setup--configuration)) has **no effect**
on an already-running server. Stop it (Ctrl-C, or `kill` its PID — both are
a clean SIGTERM shutdown, see "Opening a project's Backlog.md board" in [docs/board.md](board.md#opening-a-projects-backlogmd-board))
and start it again to pick up the change.

You don't have to remember that, or notice it from behaviour that quietly
matches old code. The server knows which commit it booted from (the
version in the sidebar footer — see [Versioning](#versioning)) and, on
every board refresh, compares it with `HEAD` of the checkout it is running
from. The moment they differ — a merge landed, a commit was made — a
banner appears across the top of the board on the browser's next refresh,
naming both commits and how many commits the process is behind:

> The code on disk has changed since this server process started: it
> loaded `055ff59`, the checkout is now at `a088d15` (23 commits later).
> Restart Centrale to load it — after checking no merge is in flight.

It stays until the restart; there is no dismiss, and deliberately no
restart button (a restart while a merge is running is unsafe — the merge
lock lives in the process). `python3 server.py --check` reports the same
comparison for the terminal. The banner cannot say *which* of those
commits touched Python: a merge of a stylesheet and a merge of `server.py`
read the same, and a frontend-only change needs only a browser reload —
but a restart is always correct, and cheap. The comparison is derived from
git each time and stored nowhere, and any doubt is silence rather than a
false alarm: a checkout with uncommitted edits is not behind (the process
loaded that commit; `HEAD` is still that commit), a downloaded snapshot
with no `.git` never shows it, and neither does a machine without `git`.
This signal replaces the working discipline recorded in decision-2 —
"restart after any merge touching Python; nothing in the UI reveals a
stale process" — which is how the gap was handled before it existed.

If you're backgrounding the server (`nohup python3 server.py > centrale.log
2>&1 &`, a systemd unit, or anything else that isn't an interactive
terminal), pass `-u` (`python3 -u server.py`) or set `PYTHONUNBUFFERED=1`:
Python fully buffers stdout whenever it isn't a TTY, so without one of
these, the one line confirming the server actually started can sit
unflushed in the log indefinitely, even though the server is already up and
serving — only the `stderr` diagnostics (a bind failure, the tmux warning,
the boot sweep) are unbuffered and show up immediately either way.

### Running as a systemd user service

To have Centrale start automatically and restart itself if it ever crashes,
put this in `~/.config/systemd/user/centrale.service` (adjust
`WorkingDirectory` to wherever you cloned this repo):

```ini
[Unit]
Description=Centrale dashboard

[Service]
WorkingDirectory=%h/code/centrale
ExecStart=/usr/bin/python3 -u server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now centrale.service
journalctl --user -u centrale.service -f   # follow the log
systemctl --user restart centrale.service  # after a code/config change
```

A user service only runs while you're logged in (or after `loginctl
enable-linger $USER`, if you want it running before login too). There's no
system-wide unit example here on purpose — Centrale is meant to run as your
own user, never as root or a shared service account.

## Cleanup

For an attempt you are deliberately throwing away, the drawer now does all
of this for you: **Discard attempt** removes the worktree and
deletes the branch (leaving a recovery tag and command first), and
**Abandon worktree, keep branch** removes only the worktree. See
["Discarding a bad attempt"](agents.md#discarding-a-bad-attempt). The manual
steps below are still the way out for everything those two don't cover — a
session to kill by hand, a worktree Centrale doesn't manage, stale worktree
metadata — and for doing it without the UI.

Centrale otherwise does not clean up worktrees, branches, or tmux sessions
left over from a *killed* spawn (merging only ever cleans up after a merge
it just performed) — that is a manual step you own.
After you're done with a spawned task's work you don't intend to merge
(abandoned, or merged some other way):

1. Kill the tmux session:

   ```bash
   tmux kill-session -t centrale-<project>-<taskid>
   ```

2. Remove the git worktree, run from the source repository:

   ```bash
   git worktree remove <worktreeRoot>/<project>-<taskid>
   ```

   Add `--force` if the worktree has uncommitted changes you're intentionally
   discarding.

3. Delete the branch, also from the source repository:

   ```bash
   git branch -d task/<taskid>
   ```

   Use `-D` instead of `-d` to discard the branch even if it has unmerged
   work.

4. Optionally inspect and tidy up stale worktree metadata:

   ```bash
   git worktree list
   git worktree prune
   ```

Worked example, cleaning up after spawning on `my-app` task `TASK-2`, with
the shipped `worktreeRoot: "@repo"` (worktree root
`~/code/my-app/.centrale-worktrees`):

```bash
tmux kill-session -t centrale-my-app-task-2
cd ~/code/my-app
git worktree remove .centrale-worktrees/my-app-task-2
git branch -d task/task-2
git worktree list
```

With a plain-path `worktreeRoot` set explicitly instead, the worktree path
is the same shared directory for every project, e.g.
`git worktree remove ~/code/.centrale-worktrees/my-app-task-2`.

## Troubleshooting

Run `python3 server.py --check` first (see "Running it" above) — it catches
most of the causes below in one pass.

- **"could not bind 127.0.0.1:\<port\>"** on startup — something is already
  listening on that port, most likely another Centrale instance (see
  "Running it" above for the exact message). Find and stop it
  (`lsof -i :7420` or `ss -ltnp | grep 7420`), or change `port` in
  `projects.json` and restart.
- **A `backlog browser` process is still running after Centrale crashed
  (`kill -9`, an OOM kill, a machine crash)** — a graceful stop (Ctrl-C,
  `kill`, `systemctl stop`) always cleans up every `backlog browser`
  process Centrale itself launched, but nothing can run that cleanup code
  after a SIGKILL. Centrale's *next* startup heals this on its own: it reads
  its process registry (`~/.cache/centrale/browsers.json`, or under
  `$XDG_CACHE_HOME` if set) and kills anything recorded there that's both
  still alive and still actually a `backlog browser` process (never on pid
  alone — a reused pid is left alone), logging one line if it found
  anything (see "Opening a project's Backlog.md board" in [docs/board.md](board.md#opening-a-projects-backlogmd-board)). If you don't
  intend to restart Centrale right away, sweep by hand instead:
  `pkill -f 'backlog browser'`.
- **A previously-opened Backlog.md board tab now shows "can't connect" /
  disconnected** after you restarted Centrale — expected, not a bug. Every
  graceful Centrale stop terminates every `backlog browser` process it
  launched along with itself (see above), so any browser tab pointed at one
  goes stale the moment Centrale exits. Just click the ⧉ "open board" icon
  again once Centrale is back up; it launches (or reuses) a fresh one.
- **A backgrounded server's log file looks empty even though it's clearly
  serving requests** — Python fully buffers stdout whenever it isn't a TTY
  (a log file, a pipe, journald under systemd), so the one line confirming
  startup can sit unflushed indefinitely; only the `stderr` lines (bind
  failures, the tmux warning, the boot sweep) show up immediately either
  way. Run with `-u` (`python3 -u server.py`) or set `PYTHONUNBUFFERED=1` —
  see "Restarting after a change" above, and the systemd unit example,
  which already does this.
- **Edited `server.py`/`spawn.py`/`browser.py`/`harvest.py`/`settings.py`/
  `version.py` (or `projects.json`) but nothing changed** — these are only
  read once at process start; restart the server. `static/index.html`, the `static/*.js`
  files and `static/styles.css` are the exception — they are served fresh on every
  request, no restart needed. See
  "Restarting after a change" above.
- **A project shows an error banner on the board** — its `path` in
  `projects.json` doesn't exist, or it has no `backlog/config.yml` (the
  `backlog` CLI itself will fail there). `python3 server.py --check` names
  exactly which project and which of the two it is; every other configured
  project keeps working regardless.
- **Spawning is disabled / every Spawn button is grayed out** — `tmux`
  isn't on `PATH`. See "Without tmux" in [docs/agents.md](agents.md#without-tmux); everything except spawning
  still works.
- **A spawned `codex` agent fails immediately (its own sandbox errors out
  trying to start), while `claude` spawns are fine** — on Ubuntu 24.04+,
  AppArmor can block unprivileged user-namespace creation for any process
  without a permissive profile, and codex's own sandboxing needs exactly
  that (it shells out to `bwrap`, the same tool Centrale's own
  `--check` probes). `python3 server.py --check` reports this as
  `codex agents: bubblewrap sandbox blocked by AppArmor userns
  restriction`; confirm it yourself with
  `cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns` (`1` means
  restricted). Two fixes, in order of preference:
  - **Targeted (recommended)** — grant the exception to `bwrap` itself,
    not to every unprivileged process on the system. Save as e.g.
    `/etc/apparmor.d/bwrap-userns`:
    ```
    abi <abi/4.0>,
    include <tunables/global>

    profile bwrap-userns /usr/bin/bwrap flags=(unconfined) {
      userns,
    }
    ```
    then load it: `sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns`.
    (Confirm `bwrap`'s actual path first with `which bwrap` — adjust the
    profile if it differs, and expect minor syntax differences across
    AppArmor/Ubuntu versions.)
  - **Global (simpler, broader)** — disables the restriction for *every*
    unprivileged process on the machine, not just `bwrap`, which is a real
    reduction in this specific sandboxing protection: `sudo sysctl -w
    kernel.apparmor_restrict_unprivileged_userns=0` (add
    `kernel.apparmor_restrict_unprivileged_userns=0` to a file under
    `/etc/sysctl.d/` to persist it across reboots).
  This is non-fatal to Centrale itself either way — only codex's own
  sandboxed spawns are affected, and `--check` reports it as a `[WARN]`,
  not a `[FAIL]`. The shipped `worktreeRoot: "@repo"` (see "Setup /
  configuration" in [docs/configuration.md](configuration.md#setup--configuration)) largely sidesteps this already, since it gives a
  spawned worktree the project's own directory trust instead of an
  untrusted sibling path — both fixes above are mainly needed if you've
  switched to a custom, out-of-repo `worktreeRoot`, or for other tools
  that shell out to `bwrap` outside of a Centrale spawn.

## Tests

```bash
python3 -m unittest discover tests
```

The suite needs no network access and no running tmux server: the
subprocess boundaries (`run_backlog`, `run_backlog_raw`, `run_git`,
`run_tmux`, `which`, `run_check_command`, `launch_browser_process`,
`resolve_listener_pid`) are small, patchable functions that the tests
mock with recorded fixture data. The test suite never launches a real
coding agent, a real `backlog browser` process, or a real merge, and never
writes to a git repository. It reads one: `tests/test_release_snapshot.py`
asks git for the file list `git archive HEAD` would produce, to prove a
release still excludes `backlog/` (see [Releasing](#releasing)) — a
read-only local call that skips itself wherever there is no repository to
read, including the staged snapshot the release gate runs it in. The
auto-merge background thread (`harvest.AutoHarvestThread`) is exercised
with an injectable clock (`sleep_fn`) and a directly-callable `run_cycle()`
so its tests never depend on real timing, and its locking is checked with
both a synchronous assertion and an empirical multi-thread run proving
concurrent attempts never overlap. `browser.py`'s launch verification and
port bind-check/walk-forward are exercised entirely through
`server.port_is_free` and `server.launch_browser_process` as injectable
boundaries, plus an injectable `sleep_fn` (same pattern as
`AutoHarvestThread`) so no test ever waits out the real verification
timeout; `port_is_free` itself, being the boundary function, is the one
exception allowed a real (localhost-only) socket bind in its own tests,
since there's nothing further downstream left to fake (the startup
bind-failure path is checked the same way — a real, second
`CentraleHTTPServer` bind on an already-taken ephemeral port — rather than
mocking `socket` itself). SIGTERM/SIGINT handling is verified by mocking
`signal.signal` and invoking the captured handler directly — no test ever
installs a real process signal handler. The subprocess-timeout config
(`configure_subprocess_timeout`) is checked by mocking `subprocess.run`
itself and reading the `timeout=` kwarg it received, restored to the
default afterward so it can't leak into other tests. The browser process
registry (`browser.py`'s `registry_path`) is redirected to a throwaway
temp file for every test that could otherwise touch it, so nothing ever
reads or writes the real `~/.cache/centrale/browsers.json`; the boot
sweep's pid-alive/cmdline-still-matches decision is exercised entirely
through `server.process_cmdline`/`server.kill_process` as mocked
boundaries, never a real `/proc` read or signal. Agent lifecycle events
(`server.ensure_hooks_settings_file`) are covered the same way, redirected
to a throwaway temp dir via `server.hooks_settings_path` rather than ever
touching the real `~/.cache/centrale/hooks-settings.json`; spawn/resume
tests that exercise the claude/codex argv injection mock
`server.ensure_hooks_settings_file` itself so no cache-dir I/O happens at
all. `server.probe_codex_hook_trust` (the `--help`-output feature
detection gating the whole codex hooks path) never launches a real codex
either: its own tests mock `server._run`, and every spawn/resume test
that reaches the codex branch mocks `server.probe_codex_hook_trust`
directly, so `codex --help` is never actually executed and the module-
level probe cache is never touched by more than one test at a time (tests
that exercise the real cache clear it via
`server._reset_codex_hook_trust_probe_cache` in setUp/addCleanup, mirroring
`server._reset_agent_events`). `server.codex_hooks_overrides()` (task-44)
is pure — no filesystem or subprocess calls at all — so its tests
(`CodexHooksOverridesTests`) run unmocked and just assert on the returned
argv, including parsing each value as real TOML via the stdlib `tomllib`
to prove it's the schema codex actually expects; `spawn._inject_agent_hooks`
tests for the codex branch (`InjectAgentHooksTests`) assert directly that
no file gets written and no git call is made (`server.run_git`/`open`
mocked and asserted never-called) — there is no worktree-local file-I/O
or git-exclude step left to test now that the overrides live entirely on
the argv. `centrale_notify.py`
(`tests/test_centrale_notify.py`) is tested both
with its own network call mocked out (argv/env gating, exception
swallowing) and, separately, against a real `ThreadingHTTPServer` bound to
`127.0.0.1:0` standing in for `/api/agent-event` — never a real agent or
external network.

### The integration tier

`tests/` is only the first of two tiers. The second lives in
`tests_integration/` and does the opposite: it runs **real** `git`, `tmux`
and `backlog` processes — and, for three modules, a real `server.py`
subprocess — inside fully sandboxed environments, to catch the class of bug
a fully mocked suite cannot see by construction (orphaned `backlog browser`
processes, port squatting, stale tmux sessions).

```bash
python3 -m unittest discover tests_integration
```

It is a separate `discover` root on purpose: nothing under
`tests_integration/` is ever collected by `python3 -m unittest discover
tests`, so the default suite stays fast and hermetic. Eleven modules are
tracked there, covering the browser launcher, the server process
lifecycle, spawn, harvest, resume, cleanup, discarding an attempt,
reconcile, the first-run settings path, the release script, and the
tier's own tmux-socket namespacing.

The tier needs `git`, `tmux` and the `backlog` CLI on `PATH` — plus `tar`,
which only the release-script module needs, and `node`, which that module
and the settings one both need; a
module whose tests need a missing tool skips cleanly rather than failing. It
sandboxes every axis it touches — throwaway git repos, a tmux socket
named uniquely per test-run process (so two concurrent runs, or a run
alongside a release, cannot kill or observe each other's sessions —
task-161), ports chosen by binding to 0, an isolated `XDG_CACHE_HOME`, bare
"remotes" that are directories inside the test's own temp tree, and
harmless probe commands in place of any real coding agent — so it never
touches your repos, your tmux server, your cache, the network, or the
running Centrale on port 7420. `tests_integration/README.md` documents
those guarantees and what each module covers.

## Manual smoke test

This is the reproducible manual check to run against a real repository
(e.g. `my-app`) after making changes, in place of exercising the real
`claude` CLI:

1. Start the server with the spawn command overridden to a harmless probe,
   so no real agent can be launched:

   ```bash
   CENTRALE_SPAWN_CMD='sh -c "sleep 300" centrale-probe' python3 server.py
   ```

2. Confirm the board reflects real data:

   ```bash
   curl -s 'http://127.0.0.1:7420/api/board?force=1' | python3 -m json.tool
   ```

   Check that the real tasks from each configured project appear, with
   `ready` flags matching what `backlog task list --ready` reports for that
   repo.

3. Confirm validation rejects bad input before anything is created — each of
   these should return a 4xx with an `error` body, and leave no worktree
   behind:

   ```bash
   curl -s -X POST http://127.0.0.1:7420/api/spawn \
     -H 'Content-Type: application/json' \
     -d '{"project": "nope", "taskId": "TASK-1"}'
   curl -s -X POST http://127.0.0.1:7420/api/spawn \
     -H 'Content-Type: application/json' \
     -d '{"project": "my-app", "taskId": "../../etc/passwd"}'
   ```

4. Spawn on a real task:

   ```bash
   curl -s -X POST http://127.0.0.1:7420/api/spawn \
     -H 'Content-Type: application/json' \
     -d '{"project": "my-app", "taskId": "TASK-1"}'
   ```

5. Verify the session and worktree were actually created, and that the
   prompt reached the spawned process:

   ```bash
   tmux list-sessions | grep centrale-
   curl -s http://127.0.0.1:7420/api/sessions
   git -C ~/code/my-app worktree list
   tr '\0' '\n' < /proc/$(tmux list-panes -t centrale-my-app-task-1 -F '#{pane_pid}')/cmdline
   ```

6. Spawn the same task again and confirm it is refused with a 409, then kill
   the session and spawn once more to confirm the re-spawn path reuses the
   existing branch and worktree instead of creating a second one.

7. Clean up using the sequence in "Cleanup" above (kill the tmux session,
   remove the worktree, delete the branch), then confirm the source repo is
   untouched with `git -C ~/code/my-app status --porcelain` and
   `git -C ~/code/my-app branch --list 'task/*'`.

## Regenerating the documentation screenshots

`docs/img/` holds the four images the README and the docs lead with —
`board-light.png`, `board-dark.png`, `drawer-pane.png` and
`session-theater.png`. They are generated, not hand-taken:

```bash
python3 scripts/screenshots.py
```

That rewrites all four in place, at the size and light/dark pairing the
docs already reference, so the only thing a `git diff` shows is the UI
change you just made. `--only board-light` (repeatable) shoots one of
them; `--out <dir>` shoots into a scratch directory instead, which is how
you look at a change before overwriting the committed set; `--keep`
leaves the temp sandbox behind and `--verbose` prints where it was.

**Re-run it after any change to the board, the task drawer, the session
theater, or the theme** — those four surfaces are all any of the images
show. There is no staleness check anywhere: regenerating and looking at
what `git status` says is the check. Re-running with no UI change
produces byte-identical files, so a diff always means something moved.

**It never renders a real board, and there is deliberately no flag that
would let it.** A screenshot is the one published artifact
[the secret and identity scan](#the-secret-and-identity-scan) cannot
check — that scan reads text, and an image is pixels — so the script
builds its own world instead: the two invented projects, tasks, branches,
sessions and pane text in `scripts/screenshots_fixture.json`, stood up in
a throwaway temp sandbox. `projects.json` is never read, every subprocess
boundary the server uses is replaced by a fake that answers only from
that fixture, and a fake asked for any directory outside the sandbox
refuses and fails the run. Nothing is spawned either: the "live" session
in the drawer and theater shots is fixture text fed through the faked
tmux boundary, never a real agent.

**Playwright and Pillow are maintainer-only tools.** They are needed to
run this script and nowhere else — no test, no release gate and no
runtime path touches them — so when either is missing the script says so
and exits, and nothing else is affected. That is deliberate: Centrale is
Python-3.12-stdlib-only with no build step, the release already asks the
release machine for `node` and `tmux` (see
[Releasing](#releasing)), and a documentation image is not worth a third
release prerequisite.

```bash
python3 -m pip install playwright pillow
python3 -m playwright install chromium
```

Everything it touches outside the repo is named and overridable: the temp
sandbox (`TMPDIR`), one ephemeral `127.0.0.1` port chosen by the OS,
`XDG_CACHE_HOME` redirected into the sandbox for the run so nothing can
reach a real `~/.cache/centrale`, and `CENTRALE_SCREENSHOT_CHROMIUM` for
an explicit browser executable when you would rather not let Playwright
download its own.

## Limitations / out of scope for v1

- View-only: there is no way to edit a task, change its status, or check off
  acceptance criteria from the board. Use the `backlog` CLI or edit tasks in
  the source repo for that.
- No authentication. Combined with binding only to `127.0.0.1`, this is
  meant strictly for local, single-user use.
- No automatic worktree or session cleanup — see "Cleanup" above.
- `POST /api/spawn` reports success once `tmux new-session` returns, which
  means it returns 200 even if the spawned command exits immediately
  afterwards and takes the session down with it. If a spawn reports success
  but no session shows up in the sessions panel, the spawn command itself
  failed to start — check `CENTRALE_SPAWN_CMD`, and remember the prompt is
  always appended as a trailing argument.
- Agent selection only knows about the agents listed in the `agents` config
  map (Claude Code and Codex out of the box); an assignee that doesn't match
  a key there falls back to `defaultAgent` rather than erroring. Any other
  agent CLI needs its own `agents` entry (as long as it accepts the prompt
  as a trailing argument), or the `CENTRALE_SPAWN_CMD` escape hatch.
- Centrale-launched `backlog browser` processes aren't guaranteed to be
  cleaned up (only a best-effort `atexit` termination on graceful shutdown)
  — see "Opening a project's Backlog.md board" in [docs/board.md](board.md#opening-a-projects-backlogmd-board).
- Depends on the `backlog` CLI's JSON contract (`schemaVersion: 1`, verified
  against CLI v1.51.0). A future upstream schema change would need a
  corresponding update here. Centrale already treats a `schemaVersion` other
  than `1` as a per-project error rather than letting it crash the whole
  board or the `/api/task` endpoint.

## Releasing

Centrale is published as a curated **snapshot**, not as the development
repo it is built in: one clean commit per release, built from `git archive
HEAD` — tracked content as of the last commit, minus anything marked
`export-ignore` — and pushed to a separate public remote. The development
repo's own history (real commit identity, the full task-by-task build log)
stays private.

So does `backlog/`, the development repo's own Backlog.md board.
`.gitattributes` marks that directory `export-ignore`, which `git archive`
honours, so no release can carry it: the board is a maintainer's working
record, and the agents working in it write local filesystem paths and
references to other repos into task notes as a matter of course — not a
surface a one-time scrub would keep clean. `tests/test_release_snapshot.py`
asserts this against the real archive listing, and the release gate below
runs the suite, so a regression fails a release instead of becoming a
publication. What that costs, recorded plainly: a tool that tracks its own
development with the tracker it complements is convincing documentation,
and the published repo does not have it. Shipping a curated subset later —
the decision records, a few exemplar tasks written for an outside reader —
is a reasonable move; shipping the whole board is not.

`scripts/release.sh` does the publishing, and nothing in it is specific to
this project — it will publish any repo the same way:

1. Reads the public remote URL and a clean release identity from
   environment variables (`RELEASE_REMOTE`, `RELEASE_AUTHOR_NAME`,
   `RELEASE_AUTHOR_EMAIL`, `RELEASE_PRIVATE_NAMES` — empty if there are
   none, but say so, see [the secret and identity
   scan](#the-secret-and-identity-scan) — and optionally
   `RELEASE_BRANCH`), or from
   `.release-remote`, a local file next to the working repo's root that is
   gitignored and never committed — see the script's own `--help`/header
   for the exact format. **The environment wins where both set the same
   name**, so a one-off `RELEASE_REMOTE=… scripts/release.sh …` does what
   it looks like it does. **The working repo never gets the public remote
   configured** — there is nothing in it for a plain `git push` to
   accidentally reach.
2. In a throwaway temp directory, clones the public repo (or starts fresh
   for a first release against an empty one) and **refuses to go on unless
   every commit already on the release branch carries the configured
   release identity — name and email, as both author and committer** — see
   "the release identity is also a guard" below.
3. Replaces the staging tree with `git archive HEAD` of the working repo
   and commits that as a single release commit under the configured
   identity — never the maintainer's real name and email — with the
   previous release as its parent (linear, one commit per release on the
   public side, however many commits it took privately), then cuts an
   **annotated tag** on it named after the version — under that same
   identity, because an annotated tag records a tagger of its own. See
   [Versioning](#versioning) below. Between reading the version and
   committing, it also **refuses to go on if this machine's `backlog` is
   not the version the snapshot says it was tested against** — exit `3`,
   a machine problem, see
   [The tested backlog baseline](#the-tested-backlog-baseline).
4. Runs the full release gate against that staged snapshot before anything
   is pushed — `python3 scripts/scan_release.py` (secrets and personal
   identity, see below), then that a changelog entry for this version
   *exists* (a heading check only; whether the text is complete is the
   [Versioning](#versioning) read, below), then `python3 -m unittest
   discover tests` **with
   `CENTRALE_REQUIRE_NODE=1`** (see [the suite may not skip its way past a
   release](#the-suite-may-not-skip-its-way-past-a-release)), then the
   integration tier — `python3 -m unittest discover tests_integration`,
   **with `CENTRALE_REQUIRE_INTEGRATION=1`**, real `git`, `tmux` and
   server processes in sandboxes, against the same staged tree (same
   section; a snapshot with no `tests_integration/` in it fails here) —
   then `python3 server.py --check`. Any failure aborts with nothing
   pushed, and leaves the staged snapshot on disk (printing its path) so
   the tree that failed can actually be looked at. Budget about a minute
   for the whole gate: the integration tier is roughly half of it.
5. Pushes **fast-forward only** onto the public branch, branch and tag
   together in one `--atomic` push. `--force` never appears anywhere in
   the script.

Usage: `scripts/release.sh [--dry-run] <release message>`. `--dry-run`
builds and gates the commit locally and prints what it would have pushed,
without any network contact at all (clone, fetch, and push are all
skipped). Flags are recognised **before or after** the message, so
`release.sh "ship it" --dry-run` is a dry run rather than a real push of a
message ending in `--dry-run`; `--` ends flag parsing if a message must
itself begin with a dash.

Exit codes are worth reading before wiring this into anything: `0`
released (or dry run, or nothing to release), `1` the release failed
(unconfigured, foreign identity on the remote, gate failure, push
rejected — nothing pushed in any case), `2` usage error, and `3` **this
machine** could not run a release at all — a tool missing from `PATH`, a
temp directory that could not be created, or a `backlog` CLI that is not
the version the snapshot declares it was tested against. A `3` says
nothing about the snapshot: it was never evaluated, and in the version
case it will release unchanged from a machine on the baseline. That
distinction matters because the gate runs the suite and `--check` inside
the snapshot, and `--check` fails without `git`/`backlog` on `PATH` — a
release machine missing a CLI would otherwise look exactly like a broken
snapshot.

**The release machine needs `git`, `tar`, `python3`, `backlog`, `node`
and `tmux`** — all six checked up front, before anything is staged or
cloned, with a missing one reported as exit `3`. The first four are
there because the gate would fail confusingly without them. `node` and
`tmux` are there for the opposite reason, and get their own section next.
`backlog` is also the one tool whose **version** is checked, not just its
presence — that check needs the staged snapshot to read the baseline out
of, so it happens a few steps later, and is the only exit `3` that comes
after staging.

#### The suite may not skip its way past a release

`python3 -m unittest discover tests` reports **OK** when a test tier
skips itself for want of a runtime, and on a development machine that is
exactly right: Centrale's frontend behavioural tier
(`tests/test_frontend_behaviour.py`, over `tests/js_harness.py`) drives
the real `static/*.js` sources under `node`, `node` is optional, and a
machine without it still gets a green suite with that tier counted as
skips. Nothing about that changes — see
[Testing](architecture.md#testing) in the architecture chapter.

For a **release** it is the wrong answer, and was a real hole (task-124):
with `node` absent, the whole behavioural tier vanished into an
`OK (skipped=N)` and the gate passed. The rest of the frontend suite
cannot cover for them, because it does not execute anything — the source-shape contracts
*grep* `static/` for invariants like "the pane fetch has one home", so
they stay green over JavaScript that cannot even be parsed, and
`server.py --check` never loads the frontend at all. A syntax error
inside a file's IIFE was demonstrated to pass the entire gate on a
machine without `node`, and be published.

Two things close it, and both are in `scripts/release.sh`:

- **`node` is a release-machine prerequisite** (exit `3`, above): a
  release machine that cannot run the tier is told so before a snapshot
  is built or a remote is contacted.
- **The gate exports `CENTRALE_REQUIRE_NODE=1`** around the staged
  snapshot's suite run. `tests/js_harness.py` reads it: with it set, a
  missing `node` is a test **failure** instead of a skip, so a tier that
  finds no runtime anyway — for whatever reason the prerequisite check
  did not foresee — fails the release rather than disappearing into an
  OK. Nothing outside a release sets that variable, and a test asserts
  that nothing else in the repo does.

The gate therefore runs the real behavioural suite against the staged
snapshot; it does not count the tests or grep for them.

The integration tier had the same hole one tier over, and task-130
closed it the same way. `tests_integration/` is the only thing that
drives **real** `git`, `tmux` and server processes — spawn, resume,
reconcile, harvest, discard, branch cleanup, the browser launcher, the
server's own lifecycle, and `release.sh` itself — and `discover tests`
never collects it (see its README), so until then a release published
code whose actual subprocess behaviour was last exercised whenever
someone last ran that tier by hand. Now:

- **The gate runs `python3 -m unittest discover tests_integration`
  against the staged snapshot**, after the unit tier and before
  `--check`, and a failure aborts the release exactly as any other gate
  step does. A snapshot with no `tests_integration/` in it fails the
  gate — the same rule as a missing scan: silently publishing without
  it is the outcome the step exists to prevent.
- **`tmux` is a release-machine prerequisite** (exit `3`, above), for
  the reason `node` is: the tier's modules are gated on the tools they
  need and *skip* without them.
- **The gate exports `CENTRALE_REQUIRE_INTEGRATION=1`** around that run.
  `tests_integration/base.py` reads it in `require_tools`: with it set,
  a test whose tool is missing **fails**, naming the tool, instead of
  skipping. Outside a release nothing sets it, so a development machine
  without `tmux` or `backlog` still gets the clean skip the tier's
  README promises — and a test asserts that only `release.sh` sets it.

The tier's own sandboxing is what makes running it inside a release safe
on a maintainer's machine, and it holds from the staging directory as it
holds from a checkout: every path it uses is resolved from its own
location (so the staged snapshot's copy tests the staged snapshot's
code, including the staged `release.sh`), every tmux session lives on a
dedicated `-L centrale-itest-<pid>-<random>` socket of that run's own —
including the socket *file*, which `kill-server` does not remove — torn
down and asserted gone when the tier finishes, every repo is a throwaway
`git init`, and every port is ephemeral — the default tmux server, the live Centrale on 7420
and the working repo are never touched. Nothing in it needs the tree it
runs from to be a checkout with history; the release commit's single
parentless commit is enough. What it costs is time: about 30 seconds on
top of the gate's previous 25, so a release now runs for roughly a
minute before it pushes.

**The release identity is also a guard, not only a label.** Before
building on an existing public history, the script reads every commit on
the release branch and aborts unless the complete identity —
`RELEASE_AUTHOR_NAME <RELEASE_AUTHOR_EMAIL>` — is both its author *and*
its committer. All four fields, checked independently, because an address
alone is not an identity: a commit made as `Your Real Name
<the-configured-release-address>` puts a personal display name into the
public history exactly as permanently as a personal address would (that
gap was real, and is task-123). Emails compare case-insensitively, names
exactly — a display name is not an address, so `release bot` and `Release
Bot` are two different names. The refusal names each offending identity by
role (`author:` / `committer:`) and prints the complete identity it
expected. This exists for one specific, unrecoverable mistake: GitHub's
"Create a new repository" page has **"Add a README file" checked by
default**, and that initial commit is authored
under the account owner's real name and email. Without the check, release
#1 would be built on top of it and pushed cleanly, putting the real
identity permanently at the root of the public history — and since the
script never uses `--force`, re-running could not walk it back. Create the
public repo completely empty (no README, no `.gitignore`, no license); the
check is what enforces that rather than merely advising it.

#### Versioning

There is **one number**, `__version__` in `version.py`, and everything
else is derived from it:

| Derived | How |
|---|---|
| The release tag | `scripts/release.sh` reads `version.py` **out of the staged snapshot** and cuts `v<version>` on the release commit |
| `python3 server.py --check`'s first line | `server.detect_version()` |
| The version in the sidebar footer | the same call, made **once at server start** and then only read back |

`version.py` holds a **second** number beside it, `TESTED_BACKLOG_VERSION`
— the `backlog` CLI this release was verified against — and it works the
same way: declared once in code, and everything that states it derived
from there. See [The tested backlog baseline](#the-tested-backlog-baseline)
below; bumping it is a separate procedure from bumping `__version__`, and
the two are usually done in the same release.

The server layers `git describe --tags --always --dirty` on top of the
constant when the checkout has a `.git`, so a development checkout reports
`v0.1.0-14-g4570911` (a suffix that moves with every commit) while a
released snapshot — whose release commit carries the tag — reports a clean
`v0.1.0`. A downloaded zip has no `.git` and reports the constant alone,
which is exactly right: nothing is generated into the snapshot to cover
that case, and nothing needs to be. What the footer shows is what **this
server process booted from**, never what the checkout has become since —
the first half of the answer to the staleness gap
[Restarting after a change](#restarting-after-a-change) describes. The
second half (task-128) is derived on every board request, not captured:
the server reads the commit back out of that string, runs `git rev-parse
HEAD` in its own checkout, and reports the two as `codeDrift` on `GET
/api/board` when they differ — the banner that section shows, and the
`--check` line that relays it. Nothing about the comparison is remembered
between requests, so it cannot itself go stale.

**To cut a release**, in this order:

1. Bump `__version__` in `version.py`.
2. Add a `## v<version>` section to `CHANGELOG.md` saying what changed.
   Written by hand: the private commit log is not published, so there is
   nothing honest to generate this from.
3. **Read that entry against what actually merged since the previous
   release** — `git log --merges --first-parent <previous tag>..HEAD`
   (or all merges, for a first release) — and add whatever it misses.
   The entry is usually drafted before the last few branches land, and
   the gate cannot judge prose: it checks that the heading *exists*, not
   that the text covers the product. The v0.1.0 entry was drafted
   describing less than the tree it was attached to for exactly that
   reason, and this read is what caught it before the release rather
   than after (task-129), so it is a step, not advice.
4. Commit both — `release.sh` publishes `HEAD`, never uncommitted work.
5. `scripts/release.sh --dry-run "<message>"` to preview, then the same
   without `--dry-run`.

Two of those steps are enforced rather than remembered, so the constant,
the tag and the changelog cannot drift apart:

- **The tag is derived, not maintained.** It is `v` + the constant, read
  from the tree being published. There is no second place to update, so
  there is nothing to disagree with.
- **A version already published is refused.** If the snapshot's content
  differs from the public branch but its tag is already on the remote,
  the release aborts and tells you to bump — releasing would otherwise
  either move a published tag or ship an untagged release. (Unchanged
  content is still the benign "nothing to release" it always was: that
  check runs first.)
- **A missing changelog entry is a gate failure.** No `## v<version>`
  heading in the snapshot's `CHANGELOG.md`, no release. That is the whole
  check — presence of the heading, nothing about what is under it — which
  is why step 3 above is a human read and not a gate.

#### The tested backlog baseline

Centrale is a veneer over the `backlog` CLI: every task, status and
acceptance criterion the board shows came out of that binary. So every
release is implicitly a claim about **which `backlog` it was verified
against**, and that claim used to live only in README prose with nothing
checking it. Upstream makes that untenable rather than merely untidy —
Backlog.md shipped 100 releases in its last 355 days, a median of one day
apart — so the claim is tied to reality mechanically instead
(task-156):

| Where the baseline is stated | What keeps it honest |
|---|---|
| `TESTED_BACKLOG_VERSION` in `version.py` | the source of truth; nothing else declares it |
| `README.md`'s "verified against `backlog` vX.Y.Z" | `tests/test_version.py` asserts it equals the constant |
| This chapter's "verified against CLI vX.Y.Z" (Limitations) | the same test, one document over |
| `python3 server.py --check` | reads the constant and reports the installed version against it |
| The release itself | `scripts/release.sh` **refuses** to cut one on a machine running a different version |

The two ends behave deliberately differently, and the difference is the
whole design:

- **For a user, it is information, never a verdict.** `--check` prints
  the installed version next to the baseline and says plainly when they
  differ. It is always a `[PASS]`: it never fails the check, never warns,
  and nothing refuses to start. Running a newer CLI is the user's call,
  and given upstream's cadence most users are ahead within a fortnight of
  any release — a warning that fires for nearly everyone, nearly always,
  only teaches people to skim past the warnings that mean something. The
  line exists so that if the board ever misreads a task, the version gap
  is already in front of whoever is looking.
- **For a release, it is a refusal.** The release gate reads the
  constant out of the staged snapshot and compares it with the release
  machine's own `backlog --version`. A mismatch aborts with **exit 3** —
  the machine-problem code, alongside a missing tool — because the
  snapshot is fine and will release unchanged from a machine on the
  baseline. Nothing is pushed, and the staging tree is not kept for
  inspection, since there is nothing wrong in it. The point is narrow:
  the snapshot's README and CHANGELOG assert a tested combination, and
  cutting it elsewhere publishes a claim nobody checked.

There is a `TESTED` baseline and deliberately **no `MINIMUM`** beside it.
They answer different questions — "what was this exercised on" versus
"below what does it genuinely not work" — and only the first has an
honest answer today: nothing here has been run against an older CLI to
find the floor, and Backlog.md publishes no minimum of its own. A floor
nobody measured would be exactly the sort of unchecked claim this
constant exists to replace. Add one when someone has a use for it and
has measured it.

**To bump the baseline** (usually in the same release as a version bump,
and it is a procedure rather than an edit):

1. Install the new CLI on this machine: `npm i -g backlog.md@<version>`.
2. Change `TESTED_BACKLOG_VERSION` in `version.py`. That is the only
   place the number is written down.
3. Update the README's "verified against `backlog` vX.Y.Z" and this
   chapter's "verified against CLI vX.Y.Z" in
   [Limitations](#limitations--out-of-scope-for-v1). The suite fails
   until both match the constant, so this is enforced, not remembered.
4. Run the suite — `python3 -m unittest discover tests` — and the
   integration tier, both now talking to the new CLI.
5. Do a real spawn and a real merge from the board, plus the
   [Manual smoke test](#manual-smoke-test). The suite mocks every
   `backlog` call by design (see [Tests](#tests)), so it can tell you
   the contract still parses but not that the CLI still behaves; this
   step is the only thing that exercises the actual binary end to end,
   and it is what the word "tested" in the constant's name is claiming.
6. Say so in the `CHANGELOG.md` entry for the release. Older entries
   naming older baselines are correct history and are left alone.

#### The secret and identity scan

Publication is irreversible, and this project has missed the same class of
problem twice by hand — a private repo's live captures in `tests/fixtures/`
and its name in the Settings placeholder both survived two manual sweeps
(task-48, task-49) before task-100 caught them. `scripts/scan_release.py`
replaces the manual pass with a check that runs every time. It reads the
**staged snapshot**, not the working tree, and aborts the release on:

- **credential shapes** — PEM private-key headers, `sk-` / `ghp_` /
  `github_pat_` / `AKIA` / `xox?-` prefixes, `Authorization: Bearer` with a
  real token, and key/secret/password assignments that carry a *value*
  rather than name a field;
- **personal identity** — home-directory paths (`/home/<account>`,
  `/Users/<account>`), anything shaped like a real email address, and the
  login name plus `git config user.name` / `user.email` of the machine the
  snapshot is built on;
- **private project names** — the repos on your dashboard whose names must
  not appear in public.

Run it yourself, from a clone, with nothing installed and nothing
configured — it is the same check the release runs:

```bash
python3 scripts/scan_release.py          # this repo
python3 scripts/scan_release.py <dir>    # any extracted snapshot
```

Exit status is the interface: 0 clean, 1 findings, 2 misuse, 3 unarmed —
the scan resolved no private-name configuration at all, so it never
checked for names and refuses to call the tree clean (see below).

Two things are configurable, both in one place each:

- **Private project names** live in `RELEASE_PRIVATE_NAMES` in
  `.release-remote` (space- or comma-separated), next to the rest of the
  release config, or in an environment variable of the same name.
  `release.sh` exports it into the gate, because `.release-remote` is
  gitignored and so never reaches the snapshot. **Add a repo to the
  dashboard, add its name here.** The list is deliberately *not* committed:
  a tracked file listing your private repo names would publish exactly what
  it exists to withhold. For the same reason the script derives personal
  identity from the machine at runtime instead of hardcoding it — which
  also means a stranger who clones the snapshot gets a check that protects
  *their* identity.

  **Where the list is looked for, in order** (task-166): the
  `RELEASE_PRIVATE_NAMES` environment variable, which always wins; then
  `.release-remote` in the directory being scanned; then in the **main
  working tree** of that directory's repository, if it is a linked
  worktree; then next to the script's own repository root; then in the
  main working tree of *that* repository. The worktree steps are the
  point of the ordering. `.release-remote` is gitignored, so it exists in
  exactly one checkout — and every line of code here is written inside a
  `git worktree add` spawn worktree, which has none. Before task-166 the
  private-name rules were therefore disarmed in the only place they could
  ever have fired, and armed only in the main checkout, where the work
  has already landed: task-157 merged 26 private names into two shipping
  test files with `python3 -m unittest discover tests` green in the
  worktree that wrote them. A linked worktree shares its repository's
  common git dir (`git rev-parse --git-common-dir`), whose parent is the
  main working tree — where the file actually is.

  **An unarmed scan is not a clean scan.** With no list resolved from any
  of those places, the private-name rules have nothing to match and
  cannot fail, and "found nothing" and "looked for nothing" printed the
  same verdict and the same exit `0` until task-166. A run that could not
  be armed now reports `UNARMED` and exits `3`, and `release.sh` fails
  the gate on it with a message that says the scan could not be armed
  rather than that the snapshot is dirty. If a tree genuinely has no
  private names to check for, **say so** — an empty value is the
  declaration, and arms the scan with nothing to match:

  ```bash
  RELEASE_PRIVATE_NAMES="" python3 scripts/scan_release.py
  ```

- **Known-good placeholders** are allowlisted in the `ALLOWLIST` table in
  the script, where every entry carries the reason it is safe as a required
  field. Today that is the literal `/home/user` used in the API examples,
  the RFC 2606 reserved `example.com`/`.net`/`.org` domains, the reserved
  `.invalid`/`.test`/`.example`/`.localhost` TLDs the integration tier
  builds its throwaway release identities under, and the `git@github.com`
  SSH URL form.

What it enumerates is what ships: tracked content **minus** anything
`export-ignore` keeps out of `git archive`. So `backlog/` is not scanned
in a checkout at all — it is not published, and a finding in a file that
cannot reach the public repo is noise. The scan keeps one identity
exemption for `backlog/` behind that, for the case the filtering cannot
reach: an unpacked directory that is not a git checkout, where
`.gitattributes` is unreadable and a development tree's board could still
be handed to the scan by hand. The credential rules run there without
exception, and a run that skips anything prints it rather than skipping in
silence.

The unit suite runs the scan over the tracked tree
(`tests/test_scan_release.py`), so a reintroduced name or path fails at
test time rather than at release time — which is exactly how the last one
got in. That guard is only worth having where the rules are armed, so in
a tree that carries a `backlog/` board — a development checkout, main or
spawn worktree; the board is where private project names come from, and
the published snapshot never has one — the suite requires a clean **and
armed** run, and separately asserts that the list resolves from disk with
nothing in the environment. In a clone of the snapshot there is no board
and no list to find, so an `UNARMED` verdict passes there while a real
finding still fails.

**Why an accidental mispush is structurally hard, not just discouraged:**
the public remote's history and the private working repo's history share no
common ancestor — the public side is an entirely separate, linear chain of
release commits. If a plain `git push <public-remote> main` were ever run
from the working repo by mistake (however that remote got configured), git
itself refuses it as a non-fast-forward push: there is no fast-forward path
between two histories with no shared ancestor. That refusal comes from the
actual git object graph, not from anyone remembering a rule. The one
override is `--force`, which this script never uses and which a human
would have to type deliberately. Enabling **branch protection ("do not
allow force pushes") on the public repo itself**, on GitHub, closes that
last remaining deliberate-override path — done once, outside this script,
in the public repo's settings.

`tests_integration/test_release_integration.py` exercises the real script
against local bare "scratch remotes" — the foreign-identity refusal, the
misordered `--dry-run`, the config precedence, a gate failure keeping its
evidence, and the happy path of a first and second release. It lives in
the integration tier rather than `tests/` on purpose: the release gate
runs `tests/` *inside the snapshot*, so a release-script test there would
be run by every release.

For an extra local layer, a `pre-push` hook in the private working repo can
refuse any push whose target remote matches `.release-remote`'s
`RELEASE_REMOTE`, so even a manual push from there is blocked at the source
regardless of how the remote got added. This is optional and not installed
automatically — the copy-paste recipe lives in `scripts/release.sh`'s own
header comment (`.git/hooks/pre-push`, executable).
