#!/usr/bin/env bash
# harvest.sh — report on (and optionally clean up) merged centrale worktrees.
#
# Usage:
#   scripts/harvest.sh            report each worktree's branch/dirty/merged state
#   scripts/harvest.sh --clean    also remove worktrees + branches that are
#                                  merged into their default branch and have
#                                  no uncommitted changes, and kill their
#                                  matching centrale-* tmux session
#
# worktreeRoot is read from projects.json next to this script's parent dir,
# mirroring server.py's own resolution: the "@repo" sentinel (the shipped
# default, and the code-level fallback when the key is absent/missing
# entirely) means each project's worktrees live inside that project at
# <project path>/.centrale-worktrees, not one shared directory -- so this
# resolves and scans one such directory PER CONFIGURED PROJECT (a project's
# own "worktreeRoot" override, if set, wins over the global one, exactly
# like server.py's resolve_worktree_root), deduplicated in case several
# projects share one plain-path global root.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(dirname -- "$SCRIPT_DIR")"
CONFIG_FILE="$PROJECT_ROOT/projects.json"

CLEAN=0
case "${1:-}" in
  "") ;;
  --clean) CLEAN=1 ;;
  *)
    echo "usage: $0 [--clean]" >&2
    exit 2
    ;;
esac

if [ ! -f "$CONFIG_FILE" ]; then
  echo "harvest.sh: config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# One resolved worktree-root directory per configured project, deduplicated
# (preserving first-seen order) so two projects sharing one plain-path
# global root are only scanned once.
WORKTREE_ROOTS="$(python3 -c '
import json, os, sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)

global_root = data.get("worktreeRoot") or "@repo"

def resolve(project):
    root = project.get("worktreeRoot") or global_root
    if root == "@repo":
        return [os.path.expanduser(os.path.join(project["path"], ".centrale-worktrees"))]
    return [os.path.expanduser(root)]

seen = set()
for project in data.get("projects") or []:
    if not isinstance(project, dict) or not project.get("name") or not project.get("path"):
        continue
    for resolved in resolve(project):
        if resolved not in seen:
            seen.add(resolved)
            print(resolved)
' "$CONFIG_FILE")"

if [ -z "$WORKTREE_ROOTS" ]; then
  echo "harvest.sh: no projects configured in $CONFIG_FILE"
  exit 0
fi

SELF_DIR="$(pwd -P)"
HAVE_TMUX=0
command -v tmux >/dev/null 2>&1 && HAVE_TMUX=1

printf '%-40s %-20s %-6s %-6s\n' "WORKTREE" "BRANCH" "DIRTY" "MERGED"

shopt -s nullglob
while IFS= read -r WORKTREE_ROOT; do
  [ -n "$WORKTREE_ROOT" ] || continue
  if [ ! -d "$WORKTREE_ROOT" ]; then
    echo "harvest.sh: worktreeRoot does not exist, skipping: $WORKTREE_ROOT"
    continue
  fi

  for dir in "$WORKTREE_ROOT"/*/; do
    dir="${dir%/}"
    [ -e "$dir/.git" ] || continue

    branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    [ -n "$branch" ] || branch="(unknown)"

    dirty="no"
    if [ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]; then
      dirty="yes"
    fi

    default_branch="main"
    if ! git -C "$dir" rev-parse --verify --quiet refs/heads/main >/dev/null 2>&1; then
      if git -C "$dir" rev-parse --verify --quiet refs/heads/master >/dev/null 2>&1; then
        default_branch="master"
      fi
    fi

    merged="no"
    if [ "$branch" != "$default_branch" ] \
      && git -C "$dir" rev-parse --verify --quiet "refs/heads/$default_branch" >/dev/null 2>&1 \
      && git -C "$dir" merge-base --is-ancestor HEAD "$default_branch" 2>/dev/null; then
      merged="yes"
    fi

    printf '%-40s %-20s %-6s %-6s\n' "$dir" "$branch" "$dirty" "$merged"

    if [ "$CLEAN" -eq 1 ] && [ "$dirty" = "no" ] && [ "$merged" = "yes" ]; then
      if [ "$(cd -- "$dir" 2>/dev/null && pwd -P)" = "$SELF_DIR" ]; then
        echo "  skip: refusing to clean the current working directory ($dir)"
        continue
      fi

      common_dir="$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
      if [ -z "$common_dir" ]; then
        echo "  skip: could not resolve git-common-dir for $dir"
        continue
      fi
      main_repo="$(dirname -- "$common_dir")"

      if [ "$HAVE_TMUX" -eq 1 ]; then
        session="centrale-$(basename -- "$dir")"
        if tmux has-session -t "$session" 2>/dev/null; then
          tmux kill-session -t "$session" 2>/dev/null \
            && echo "  killed tmux session: $session" \
            || echo "  warning: failed to kill tmux session: $session"
        fi
      fi

      if git -C "$main_repo" worktree remove "$dir" 2>/dev/null; then
        echo "  removed worktree: $dir"
      else
        echo "  warning: failed to remove worktree: $dir"
        continue
      fi

      if git -C "$main_repo" branch -d "$branch" >/dev/null 2>&1; then
        echo "  deleted branch: $branch"
      else
        echo "  warning: failed to delete branch: $branch"
      fi
    fi
  done
done <<< "$WORKTREE_ROOTS"
