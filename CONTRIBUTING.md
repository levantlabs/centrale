# Contributing to Centrale

Pull requests and issues are welcome. Before you spend a weekend on one,
read the next section: this repository accepts contributions through a
route that is not the usual one, and you should know that going in rather
than discovering it when your PR closes without a merge commit.

## How a pull request actually reaches the code

Centrale is developed in a **private repository** and published here as a
curated snapshot — one squashed commit per release, built from `git archive
HEAD` of that private repo. The two repositories share no common ancestor,
so a PR opened here cannot be merged and then appear upstream: there is no
upstream branch for it to merge into.

What happens instead, in full:

1. You open a PR against this repository. It is reviewed here, in the open,
   like any other PR — comments, requested changes, the usual.
2. When it is accepted, the maintainer **cherry-picks** it into the private
   development repository.
3. It ships to this repository in the **next release snapshot**, folded into
   that release's single commit along with everything else that changed.

So your PR will not show as *merged* in GitHub's green sense. It will be
closed with a note saying it was taken, and the code will land here at the
next release. That is the whole mechanism; there is no second, better path
that some contributors get.

**Credit.** Because the release commit is authored under the project
identity, GitHub's contributor graph will not show your name. Credit is
given in [CHANGELOG.md](CHANGELOG.md): the entry for the release that
carries your change names you and links the PR. If you would rather not be
named there, say so in the PR and you won't be. Cherry-picked commits also
keep your authorship in the private history, but that history is not
published, so the changelog is the credit that anyone can actually see —
which is why it is the mechanism rather than a courtesy.

**Why the history here looks like that.** A clone of this repository has
one commit per release — exactly one at v0.1.0, and one more each time a
release is cut — and several hundred files, each of those commits a whole
release authored by the project identity rather than by a person. That is
not a squashed import or a lost history: development happens in a private
repo whose commit log — real names, machine paths, a task-by-task build
log written for the maintainer — is deliberately not published. The published
tree is exactly the tracked content of one private commit, minus what
`.gitattributes` marks `export-ignore`. See
[Releasing](docs/operations.md#releasing) for the procedure and its
guarantees.

**The intention, stated as an intention.** The maintainer intends to move
development into this public repository once Centrale is stable enough for
that, at which point ordinary PRs merge ordinarily and this section goes
away. That is a direction of travel, not a commitment or a date — plan your
contribution on the process described above, which is the one in force.

## What to reference in a PR

Centrale tracks its own work in a Backlog.md board that does not ship: the
`backlog/` directory is `export-ignore`d out of every snapshot, because it
is a maintainer's working record full of local paths and cross-repo
references. So, unlike the project it builds on, this one cannot ask you to
reference a task id.

Reference instead:

- **the issue number**, if there is one — open an issue first for anything
  beyond a small fix, so the design conversation happens before the work;
- **the docs chapter** your change touches (`docs/merging.md`,
  `docs/api.md`, …), so a reviewer knows what has to stay in step;
- **what you actually verified**: which tests you ran, and whether the
  frontend and integration tiers ran or skipped on your machine (see
  below — a skip is not a failure, but it is worth saying).

You may also see `task-NN` citations in comments and docs throughout the
tree. They tag the board above, so you cannot open them. Nothing is missing
when you can't: each tags a sentence that already carries its reason inline.
[AGENTS.md](AGENTS.md) explains this at more length, and asks you to add new
ones the same way — state the reason, then tag it.

## Running the tests

```bash
python3 -m unittest discover tests
```

That is the whole default suite, and it needs no network, no tmux server,
no real repositories and no coding agent: every subprocess boundary is a
small patchable function the tests mock. Run it before opening a PR.

Two tiers inside and beside it need a runtime you may not have:

- **The frontend behavioural tier needs `node`.** `tests/test_frontend_behaviour.py`
  drives the real `static/*.js` sources under `node` over a small DOM shim.
  Without `node` that module **skips silently** and the suite still reports
  `OK` — so a green run on a machine without `node` has not exercised the
  frontend at all. Install `node` if you are changing anything under
  `static/`, or set `CENTRALE_REQUIRE_NODE=1` to turn that skip into a
  failure and be sure. (Releases always set it; see
  [the suite may not skip its way past a release](docs/operations.md#the-suite-may-not-skip-its-way-past-a-release).)
- **The integration tier needs `tmux`** (plus `git`, `tar` and the `backlog`
  CLI), and is opt-in:

  ```bash
  python3 -m unittest discover tests_integration
  ```

  It runs real `git`, `tmux` and server processes inside sandboxes —
  throwaway repos, a dedicated tmux socket, ports chosen by binding to `0`,
  an isolated `XDG_CACHE_HOME` — so it never touches your repos, your tmux
  server or a running Centrale. A module whose tools are missing skips
  cleanly; `CENTRALE_REQUIRE_INTEGRATION=1` makes that a failure instead.
  See [tests_integration/README.md](tests_integration/README.md) and
  [The integration tier](docs/operations.md#the-integration-tier).

`python3 server.py --check` is the other thing worth running: it reports one
`[PASS]`/`[WARN]`/`[FAIL]` line per prerequisite and starts nothing.

## House rules a review will apply

These are the ground rules from [AGENTS.md](AGENTS.md), which is the
canonical orientation for this repo and worth reading in full alongside
[docs/architecture.md](docs/architecture.md). If your change is a design
decision rather than a fix, read [MANIFESTO.md](MANIFESTO.md) as well: it is
the positions this project holds and the incident behind each, and a change
that contradicts one is a conversation rather than a patch. Briefly, so a
review is not a surprise:

- **Python 3.12, standard library only.** No dependencies, no `pip install`,
  no virtualenv. A PR that adds a package is a design conversation, not a
  patch. The one documented exception is maintainer-only tooling for
  `scripts/screenshots.py`; see
  [docs/operations.md](docs/operations.md#regenerating-the-documentation-screenshots).
- **No build step, no bundler, no modules on the frontend.** It is
  `static/index.html` plus one plain `<script src>` per concern, loaded in a
  fixed order, sharing exactly one seam: the `window.Centrale` namespace
  object documented at the top of `static/state.js`. →
  [The frontend files](docs/architecture.md#the-frontend-files)
- **All task data goes through the `backlog` CLI.** Centrale stores no task
  state of its own and never parses `backlog/tasks/*.md`; lifecycle state is
  re-derived from backlog + git + tmux on every read.
- **Every subprocess call goes through an injectable boundary**
  (`run_git`, `run_tmux`, `run_backlog`, …), so tests stay hermetic. No test
  in `tests/` may launch a real agent, tmux session, browser or merge.
- **Behaviour tests over source-text assertions.** A claim about what the
  code *does* belongs in `tests/test_frontend_behaviour.py`, driven under
  `node`; only a claim about the *shape* of the source — one home for an
  endpoint, a file's load order — stays a grep. The source-text tier has a
  ceiling that fails the suite when it grows. →
  [Testing](docs/architecture.md#testing)
- **Any new resource outside the repo** — a cache file, port, socket or temp
  path — must be overridable by env or config, documented, and called out in
  the PR description.
- **Docs are part of the change.** The chapters under `docs/` are the manual;
  if your change alters behaviour, update the chapter that describes it. New
  `tests/test_*.py` modules must also be listed in the
  [module layout](docs/architecture.md#module-layout) table — the suite fails
  until they are.

Match the surrounding code. There is no formatter and no linter to argue
with; the existing style is the style.

## Issues

Issues are welcome, including "this is confusing" — the documentation is
part of the project, and a chapter that failed to explain something is a
bug in it.

A good issue contains:

- **what you did, what happened, and what you expected**, concretely enough
  to follow;
- **the version line from `python3 server.py --check`** (`[PASS] Centrale
  v0.1.0`), plus anything else it flagged;
- **your OS**, and the versions of `backlog`, `git`, `tmux` and `python3`
  that are relevant — Centrale is developed on Linux and supported on macOS
  best-effort; Windows is not supported;
- **the relevant part of the server's terminal output**, if it produced any.
  Centrale never returns a traceback to the browser, so the terminal is
  where errors go.

Please redact your own repository paths and project names if they are
sensitive — a reproduction rarely needs the real ones.

Before filing, it is worth checking
[Limitations](docs/operations.md#limitations--out-of-scope-for-v1): a few
behaviours that look like bugs are deliberate for now, and are listed there
with the reason.

## License

Centrale is MIT-licensed (see [LICENSE](LICENSE)). By contributing, you
agree that your contribution is licensed under the same terms. There is no
CLA and nothing to sign.
