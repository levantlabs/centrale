#!/usr/bin/env bash
# release.sh — publish a curated snapshot of this repo to a public remote,
# while making an accidental push of the PRIVATE repo (full history, real
# personal commit identity) structurally hard to do by mistake.
#
# THE MODEL
#   This script never configures a remote in the working repo you're
#   reading this from -- a plain `git push` here has nowhere dangerous to
#   go, ever. Instead, every release:
#     1. Reads the public remote URL + a clean release identity from
#        environment variables, or from a local, gitignored, untracked
#        file (.release-remote next to this repo's root) -- see "CONFIG"
#        below.
#     2. Builds the snapshot in a throwaway TEMP staging clone/checkout of
#        the PUBLIC remote -- never touches your real working tree's own
#        git state.
#     3. Replaces the staging tree with `git archive HEAD` of THIS repo:
#        tracked content at the exact state of your last commit, minus
#        anything marked `export-ignore` in .gitattributes (backlog/, this
#        repo's own private board). Untracked stray files and anything
#        gitignored (projects.json, .centrale-worktrees/, ...) cannot
#        ride along even by accident, and
#        neither can an export-ignored path -- see .gitattributes for what
#        is excluded and why, and tests/test_release_snapshot.py, which
#        the release gate in step 5 runs against the real archive listing.
#     4. Commits that tree as a single release commit, authored under the
#        clean configured identity (never your real git identity), with
#        the previous release as its parent (or no parent, for the very
#        first release) -- one linear commit per release on the public
#        side, regardless of how many commits it took privately, and
#        cuts an ANNOTATED tag on it named after the version (see
#        "VERSIONING" below), under that same identity.
#     5. Runs the release gate against the STAGED snapshot before
#        anything is pushed -- scripts/scan_release.py (secrets and
#        personal identity), then that CHANGELOG.md HAS a "## v<version>"
#        heading (presence only: the gate cannot judge whether the text
#        under it describes what is shipping -- that read is a step in
#        docs/operations.md, "Versioning"), then the full unit suite
#        (`python3 -m unittest discover tests`), then the integration
#        tier (`python3 -m unittest discover tests_integration` -- real
#        git, tmux and server processes in sandboxes), then `--check`;
#        any failure aborts with nothing pushed. Both tiers run with
#        CENTRALE_REQUIRE_NODE=1 and CENTRALE_REQUIRE_INTEGRATION=1
#        exported, so a test that would SKIP for want of its runtime or
#        tools fails the gate instead -- see "THE SUITE MAY NOT SKIP ITS
#        WAY PAST A RELEASE" below.
#     6. Pushes fast-forward only -- never --force -- onto the public
#        branch, with the tag, atomically.
#
# VERSIONING
#   There is one number, in version.py's `__version__`, and everything
#   else is derived from it: the tag this script cuts is "v<version>"
#   read out of the STAGED SNAPSHOT (so it names what is actually being
#   published), and the release refuses to go out if
#     * that tag is already on the public remote while the content
#       differs -- releasing would either move a published tag or ship
#       an untagged release, so bump version.py instead; or
#     * CHANGELOG.md has no "## v<version>" section -- a release nobody
#       wrote a line about. The heading is all that is checked; an entry
#       that exists but says too little passes, so read it against the
#       merges since the previous release before running this (see
#       docs/operations.md, "Versioning").
#   Unchanged content is still the benign "nothing to release" it always
#   was: that check runs first, so it is never reported as a collision.
#   See docs/operations.md, "Versioning", for the bump procedure.
#
# THE RELEASE MACHINE'S BACKLOG MUST BE THE TESTED BASELINE
#   Centrale is a veneer over the `backlog` CLI, so every release is
#   implicitly a claim about which one it was verified against -- the
#   snapshot's README says so in prose, and version.py's
#   TESTED_BACKLOG_VERSION says so in code. This script reads that
#   constant out of the STAGED SNAPSHOT (the same way it reads the
#   version) and refuses to release when this machine's `backlog
#   --version` is a different number: the snapshot would otherwise
#   publish a tested-against claim nothing here ever exercised, and
#   upstream ships a minor every few days, so a release machine drifts
#   off the baseline without anyone doing anything. That refusal is a
#   fact about the MACHINE (exit 3), not about the snapshot, which will
#   release unchanged from a machine on the baseline (task-156).
#
# THE SUITE MAY NOT SKIP ITS WAY PAST A RELEASE
#   `python3 -m unittest discover tests` reports OK when a test tier
#   skips itself for want of a runtime, and that is the right behaviour
#   on a development machine -- Centrale's frontend behavioural tier
#   (tests/js_harness.py, tests/test_frontend_behaviour.py) drives the
#   real static/*.js sources under `node`, and `node` is optional there.
#   It is the wrong behaviour for a release: the remaining frontend
#   tests only GREP the sources, so nothing would have parsed, let alone
#   executed, the JavaScript in the snapshot being published. A frontend
#   broken badly enough not to parse passed this gate before task-124.
#   Two things close that: `node` is a prerequisite of the release
#   MACHINE below (exit 3, before anything is staged), and the gate
#   exports CENTRALE_REQUIRE_NODE=1 so that a tier which nonetheless
#   finds no runtime FAILS rather than skips. Nothing outside a release
#   sets that variable.
#
#   The integration tier (tests_integration/) had the same hole one tier
#   over, and closes it the same way (task-130). That tier is the only
#   thing that drives REAL git, tmux and the server process -- spawn,
#   resume, reconcile, harvest, discard, cleanup, the browser launcher,
#   the server's lifecycle, and this script itself -- and `discover
#   tests` never collects it, so before task-130 a release published
#   code whose actual subprocess behaviour was last exercised whenever
#   someone last ran the tier by hand. Now the gate runs it against the
#   staged snapshot after the unit tier, `tmux` is a prerequisite of the
#   release MACHINE (exit 3), and CENTRALE_REQUIRE_INTEGRATION=1 makes a
#   test that would skip for want of a tool (tests_integration/base.py's
#   `require_tools`) fail with the missing tool named. The tier's own
#   sandboxing is what makes this safe on a maintainer's machine: it
#   talks to a tmux socket of its own (`-L centrale-itest-<pid>-<rand>`,
#   task-161 -- so a release cannot be broken by, or break, an
#   integration run happening at the same time), never the default
#   server, uses throwaway repos and ephemeral ports, and
#   resolves every path from its own location, so it runs from the
#   staging directory exactly as it runs from a checkout. A snapshot with
#   no tests_integration/ in it fails the gate, for the same reason a
#   missing scan does. Budget roughly half a minute more per release.
#
# WHY THIS MAKES A MISPUSH STRUCTURALLY HARD, NOT JUST DISCOURAGED
#   The public remote's history and this repo's own private history share
#   NO common ancestor -- the public side is an entirely separate, linear
#   chain of release commits. If you (or a script, or a habit) ever ran a
#   plain `git push <public-remote> main` from THIS repo by mistake, git
#   itself would refuse it as a non-fast-forward push: the private
#   history doesn't build on top of the public release chain, so there is
#   no fast-forward path between them. That refusal is automatic and
#   requires no discipline to remember -- it's the actual git object
#   graph, not a policy. The one way to override it is `--force`, which
#   this script never uses and which a human would have to type
#   deliberately and locally. Enabling branch protection ("Do not allow
#   force pushes") on the PUBLIC repo on GitHub closes that last
#   deliberate-override path entirely -- do this once, on the public repo
#   itself, outside this script.
#
# THE RELEASE IDENTITY IS ALSO A GUARD, NOT ONLY A LABEL
#   Before building on top of an existing public history, this script
#   reads every commit already on the release branch and refuses to
#   continue unless the COMPLETE identity -- name AND email -- of both
#   its author and its committer is RELEASE_AUTHOR_NAME
#   <RELEASE_AUTHOR_EMAIL>. All four fields, because an address alone is
#   not an identity: a commit made as "Your Real Name
#   <the-configured-release-address>" publishes a personal display name
#   just as permanently as a personal address would. Emails match
#   case-insensitively; names match exactly, since a display name is not
#   an address. That is what stops the one mistake this whole design
#   cannot undo afterwards: GitHub's "Create a new
#   repository" page has "Add a README file" CHECKED BY DEFAULT, and that
#   initial commit is authored under your real name and email. Without
#   the check, release #1 would be built on top of it and pushed cleanly,
#   putting your real identity permanently at the root of the public
#   history -- and since this script never uses --force, re-running could
#   not walk it back. Create the public repo EMPTY (no README, no
#   .gitignore, no license); this check is what enforces that rather than
#   merely advising it.
#
# OPTIONAL: A LOCAL PRE-PUSH HOOK
#   For extra safety, you can install a pre-push hook in THIS (private)
#   repo that refuses any push whose target remote matches the URL in
#   .release-remote, so even a manual `git push <public-remote>` from
#   here (however it got configured) is blocked at the source. This is
#   optional and not installed by this script -- copy the recipe below
#   into .git/hooks/pre-push in this repo and `chmod +x` it:
#
#     #!/usr/bin/env bash
#     # Refuses any push whose remote URL matches .release-remote's
#     # RELEASE_REMOTE -- belt-and-suspenders alongside release.sh's own
#     # unrelated-history protection.
#     remote_url="$2"
#     config_file="$(git rev-parse --show-toplevel)/.release-remote"
#     if [ -f "$config_file" ]; then
#       # shellcheck disable=SC1090
#       . "$config_file"
#       if [ -n "${RELEASE_REMOTE:-}" ] && [ "$remote_url" = "$RELEASE_REMOTE" ]; then
#         echo "pre-push: refusing a direct push to the release remote -- use scripts/release.sh instead." >&2
#         exit 1
#       fi
#     fi
#     exit 0
#
# CONFIG (.release-remote, next to this repo's root -- gitignored, never
# committed; sourced as shell, so treat it as trusted local config you
# wrote yourself, same as any other dotfile):
#
#   RELEASE_REMOTE="git@github.com:you/your-public-repo.git"
#   RELEASE_AUTHOR_NAME="Your Release Name"
#   RELEASE_AUTHOR_EMAIL="noreply@example.com"
#   # RELEASE_BRANCH="main"   # optional, defaults to main
#   RELEASE_PRIVATE_NAMES="acme-internal skunkworks"   # "" if none
#
# RELEASE_PRIVATE_NAMES is the one place to list repos whose NAMES must
# never appear in a published snapshot -- add a repo to the dashboard,
# add its name here. scripts/scan_release.py reads it (see that script's
# header for why the list lives in this untracked file rather than in
# tracked source), and this script exports it into the release gate.
# It is not optional: leaving it unset makes the scan refuse to report a
# clean snapshot (exit 3), because nothing would have checked for names
# at all. An empty value is the way to say there are none.
#
# Environment variables of the same names take PRECEDENCE over the config
# file (and can substitute for it entirely): a value exported in the
# environment always wins over the same name in .release-remote, so a
# one-off `RELEASE_REMOTE=... scripts/release.sh ...` does what it looks
# like it does even when the file exists.
#
# USAGE
#   scripts/release.sh [--dry-run] <release message>
#
#   Flags are recognised before OR after the message -- `release.sh "ship
#   it" --dry-run` is a dry run, not a real push of a message that
#   happens to end in "--dry-run". Use `--` to end flag parsing if a
#   message must literally begin with a dash.
#
#   --dry-run   builds and gates the snapshot commit locally only -- never
#               clones, fetches, or pushes anything over the network --
#               and prints what it would have pushed. Since it never
#               contacts the real remote, it always previews as if this
#               were a first release (no prior-release parent commit);
#               it cannot know the real remote's current state.
#
# EXIT CODES
#   0  released (or dry run completed, or nothing to release)
#   1  the release itself failed: not configured, the remote carries a
#      foreign identity, the version's tag is already published, the
#      release gate failed against the snapshot, or the push was
#      rejected. Nothing was pushed.
#   2  usage error (bad flag, missing message).
#   3  this MACHINE could not run a release: a missing tool on PATH, a
#      temp directory that could not be created, or a `backlog` CLI whose
#      version is not the one the snapshot says it was tested against
#      (see "THE RELEASE MACHINE'S BACKLOG" below). Says nothing about
#      the snapshot -- it was never evaluated, and in the version case it
#      will release unchanged from a machine on the baseline.
#
# bash + git + tar + python3 only. No new Python code. (`backlog`, `node`
# and `tmux` are required on the release MACHINE, for the gate's
# `--check`, its frontend behavioural tier and its integration tier. Of
# the three, only `backlog` is called from this script directly, and
# only for `--version`.)

# `set -e` is load bearing here, not decoration: every step between
# emptying the staging tree and committing it (the rm, the archive, the
# extraction, `git add`, `git commit`) used to run unchecked, so a
# partially built tree could still reach a commit and a push.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
CONFIG_FILE="$REPO_ROOT/.release-remote"

# Exit code for "this machine can't run a release", kept distinct from a
# failure of the release itself -- see EXIT CODES in the header.
EXIT_ENV=3

usage() {
  echo "usage: $0 [--dry-run] <release message>" >&2
  echo "       flags may appear before or after the message; '--' ends flag parsing" >&2
}

# --- arguments ----------------------------------------------------------
#
# Flags are recognised wherever they appear. The old parser stopped at the
# first non-flag argument and swallowed the rest as the message, so
# `release.sh "ship it" --dry-run` set MESSAGE="ship it --dry-run" with
# DRY_RUN=0 and performed a real push -- a typo in argument order turning
# a preview into a publication is the wrong failure direction for this
# script.

DRY_RUN=0
MESSAGE_PARTS=()
END_OF_FLAGS=0
while [ $# -gt 0 ]; do
  if [ "$END_OF_FLAGS" -eq 0 ]; then
    case "$1" in
      --dry-run)
        DRY_RUN=1
        shift
        continue
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --)
        END_OF_FLAGS=1
        shift
        continue
        ;;
      -*)
        echo "release.sh: unknown option: $1" >&2
        usage
        exit 2
        ;;
    esac
  fi
  MESSAGE_PARTS+=("$1")
  shift
done

MESSAGE=""
if [ "${#MESSAGE_PARTS[@]}" -gt 0 ]; then
  MESSAGE="${MESSAGE_PARTS[*]}"
fi

if [ -z "$MESSAGE" ]; then
  echo "release.sh: missing release message" >&2
  usage
  exit 2
fi

# --- load config --------------------------------------------------------
#
# Environment wins over the file. .release-remote is sourced as plain
# shell assignments, so sourcing it last would silently override an
# exported value -- the opposite of what this script's own usage text and
# docs/operations.md promise. Remember what the environment said, source
# the file, then put the environment's values back.

ENV_RELEASE_REMOTE="${RELEASE_REMOTE:-}"
ENV_RELEASE_AUTHOR_NAME="${RELEASE_AUTHOR_NAME:-}"
ENV_RELEASE_AUTHOR_EMAIL="${RELEASE_AUTHOR_EMAIL:-}"
ENV_RELEASE_BRANCH="${RELEASE_BRANCH:-}"
# Empty is a MEANINGFUL value for this one -- "there are no private
# names" is a declaration, not an absence (task-166) -- so remember
# whether it was set at all, not merely whether it was non-empty.
ENV_RELEASE_PRIVATE_NAMES_SET="${RELEASE_PRIVATE_NAMES+set}"
ENV_RELEASE_PRIVATE_NAMES="${RELEASE_PRIVATE_NAMES:-}"

if [ -f "$CONFIG_FILE" ]; then
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
fi

[ -n "$ENV_RELEASE_REMOTE" ] && RELEASE_REMOTE="$ENV_RELEASE_REMOTE"
[ -n "$ENV_RELEASE_AUTHOR_NAME" ] && RELEASE_AUTHOR_NAME="$ENV_RELEASE_AUTHOR_NAME"
[ -n "$ENV_RELEASE_AUTHOR_EMAIL" ] && RELEASE_AUTHOR_EMAIL="$ENV_RELEASE_AUTHOR_EMAIL"
[ -n "$ENV_RELEASE_BRANCH" ] && RELEASE_BRANCH="$ENV_RELEASE_BRANCH"
[ -n "$ENV_RELEASE_PRIVATE_NAMES_SET" ] && RELEASE_PRIVATE_NAMES="$ENV_RELEASE_PRIVATE_NAMES"

RELEASE_BRANCH="${RELEASE_BRANCH:-main}"

# The snapshot never contains .release-remote (it is gitignored, which is
# the point), so the private-name list can only reach the scan through
# the environment. Exported whenever it is SET, empty included: an empty
# value is the explicit "this tree has no private names" declaration that
# arms the scan with nothing to match, and dropping it here would leave
# the gate's scan unarmed -- which since task-166 is a refusal (exit 3),
# not a quiet pass.
if [ -n "${RELEASE_PRIVATE_NAMES+set}" ]; then
  export RELEASE_PRIVATE_NAMES
fi

if [ -z "${RELEASE_REMOTE:-}" ] || [ -z "${RELEASE_AUTHOR_NAME:-}" ] || [ -z "${RELEASE_AUTHOR_EMAIL:-}" ]; then
  cat >&2 <<EOF
release.sh: release not configured (RELEASE_REMOTE / RELEASE_AUTHOR_NAME /
RELEASE_AUTHOR_EMAIL must all be set).

Create $CONFIG_FILE (gitignored, never committed) with:

  RELEASE_REMOTE="git@github.com:you/your-public-repo.git"
  RELEASE_AUTHOR_NAME="Your Release Name"
  RELEASE_AUTHOR_EMAIL="noreply@example.com"
  RELEASE_PRIVATE_NAMES=""   # names that must never be published; "" if none
  # RELEASE_BRANCH="main"    # optional, defaults to main

...or export the same names as environment variables instead (those take
precedence over the file). This is required even for --dry-run, so the
preview reflects what a real release would actually use.
EOF
  exit 1
fi

# --- this machine's own prerequisites -----------------------------------
#
# The release gate below runs the suite and `server.py --check` inside the
# staged snapshot, and both fail if a tool they need is missing from THIS
# machine's PATH -- which says nothing about the snapshot. Check for them
# up front so that failure is reported as what it is (and with a distinct
# exit code) instead of arriving later dressed as a bad snapshot.
#
# `node` is here for the opposite reason: without it the suite does not
# fail, it SKIPS its frontend behavioural tier and still reports OK. A
# release machine that never noticed would publish a frontend nothing
# had run -- see "THE SUITE MAY NOT SKIP ITS WAY PAST A RELEASE" in the
# header. Missing it is a fact about this machine, so it belongs here
# rather than in the gate. `tmux` is here for the same reason (task-130):
# the integration tier the gate runs is built on a real tmux server on a
# dedicated socket, and its tool-gated modules SKIP without one.

MISSING_TOOLS=()
for tool in git tar python3 backlog node tmux; do
  command -v "$tool" >/dev/null 2>&1 || MISSING_TOOLS+=("$tool")
done
if [ "${#MISSING_TOOLS[@]}" -gt 0 ]; then
  echo "release.sh: cannot run a release on THIS MACHINE -- missing on PATH: ${MISSING_TOOLS[*]}" >&2
  echo "  This is a problem with the release machine, not with the snapshot: the" >&2
  echo "  snapshot was never built or evaluated, and nothing was pushed. Install" >&2
  echo "  the tool(s) above and re-run. ('backlog' is needed because the release" >&2
  echo "  gate runs 'server.py --check', which requires it; 'node' because the" >&2
  echo "  gate runs the frontend behavioural test tier, which drives the snapshot's" >&2
  echo "  own JavaScript under node -- without it that tier SKIPS, the suite still" >&2
  echo "  says OK, and a frontend nothing ever executed gets published; 'tmux'" >&2
  echo "  because the gate runs the integration tier, which drives real git, tmux" >&2
  echo "  and server processes on a dedicated tmux socket -- without it those" >&2
  echo "  modules SKIP the same way.)" >&2
  exit "$EXIT_ENV"
fi

if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
  echo "release.sh: note -- the working tree has uncommitted changes; the release always reflects HEAD (the last commit) only, never uncommitted work." >&2
fi

# --- build the staging copy ---------------------------------------------

if ! STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/centrale-release.XXXXXX")" || [ -z "$STAGING_DIR" ]; then
  echo "release.sh: could not create a staging directory under ${TMPDIR:-/tmp}" >&2
  echo "  This is a problem with the release machine, not with the snapshot." >&2
  exit "$EXIT_ENV"
fi
if ! ARCHIVE_TAR="$(mktemp "${TMPDIR:-/tmp}/centrale-release-archive.XXXXXX.tar")" || [ -z "$ARCHIVE_TAR" ]; then
  rm -rf "$STAGING_DIR"
  echo "release.sh: could not create a temp file under ${TMPDIR:-/tmp}" >&2
  echo "  This is a problem with the release machine, not with the snapshot." >&2
  exit "$EXIT_ENV"
fi

# A gate failure is the one outcome where the staging tree is the
# evidence: it is the exact snapshot that failed, and deleting it on the
# way out leaves nothing to debug. KEEP_STAGING is set only there.
KEEP_STAGING=0
cleanup() {
  rm -f "$ARCHIVE_TAR"
  if [ "$KEEP_STAGING" -eq 1 ]; then
    echo "release.sh: the staged snapshot has been left in place for inspection:" >&2
    echo "  $STAGING_DIR" >&2
    echo "  (delete it yourself when you're done: rm -rf '$STAGING_DIR')" >&2
  else
    rm -rf "$STAGING_DIR"
  fi
}
trap cleanup EXIT
# Ctrl-C or a TERM mid-gate should still clean up: both exit, which runs
# the EXIT trap above. (A SIGPIPE from a truncated `| head` cannot be
# caught this way and leaves the staging dir behind -- harmless, and it
# prints its own path anyway when that's the failure that matters.)
trap 'exit 130' INT
trap 'exit 143' TERM

HAS_HISTORY=0

if [ "$DRY_RUN" -eq 1 ]; then
  echo "release.sh: --dry-run -- building the snapshot locally only, never contacting $RELEASE_REMOTE"
  git -C "$STAGING_DIR" init --quiet -b "$RELEASE_BRANCH" 2>/dev/null \
    || { git -C "$STAGING_DIR" init --quiet; git -C "$STAGING_DIR" symbolic-ref HEAD "refs/heads/$RELEASE_BRANCH"; }
else
  echo "release.sh: cloning release remote: $RELEASE_REMOTE"
  if ! git clone --quiet -- "$RELEASE_REMOTE" "$STAGING_DIR"; then
    echo "release.sh: failed to clone $RELEASE_REMOTE" >&2
    echo "  For a first release, create the (empty) remote repo first -- e.g. an" >&2
    echo "  empty GitHub repo, or 'git init --bare' for a local/self-hosted one --" >&2
    echo "  then re-run this script. On GitHub's 'Create a new repository' page," >&2
    echo "  leave 'Add a README file' UNCHECKED and add no .gitignore or license:" >&2
    echo "  those are committed under your real name and email." >&2
    exit 1
  fi
  if git -C "$STAGING_DIR" rev-parse --verify --quiet "refs/remotes/origin/$RELEASE_BRANCH" >/dev/null; then
    git -C "$STAGING_DIR" checkout --quiet -B "$RELEASE_BRANCH" "refs/remotes/origin/$RELEASE_BRANCH"
    HAS_HISTORY=1
  else
    git -C "$STAGING_DIR" checkout --quiet --orphan "$RELEASE_BRANCH" 2>/dev/null \
      || git -C "$STAGING_DIR" symbolic-ref HEAD "refs/heads/$RELEASE_BRANCH"
    HAS_HISTORY=0
  fi
fi

# --- the remote must carry the release identity, and nothing else -------
#
# Anything already on the release branch becomes an ancestor of this
# release, permanently and unforceably (this script never uses --force).
# A foreign identity down there is the one mistake that cannot be walked
# back afterwards -- see "THE RELEASE IDENTITY IS ALSO A GUARD" in the
# header. Runs before anything is staged, so an abort here has changed
# nothing anywhere.

if [ "$HAS_HISTORY" -eq 1 ]; then
  # An identity is the complete "Name <email>" pair, and every commit
  # carries TWO of them -- author and committer -- which git records
  # separately and which routinely differ (a rebase, a cherry-pick, a
  # web edit rewrite one and keep the other). All four fields are
  # checked, author and committer independently.
  #
  # task-123: this check used to read '%ae%n%ce' and compare emails
  # only, so a commit made as "Real Name <release@configured>" -- the
  # configured address under a personal display name -- passed the guard
  # and put that name permanently into the public history, which is the
  # exact class of mistake the guard exists to stop.
  #
  # Emails compare case-insensitively: an address is case-insensitive in
  # practice and git preserves whatever was typed. Names compare
  # EXACTLY, because a display name is not an address -- "release bot"
  # and "Release Bot" are two different names, and what the public
  # history shows is the name that was configured.
  expected_email="$(printf '%s' "$RELEASE_AUTHOR_EMAIL" | tr '[:upper:]' '[:lower:]')"
  foreign="$(
    git -C "$STAGING_DIR" log \
      --format=$'author\t%an\t%ae\ncommitter\t%cn\t%ce' \
      "refs/remotes/origin/$RELEASE_BRANCH" \
      | while IFS=$'\t' read -r role name email; do
          # A tab inside a name lands the remainder in $email and fails
          # both comparisons -- an unparseable identity is refused, which
          # is the safe direction for a check that cannot be undone.
          email_fold="$(printf '%s' "$email" | tr '[:upper:]' '[:lower:]')"
          if [ "$name" != "$RELEASE_AUTHOR_NAME" ] || [ "$email_fold" != "$expected_email" ]; then
            printf '%s: %s <%s>\n' "$role" "$name" "$email"
          fi
        done \
      | sort -u
  )"
  if [ -n "$foreign" ]; then
    {
      echo "release.sh: REFUSING to release -- $RELEASE_REMOTE ($RELEASE_BRANCH) carries commits"
      echo "  that were not made by the configured release identity:"
      echo "$foreign" | sed 's/^/    /'
      echo "  expected (as both author and committer): $RELEASE_AUTHOR_NAME <$RELEASE_AUTHOR_EMAIL>"
      echo
      echo "  Releasing would build on top of those commits and make them permanent"
      echo "  ancestors of the public history -- and this script never uses --force,"
      echo "  so a later run could not remove them."
      echo
      echo "  The usual cause is a repo created with 'Add a README file' checked on"
      echo "  GitHub's new-repository page (also .gitignore/license templates): that"
      echo "  initial commit is authored under your REAL name and email. Delete the"
      echo "  repo and recreate it completely empty, then re-run this script."
      echo
      echo "  If instead RELEASE_REMOTE is simply pointing at the wrong repository,"
      echo "  fix it in $CONFIG_FILE (or in the environment) before doing anything"
      echo "  else. Nothing was staged and nothing was pushed."
    } >&2
    exit 1
  fi
fi

if [ "$HAS_HISTORY" -eq 1 ]; then
  echo "release.sh: subsequent release -- new commit will follow $(git -C "$STAGING_DIR" log -1 --oneline)"
else
  echo "release.sh: first release on branch $RELEASE_BRANCH -- no parent commit"
fi

# Replace the staging tree (but not .git) with `git archive HEAD` of this
# repo: tracked content only, exactly as of the last commit. The archive
# goes to a file rather than through a pipe into tar, so a git-archive
# failure is a failure of THIS step (with `set -e` above) instead of a
# truncated stream that tar might happily extract part of.
find "$STAGING_DIR" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
git -C "$REPO_ROOT" archive --format=tar HEAD -o "$ARCHIVE_TAR"
if [ ! -s "$ARCHIVE_TAR" ]; then
  echo "release.sh: FAILED -- 'git archive HEAD' produced an empty archive. Nothing pushed." >&2
  exit 1
fi
tar -xf "$ARCHIVE_TAR" -C "$STAGING_DIR"

# Belt and braces on top of `set -e`: every path the archive claims must
# actually exist in the staging tree. A snapshot that is quietly a subset
# of the repo is exactly the failure that would be invisible afterwards.
missing_paths="$(tar -tf "$ARCHIVE_TAR" | while IFS= read -r entry; do
  case "$entry" in
    */) continue ;;
  esac
  [ -e "$STAGING_DIR/$entry" ] || echo "$entry"
done)"
if [ -n "$missing_paths" ]; then
  echo "release.sh: FAILED -- the archive did not extract completely; these paths are missing from the staged snapshot:" >&2
  echo "$missing_paths" | head -20 | sed 's/^/    /' >&2
  echo "  Nothing pushed." >&2
  exit 1
fi

# -f so the snapshot is exactly the archived tracked content: `git add -A`
# alone honours the .gitignore that came WITH the archive, which would
# silently drop any tracked-but-ignored path from the release.
git -C "$STAGING_DIR" add -A -f

if [ "$HAS_HISTORY" -eq 1 ] && git -C "$STAGING_DIR" diff --cached --quiet; then
  echo "release.sh: nothing changed since the last release -- HEAD's tracked content is byte-identical to $RELEASE_BRANCH on $RELEASE_REMOTE. Nothing to do."
  exit 0
fi

# --- the version, and the tag derived from it --------------------------
#
# Read out of the STAGED SNAPSHOT, not the working tree: the tag has to
# name what is actually being published, and the staged tree is `git
# archive HEAD` -- HEAD's tracked content, uncommitted edits excluded.
# version.py is a bare constant module that imports nothing and does
# nothing at import (see its own docstring), so this is a read, not an
# execution of the snapshot.
#
# Deliberately AFTER the "nothing changed" check above: unchanged
# content means the version is unchanged too, and that case is a benign
# "nothing to do", not a tag collision.

if [ ! -f "$STAGING_DIR/version.py" ]; then
  echo "release.sh: FAILED -- version.py is missing from the staged snapshot, so there is no version to tag." >&2
  echo "  It is tracked content and must be in 'git archive HEAD'; check .gitattributes." >&2
  echo "  Nothing pushed." >&2
  exit 1
fi

RELEASE_VERSION="$(cd "$STAGING_DIR" && python3 -c 'import version; print(version.__version__)' 2>/dev/null || true)"
case "$RELEASE_VERSION" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *)
    echo "release.sh: FAILED -- could not read a usable version out of the staged version.py (got '${RELEASE_VERSION}')." >&2
    echo "  __version__ must be a plain X.Y.Z string. Nothing pushed." >&2
    exit 1
    ;;
esac
RELEASE_TAG="v$RELEASE_VERSION"

# --- the release machine's `backlog` must BE the tested baseline -------
#
# task-156. Centrale is a veneer over the `backlog` CLI, so a release is
# implicitly a claim about which one it was verified against -- the
# snapshot's own README and CHANGELOG say so in prose. Cutting that
# snapshot on a machine running a different CLI publishes a claim
# nobody checked, and upstream's cadence (100 releases in 355 days,
# median gap one day) means a release machine drifts off the baseline in
# about a fortnight of not thinking about it.
#
# The baseline is read out of the STAGED SNAPSHOT, by the same mechanism
# and for the same reason as the version above: the claim that has to be
# true is the one being published, not whatever the working tree has
# been edited to say.
#
# A mismatch is a fact about THIS MACHINE, not about the snapshot, so it
# exits $EXIT_ENV like a missing tool does -- the snapshot is fine and
# will release unchanged from a machine on the baseline. The staging
# tree is not kept for inspection for that same reason: there is nothing
# wrong in it to inspect.

TESTED_BACKLOG="$(cd "$STAGING_DIR" && python3 -c 'import version; print(version.TESTED_BACKLOG_VERSION)' 2>/dev/null || true)"
case "$TESTED_BACKLOG" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *)
    echo "release.sh: FAILED -- could not read a usable TESTED_BACKLOG_VERSION out of the staged version.py (got '${TESTED_BACKLOG}')." >&2
    echo "  It must be a plain X.Y.Z string naming the 'backlog' CLI version this" >&2
    echo "  release was verified against -- see docs/operations.md, \"Versioning\"." >&2
    echo "  Without it nothing ties the release to a CLI, which is the whole point" >&2
    echo "  of the constant. Nothing pushed." >&2
    exit 1
    ;;
esac

# The first dotted number the CLI prints, whatever it wraps it in --
# `backlog --version` prints a bare "1.51.0" today, but that is somebody
# else's formatting, not a contract (server.py's backlog_version() reads
# it the same forgiving way).
MACHINE_BACKLOG="$(backlog --version 2>&1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"

if [ -z "$MACHINE_BACKLOG" ]; then
  echo "release.sh: cannot run a release on THIS MACHINE -- 'backlog --version' did not report a version number." >&2
  echo "  The release must be cut against the CLI the snapshot says it was tested" >&2
  echo "  on (${TESTED_BACKLOG}), and this machine's CLI could not be identified, so" >&2
  echo "  that cannot be confirmed. This is a problem with the release machine, not" >&2
  echo "  with the snapshot: nothing was evaluated and nothing was pushed." >&2
  exit "$EXIT_ENV"
fi

if [ "$MACHINE_BACKLOG" != "$TESTED_BACKLOG" ]; then
  {
    echo "release.sh: cannot run a release on THIS MACHINE -- its 'backlog' is $MACHINE_BACKLOG, but"
    echo "  the snapshot declares it was tested against $TESTED_BACKLOG (version.py's"
    echo "  TESTED_BACKLOG_VERSION), and its README and CHANGELOG say so to readers."
    echo "  Releasing from here would publish a tested-against claim that nothing on"
    echo "  this machine ever exercised."
    echo
    echo "  This is a problem with the release machine, not with the snapshot: the"
    echo "  snapshot was staged but never evaluated, and nothing was pushed. Two ways"
    echo "  out, and they are not equivalent:"
    echo "    * Put the baseline back on this machine and re-run:"
    echo "        npm i -g backlog.md@$TESTED_BACKLOG"
    echo "    * Or MOVE the baseline to $MACHINE_BACKLOG -- which is a verification"
    echo "      procedure, not an edit: see docs/operations.md, \"Versioning\"."
  } >&2
  exit "$EXIT_ENV"
fi

echo "release.sh: backlog $MACHINE_BACKLOG on this machine matches the snapshot's tested baseline"

# The version is the ONE number: the tag is derived from it here rather
# than maintained beside it, so a constant/tag disagreement is not a
# thing anyone has to remember -- it cannot be expressed.
if [ "$DRY_RUN" -eq 1 ]; then
  echo "release.sh: version $RELEASE_VERSION -- would tag $RELEASE_TAG (a dry run never contacts the remote, so it cannot know whether that tag is already published)"
else
  echo "release.sh: version $RELEASE_VERSION -- release tag $RELEASE_TAG"
  # The clone above fetched the remote's tags, so this is the public
  # repo's own answer, not a local guess.
  if git -C "$STAGING_DIR" rev-parse -q --verify "refs/tags/$RELEASE_TAG" >/dev/null; then
    {
      echo "release.sh: REFUSING to release -- $RELEASE_TAG is already published on $RELEASE_REMOTE,"
      echo "  but this snapshot's content differs from what is on $RELEASE_BRANCH. Releasing"
      echo "  would either move a published tag or ship an untagged release; neither is a"
      echo "  thing this script does."
      echo
      echo "  Bump the version before releasing again -- one edit, in one place:"
      echo "    1. version.py            __version__ = \"$RELEASE_VERSION\"  ->  the new number"
      echo "    2. CHANGELOG.md          a new '## v<new version>' section saying what changed"
      echo "    3. commit both, then re-run this script"
      echo
      echo "  Nothing was pushed."
    } >&2
    exit 1
  fi
fi

git -C "$STAGING_DIR" \
  -c user.name="$RELEASE_AUTHOR_NAME" -c user.email="$RELEASE_AUTHOR_EMAIL" \
  commit --quiet -m "$MESSAGE"

# An ANNOTATED tag, so the public history has real release points rather
# than a chain of untitled squashes -- and created with the same -c
# identity override as the commit, because an annotated tag records a
# TAGGER of its own. Without the override that tagger would be the
# maintainer's real name and email, published permanently next to a
# commit carefully authored not to be.
git -C "$STAGING_DIR" \
  -c user.name="$RELEASE_AUTHOR_NAME" -c user.email="$RELEASE_AUTHOR_EMAIL" \
  tag -a "$RELEASE_TAG" -m "$RELEASE_TAG -- $MESSAGE"

echo "release.sh: staged release commit: $(git -C "$STAGING_DIR" log -1 --oneline) (tagged $RELEASE_TAG)"

# --- release gate: scan + full suite + doctor check, against the STAGED tree ---

echo "release.sh: running the release gate against the staged snapshot..."

gate_failed() {
  KEEP_STAGING=1
  echo "release.sh: FAILED -- $1 Nothing pushed." >&2
  echo "  This is a failure of the SNAPSHOT, not of this machine's tooling:" >&2
  echo "  git, tar, python3, backlog, node and tmux were all found on PATH before" >&2
  echo "  the snapshot was built, and backlog's version matched the baseline the" >&2
  echo "  snapshot declares (either problem aborts earlier, with exit $EXIT_ENV)." >&2
  exit 1
}

# The scan runs FIRST, and from the snapshot's own copy of the script,
# against the snapshot: the version that gates a release is the version
# that release publishes, never a locally edited one. A missing scan is a
# failure, not a skip -- silently publishing unscanned is the exact
# outcome this gate exists to prevent.
if [ ! -f "$STAGING_DIR/scripts/scan_release.py" ]; then
  gate_failed "scripts/scan_release.py is missing from the staged snapshot, so nothing checked it for secrets or personal identity."
fi

SCAN_STATUS=0
(cd "$STAGING_DIR" && python3 scripts/scan_release.py .) || SCAN_STATUS=$?
if [ "$SCAN_STATUS" -eq 3 ]; then
  # Not "the snapshot is dirty" but "nothing checked it for private
  # names": the scan found no RELEASE_PRIVATE_NAMES anywhere, and a scan
  # that could not be armed must not be read as a clean one (task-166).
  gate_failed "the release scan could not be armed: no RELEASE_PRIVATE_NAMES was set, so nothing checked the snapshot for the names of repos that must not be published. Set it in $CONFIG_FILE -- to an empty value if there genuinely are none."
elif [ "$SCAN_STATUS" -ne 0 ]; then
  gate_failed "the staged snapshot contains something that looks like a secret or a personal identity."
fi

# The changelog has to actually mention the version being tagged. The
# constant and the tag cannot drift (the tag is derived from the
# constant a few lines up), but the CHANGELOG is written by hand and so
# CAN be forgotten -- which would publish a release whose only
# description is a one-line squash message. Checked here rather than
# trusted to a documented step, so "bump the number, forget the entry"
# fails before the push instead of after it.
CHANGELOG_VERSION_RE="$(printf '%s' "$RELEASE_VERSION" | sed 's/[].[^$*\\]/\\&/g')"
if [ ! -f "$STAGING_DIR/CHANGELOG.md" ]; then
  gate_failed "CHANGELOG.md is missing from the staged snapshot, so nothing says what $RELEASE_TAG changed."
fi
if ! grep -Eq "^#+[[:space:]]+v${CHANGELOG_VERSION_RE}([^0-9.]|\$)" "$STAGING_DIR/CHANGELOG.md"; then
  gate_failed "CHANGELOG.md has no '## $RELEASE_TAG' section, so $RELEASE_TAG would be published with nothing saying what changed in it. Add one (or bump version.py, if the number is what is wrong)."
fi

# CENTRALE_REQUIRE_NODE turns "this tier has no runtime, so skip it"
# into a failure for the length of this run only (tests/js_harness.py
# reads it). `node` was already found on PATH above, so the variable is
# not what makes the tier run -- it is what makes a tier that somehow
# still cannot run say so, loudly, instead of leaving the gate to pass
# on a suite that executed no JavaScript at all.
if ! (cd "$STAGING_DIR" && CENTRALE_REQUIRE_NODE=1 python3 -m unittest discover tests); then
  gate_failed "the test suite failed in the staged snapshot (it runs with CENTRALE_REQUIRE_NODE=1, so a test tier that skipped itself for want of a runtime is a failure here rather than an OK)."
fi

# task-130: the integration tier, against the same staged tree, AFTER the
# unit tier (it is the slower of the two, and a unit failure is the
# cheaper diagnosis). `discover tests` never collects tests_integration/,
# so this is the only point at which a release exercises the real git,
# tmux and server-process behaviour of what it is about to publish. A
# snapshot with no tier in it fails for the same reason a missing scan
# does: silently publishing without it is the outcome this step exists
# to prevent. CENTRALE_REQUIRE_INTEGRATION is the tier's counterpart to
# CENTRALE_REQUIRE_NODE (tests_integration/base.py reads it): a test
# that would skip for want of a tool fails and names the tool instead.
# `tmux` was already found on PATH above, so as with `node` the variable
# is not what makes the tier run -- it is what stops a tier that somehow
# still cannot run from passing as an OK.
if [ ! -d "$STAGING_DIR/tests_integration" ]; then
  gate_failed "tests_integration/ is missing from the staged snapshot, so nothing exercised its real git, tmux and server-process behaviour."
fi

if ! (cd "$STAGING_DIR" && CENTRALE_REQUIRE_NODE=1 CENTRALE_REQUIRE_INTEGRATION=1 python3 -m unittest discover tests_integration); then
  gate_failed "the integration tier failed in the staged snapshot (it runs with CENTRALE_REQUIRE_INTEGRATION=1, so an integration test that skipped itself for want of a tool is a failure here rather than an OK)."
fi

if ! (cd "$STAGING_DIR" && python3 server.py --check); then
  gate_failed "'server.py --check' failed in the staged snapshot."
fi

echo "release.sh: release gate passed."

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  echo "release.sh: dry run complete -- nothing pushed, no network contact at all."
  echo "  Would push to:   $RELEASE_REMOTE (branch $RELEASE_BRANCH)"
  echo "  Commit identity: $RELEASE_AUTHOR_NAME <$RELEASE_AUTHOR_EMAIL>"
  echo "  Commit message:  $MESSAGE"
  echo "  Release tag:     $RELEASE_TAG (annotated, same identity)"
  echo "  Snapshot contents:"
  git -C "$STAGING_DIR" show --stat --format="" HEAD | sed 's/^/    /'
  exit 0
fi

# --- push: fast-forward only, never --force -----------------------------

echo "release.sh: pushing to $RELEASE_REMOTE ($RELEASE_BRANCH, tag $RELEASE_TAG)..."
# --atomic: the branch and its tag land together or not at all. Pushed
# separately, a tag push that failed after the branch push succeeded
# would leave a published release that no tag names -- and the next run
# would see identical content, report "nothing to release", and never
# tag it. Neither ref is a force push; --atomic only makes the pair
# all-or-nothing.
if ! git -C "$STAGING_DIR" push --atomic origin \
     "HEAD:refs/heads/$RELEASE_BRANCH" "refs/tags/$RELEASE_TAG"; then
  echo "release.sh: push rejected." >&2
  echo "  If someone else released in the meantime, just re-run this script -- it" >&2
  echo "  rebuilds the snapshot on top of the current remote HEAD every time." >&2
  echo "  If you did NOT expect this remote to already have unrelated history at" >&2
  echo "  all, STOP: double check RELEASE_REMOTE in $CONFIG_FILE before doing" >&2
  echo "  anything else. This refusal is exactly the protection this script's" >&2
  echo "  snapshot-history model exists to provide -- see this script's own header" >&2
  echo "  comment." >&2
  echo "  Nothing was published: the branch and the tag are pushed atomically, so" >&2
  echo "  a rejection of either leaves the remote exactly as it was." >&2
  exit 1
fi

echo "release.sh: released $RELEASE_TAG. $RELEASE_REMOTE ($RELEASE_BRANCH) is now at:"
git -C "$STAGING_DIR" log -1 --oneline
