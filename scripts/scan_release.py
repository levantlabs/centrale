#!/usr/bin/env python3
"""Refuse to publish a snapshot that carries a secret or a personal identity.

WHY THIS EXISTS
    This repo has now missed the same class of problem twice by hand.
    task-48 scrubbed "docs and examples" with a sweep that explicitly
    excluded backlog/ and never reached tests/fixtures/ or the UI's own
    chrome; task-49 audited hygiene but looked mainly at backlog/. Live
    captures from a private repo and that repo's name in the Settings
    placeholder both fell through the gap and survived until task-100
    found them. A third careful manual pass is not the fix -- a check
    that runs every time is (task-109).

WHAT IT SCANS
    The STAGED SNAPSHOT: the thing that is about to be published, not the
    working tree. scripts/release.sh runs it from inside the extracted
    snapshot as the first step of the release gate, so a hit aborts the
    release before anything is pushed.

    With no arguments it scans this repo instead, which is how anyone
    runs the same check the release runs -- no setup, no dependencies,
    nothing installed:

        python3 scripts/scan_release.py

    Exit status is the whole interface: 0 clean, 1 findings, 2 misuse,
    3 unarmed (see "AN UNARMED SCAN IS NOT A CLEAN SCAN" below).

WHY IT HARDCODES NO NAMES
    A scanner that shipped a list of the author's private repo names, or
    their username, would publish exactly what it exists to withhold --
    re-committing the leak task-100 blocked the release for. So:

      * personal identity is DERIVED at runtime from the machine the
        snapshot is built on (the login account, $HOME's basename, and
        git's configured user.name / user.email), plus generic shapes
        that name no one in particular: home-directory paths and
        anything shaped like a real email address;
      * private project names come from RELEASE_PRIVATE_NAMES, set in
        the gitignored .release-remote next to the rest of the release
        config, or exported as an environment variable of the same name.
        That is the one place to add a repo when you add one to the
        dashboard.

    Nothing personal is committed to this file, and a stranger who clones
    the public snapshot gets a check that protects THEIR identity rather
    than a fossil of someone else's.

WHERE THE PRIVATE-NAME LIST IS LOOKED FOR, IN ORDER
    1. the RELEASE_PRIVATE_NAMES environment variable, which always
       wins -- scripts/release.sh exports it into the release gate,
       because the snapshot the gate scans can never contain the
       gitignored file itself;
    2. .release-remote in the directory being scanned;
    3. .release-remote in the MAIN WORKING TREE of that directory's
       repository, when it is a linked worktree;
    4. .release-remote next to this script's own repository root;
    5. .release-remote in the main working tree of THAT repository.

    Steps 3 and 5 are what task-166 added, and the reason is the whole
    point of the file being gitignored: it exists only in the checkout it
    was written in. Every line of code here is written inside a `git
    worktree add` spawn worktree, which has none -- so before task-166
    the one rule class that exists to stop a private name reaching a
    public snapshot was disarmed in the only place it could ever fire,
    and armed only in the main checkout, where the work has already
    landed. task-157 merged 26 private names into two shipping test files
    with the suite green in the worktree that wrote them. A linked
    worktree shares its repository's common git dir (`git rev-parse
    --git-common-dir`), whose parent is the main working tree -- which is
    where the file actually is.

AN UNARMED SCAN IS NOT A CLEAN SCAN
    With no list resolved from any of those five places, the private-name
    rules have nothing to match, so they cannot fail -- and "found
    nothing" and "looked for nothing" printed the same clean verdict and
    the same exit 0 until task-166. They no longer do: a run that could
    not be armed reports UNARMED and exits 3, whatever else it found or
    did not find. If a tree genuinely has no private names to check for,
    say so explicitly and the scan is armed with an empty list:

        RELEASE_PRIVATE_NAMES="" python3 scripts/scan_release.py

    Declaring nothing and finding nothing must not look alike.

WHEN IT FIRES ON SOMETHING SAFE
    Add an entry to ALLOWLIST below. Every entry carries a reason as a
    required field, not as an optional comment -- an allowlist nobody can
    audit is worse than no allowlist.
"""

import argparse
import getpass
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the private-project-name list lives when it is not in the
# environment: the same gitignored file scripts/release.sh already reads
# its remote and release identity from. One file, one obvious place --
# and, because it is gitignored, one CHECKOUT, which is why the search
# reaches a linked worktree's main working tree too (see the header,
# "WHERE THE PRIVATE-NAME LIST IS LOOKED FOR, IN ORDER").
CONFIG_BASENAME = ".release-remote"
CONFIG_VAR = "RELEASE_PRIVATE_NAMES"

# Returned by main() when nothing armed the private-name rules. Distinct
# from 1 (findings) because it is a different statement: not "this tree
# is dirty" but "this scan cannot tell you whether it is".
EXIT_UNARMED = 3

BINARY_SNIFF_BYTES = 8192

Rule = namedtuple("Rule", "name family pattern description")
Finding = namedtuple("Finding", "path lineno rule matched")


# --- rules: credential shapes -------------------------------------------
#
# Shapes, not entropy heuristics: a fixed prefix or a framing line that
# nothing but the real thing produces. Cheap to read, cheap to trust, and
# it cannot be argued with in review.

_SECRET_ASSIGNMENT = (
    r"(?i)\b(?:api[-_]?key|secret|passwd|password|access[-_]?key"
    r"|auth[-_]?token|client[-_]?secret|private[-_]?key)\b"
    r"\s*[\"']?\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
)

CREDENTIAL_RULES = [
    Rule("private-key", "credential",
         re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----"),
         "a PEM private key block"),
    Rule("openai-key", "credential",
         re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
         "an sk- style API key"),
    Rule("github-token", "credential",
         re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
         "a GitHub personal access / OAuth token"),
    Rule("github-pat", "credential",
         re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
         "a fine-grained GitHub personal access token"),
    Rule("aws-access-key-id", "credential",
         re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
         "an AWS access key id"),
    Rule("slack-token", "credential",
         re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
         "a Slack API token"),
    Rule("bearer-token", "credential",
         re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
         "an Authorization header carrying a real token"),
    Rule("secret-assignment", "credential",
         re.compile(_SECRET_ASSIGNMENT),
         "a key/secret/password assignment carrying a value, not a field name"),
]


# --- rules: personal identity -------------------------------------------
#
# These name nobody in particular; the machine-derived terms that DO name
# somebody are built in derived_identity_rules() at runtime.

IDENTITY_RULES = [
    Rule("home-path", "identity",
         re.compile(r"/(?:home|Users)/[A-Za-z0-9._][A-Za-z0-9._-]*"),
         "a home-directory path naming a specific account"),
    Rule("email-address", "identity",
         re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
         "an email address"),
]

# A derived login name from this set would match ordinary prose in every
# file it appears in, so it never becomes a rule. Skipped names are
# printed, so a run never quietly checks less than it claims to.
GENERIC_ACCOUNT_NAMES = {
    "admin", "build", "builder", "ci", "dev", "docker", "ec2-user", "git",
    "guest", "home", "jenkins", "me", "node", "root", "runner", "test",
    "ubuntu", "user", "users", "vagrant", "you",
}


# --- allowlist ----------------------------------------------------------
#
# (rule name, regex tested against the matched text, why it is safe).
# The reason is a required field: an entry cannot be added without saying
# what makes it harmless.

ALLOWLIST = [
    # The documented placeholder account in docs/api.md's example config
    # and example responses. "user" is not a person -- it is the generic
    # stand-in projects.example.json and the Settings placeholders use,
    # and a reader has to be shown a path shaped like a real one.
    ("home-path", r"^/(?:home|Users)/user$",
     "the generic /home/user placeholder in the API examples -- names no account"),

    # RFC 2606 reserves example.com / .net / .org precisely so documents
    # can print an address that can never reach a mailbox. Used by
    # release.sh's config example and by docs/operations.md.
    ("email-address", r"@example\.(?:com|net|org)$",
     "an RFC 2606 reserved domain -- can never be a real mailbox"),

    # The same RFC reserves the .invalid / .test / .example / .localhost
    # TLDs outright, and RFC 6761 makes .invalid guaranteed never to
    # resolve. tests_integration/test_release_integration.py builds its
    # throwaway release identities under .invalid for exactly that
    # reason, so the addresses it prints belong to no one.
    ("email-address", r"@(?:[A-Za-z0-9-]+\.)*(?:invalid|test|example|localhost)$",
     "an RFC 2606/6761 reserved TLD -- an address there can never resolve"),

    # Not an address at all: the user part of an SSH clone URL, which is
    # the same literal string for every GitHub user on earth.
    ("email-address", r"^git@github\.com$",
     "the SSH remote URL form, identical for every GitHub user"),
]


# --- paths whose identity rules are deliberately not enforced -----------

IDENTITY_EXEMPT_PREFIXES = (
    # backlog/ is this project's own board history: the tasks, decisions
    # and release audits that record how it was built, including incident
    # write-ups that cite the author's machine and private repos as
    # evidence. This project's protocol forbids hand-editing task files,
    # so sanitising it here was never available.
    #
    # task-101 has since settled the question a better way: `backlog/
    # export-ignore` in .gitattributes keeps the board out of every
    # snapshot, so in a git checkout the enumeration above drops it
    # before any rule runs and this entry matches nothing. It stays for
    # the one case that filtering cannot reach -- an unpacked directory
    # that is not a git checkout, where export-ignore is unreadable and a
    # development tree's backlog/ could still be handed to the scan. The
    # CREDENTIAL rules run over it there without exception, and a run
    # that skips anything says so rather than skipping in silence.
    "backlog/",
)

# Where the PRIVATE-NAME rules alone are not enforced: the credential and
# identity rules still run over every one of these, and so does every
# private-name rule in every other file.
#
# A changelog's job includes recording renames and retirements, which it
# cannot do without writing down the name being retired. task-146 dropped
# this project's own pre-rename compatibility paths, and the entry that
# records it has to name the old product name so that someone still
# running a pre-rename build can recognise what they have. That name is
# also a directory name on the maintainer's machine, so it belongs in
# RELEASE_PRIVATE_NAMES -- and would then fail every release on this one
# deliberate mention.
#
# The cost is real and worth stating: a private repo name written into
# CHANGELOG.md by mistake would ship. Nothing else here gets the
# exemption, and the entry that needs it is a line a human writes by hand
# once per release, not something generated.
PRIVATE_NAME_EXEMPT_PATHS = ("CHANGELOG.md",)

# Only consulted when the target is not a git checkout (an unpacked
# tarball, say). In a checkout the tracked-file list is authoritative,
# which is also what keeps a local projects.json or .release-remote --
# gitignored, and full of exactly what this scans for -- out of a
# standalone run.
WALK_SKIP_DIRS = {
    ".git", "__pycache__", ".centrale-worktrees",
    "node_modules", ".venv", "venv",
}


def note(message):
    print(f"scan_release.py: {message}")


# --- what to scan -------------------------------------------------------

def _ancestors(relpath):
    parts = relpath.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def export_ignored(root, paths):
    """The subset of `paths` that `git archive` would leave out, because
    something on the path is marked `export-ignore` in .gitattributes.

    Tracked content is a SUPERSET of what ships: task-101 excluded this
    repo's own board with `backlog/ export-ignore`, and git archive
    honours that while `git ls-files` knows nothing about it. Archive
    checks the attribute on every tree entry as it walks, so a marked
    directory prunes everything beneath it -- and a directory-shaped
    pattern like `backlog/` answers "unspecified" for a file INSIDE it,
    and even for the bare directory name: git only reads it as a
    directory when the path it is asked about carries the trailing slash
    too. So ask about each path's ancestors, in both forms, as well as
    the path itself -- which is what archive effectively does.

    Any failure returns nothing ignored: scanning a file that will not
    ship costs a moment, missing one that will is the whole problem.
    """
    candidates = set()
    for path in paths:
        candidates.add(path)
        for ancestor in _ancestors(path):
            candidates.add(ancestor)
            candidates.add(ancestor + "/")
    if not candidates:
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "check-attr", "--stdin", "-z", "export-ignore"],
            input="\0".join(sorted(candidates)) + "\0",
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if proc.returncode != 0:
        return set()
    # -z output is a flat NUL-separated stream of (path, attribute, value).
    fields = proc.stdout.split("\0")
    marked = {fields[i] for i in range(0, len(fields) - 2, 3)
              if fields[i + 2] == "set"}
    if not marked:
        return set()
    return {p for p in paths
            if p in marked or any(a in marked or a + "/" in marked
                                  for a in _ancestors(p))}


def git_tracked_files(root):
    """Paths git would publish from `root`, or None if it is not a git
    checkout: tracked content minus anything `export-ignore` keeps out of
    the archive. This is deliberately the same content `git archive HEAD`
    ships, so what the scan sees is what the release sends."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    tracked = sorted(p for p in proc.stdout.split("\0") if p)
    ignored = export_ignored(root, tracked)
    return [p for p in tracked if p not in ignored]


def walked_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in WALK_SKIP_DIRS)
        for filename in sorted(filenames):
            full = Path(dirpath) / filename
            found.append(str(full.relative_to(root)))
    return found


def target_files(root):
    tracked = git_tracked_files(root)
    if tracked is not None:
        return tracked, "tracked content"
    return walked_files(root), "a directory walk (not a git checkout)"


# --- identity derived from this machine ---------------------------------

def git_config(root, key):
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", key],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def derived_identity_rules(root):
    """Identity rules built from whoever is running this, so no personal
    term has to be committed to a published file. Returns (rules, terms
    used, terms skipped as too generic)."""
    candidates = []

    for probe in (getpass.getuser,
                  lambda: os.environ.get("USER"),
                  lambda: Path.home().name):
        value = _safe(probe)()
        if value:
            candidates.append(("machine-account", value.strip()))

    email = git_config(root, "user.email")
    if email:
        candidates.append(("git-email", email))
    full_name = git_config(root, "user.name")
    if full_name:
        candidates.append(("git-name", full_name))
        # Search the parts too: a snapshot leaks "Chammas" as readily as
        # it leaks the full string. Short parts are dropped as prose.
        for part in re.split(r"\s+", full_name):
            if len(part) >= 5 and re.fullmatch(r"[A-Za-z][A-Za-z'-]+", part):
                candidates.append(("git-name", part))

    rules, used, skipped = [], [], []
    seen = set()
    for name, term in candidates:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(term) < 4 or key in GENERIC_ACCOUNT_NAMES:
            skipped.append(term)
            continue
        used.append(term)
        rules.append(Rule(
            name, "identity",
            re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                       re.IGNORECASE),
            "personal identity of the account this snapshot was built from",
        ))
    return rules, used, skipped


def _safe(func):
    def call():
        try:
            return func()
        except Exception:
            return None
    return call


# --- private project names ----------------------------------------------

def read_shell_var(path, name):
    """Read NAME="value" out of a shell-sourced config file. Only this one
    assignment form is understood -- the file is read, never executed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(
        r"^[ \t]*(?:export[ \t]+)?" + re.escape(name) + r"[ \t]*=[ \t]*(.*?)[ \t]*$",
        re.MULTILINE,
    )
    value = None
    for match in pattern.finditer(text):
        value = match.group(1)      # last assignment wins, as a shell would
    if value is None:
        return None
    if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
        return value[1:-1]
    return value.split("#", 1)[0].strip()


def main_working_tree(root):
    """The main working tree of the repository `root` belongs to, or None.

    `.release-remote` is gitignored -- that is what guarantees it can
    never ride along into a published snapshot -- so it exists in exactly
    one checkout. A linked worktree (`git worktree add`, which is how
    every spawn works) has none of its own, and every line of code in
    this project is written in one. What a linked worktree DOES share is
    the repository's common git dir, whose parent is the main working
    tree: the checkout the file is in.

    --path-format=absolute needs git 2.31; older gits print a path
    relative to `root`, so resolve it against `root` rather than assuming
    either form.
    """
    for extra in (["--path-format=absolute"], []):
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "rev-parse"] + extra + ["--git-common-dir"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode == 0 and proc.stdout.strip():
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = Path(root) / common
            try:
                return common.resolve().parent
            except OSError:
                return None
    return None


def config_search_path(root):
    """The directories a `.release-remote` is looked for in, in order:
    the tree being scanned and this script's own repo, each followed by
    the main working tree of its repository. Duplicates -- which is what
    all four collapse to in an ordinary main checkout -- are dropped so a
    run reports the places it actually looked."""
    directories = []
    for base in (Path(root), REPO_ROOT):
        directories.append(base)
        main = main_working_tree(base)
        if main is not None:
            directories.append(main)
    ordered, seen = [], set()
    for directory in directories:
        try:
            key = directory.resolve()
        except OSError:
            key = directory
        if key in seen:
            continue
        seen.add(key)
        ordered.append(directory)
    return ordered


def load_private_names(root):
    """(names, source), where source is None ONLY when no configuration
    was found anywhere -- the UNARMED case. A configured but EMPTY list is
    a different answer: somebody said "there are none", and the scan is
    armed with nothing to match, which is a verdict rather than a gap."""
    raw = os.environ.get(CONFIG_VAR)
    source = f"the {CONFIG_VAR} environment variable"
    if raw is None:
        for directory in config_search_path(root):
            candidate = directory / CONFIG_BASENAME
            value = read_shell_var(candidate, CONFIG_VAR)
            if value is not None:
                raw, source = value, str(candidate)
                break
    if raw is None:
        return [], None
    names = [n for n in re.split(r"[\s,]+", raw.strip()) if n]
    return names, source


def private_name_rules(names):
    return [
        Rule("private-project-name", "private-name",
             re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
                        re.IGNORECASE),
             "the name of a repo that is not public")
        for name in names
    ]


# --- scanning -----------------------------------------------------------

def allowlisted(rule_name, matched):
    for allowed_rule, pattern, _reason in ALLOWLIST:
        if allowed_rule == rule_name and re.search(pattern, matched):
            return True
    return False


def identity_exempt(relpath):
    return relpath.startswith(IDENTITY_EXEMPT_PREFIXES)


def private_name_exempt(relpath):
    return relpath in PRIVATE_NAME_EXEMPT_PATHS


def scan_file(root, relpath, rules):
    path = Path(root) / relpath
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return []                      # binary: an image, a font, an archive
    text = data.decode("utf-8", errors="replace")
    exempt = identity_exempt(relpath)
    name_exempt = private_name_exempt(relpath)
    findings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rule in rules:
            if exempt and rule.family != "credential":
                continue
            if name_exempt and rule.family == "private-name":
                continue
            for match in rule.pattern.finditer(line):
                matched = match.group(0)
                if allowlisted(rule.name, matched):
                    continue
                findings.append(Finding(relpath, lineno, rule, matched))
    return findings


def scan(root, rules, files):
    findings = []
    for relpath in files:
        findings.extend(scan_file(root, relpath, rules))
    return findings


# --- entry point --------------------------------------------------------

def build_rules(root):
    """All rules in force for a scan of `root`, the lines describing how
    they were assembled, and whether the private-name rules are ARMED --
    i.e. whether a list was configured at all, empty or not."""
    lines = []
    rules = list(CREDENTIAL_RULES) + list(IDENTITY_RULES)

    derived, used, skipped = derived_identity_rules(root)
    rules.extend(derived)
    if used:
        lines.append("identity terms derived from this machine: "
                     + ", ".join(repr(t) for t in used))
    else:
        lines.append("no identity terms could be derived from this machine "
                     "(no login name and no git user.name/user.email) -- "
                     "only the generic home-path and email shapes apply")
    if skipped:
        lines.append("identity terms too generic to search for, skipped: "
                     + ", ".join(repr(t) for t in skipped))

    names, source = load_private_names(root)
    rules.extend(private_name_rules(names))
    if source is None:
        lines.append(
            f"NO private-name configuration found -- looked for {CONFIG_VAR} "
            f"in the environment, then for {CONFIG_BASENAME} in: "
            + ", ".join(str(d) for d in config_search_path(root)))
    else:
        lines.append(f"private project names: {len(names)} from {source}")

    return rules, lines, source is not None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fail on secrets or personal identity in a release snapshot.",
        epilog="Exit status: 0 clean, 1 findings, 2 misuse, "
               "3 no private-name configuration (see this script's header).",
    )
    parser.add_argument(
        "path", nargs="?", default=str(REPO_ROOT),
        help="the snapshot (or repo) to scan; defaults to this repository",
    )
    args = parser.parse_args(argv)

    root = Path(args.path).resolve()
    if not root.is_dir():
        note(f"not a directory: {root}")
        return 2

    rules, description, armed = build_rules(root)
    files, how = target_files(root)

    note(f"scanning {root} -- {len(files)} files ({how})")
    for line in description:
        note(line)
    exempt = [f for f in files if identity_exempt(f)]
    if exempt:
        note(f"{len(exempt)} files under {', '.join(IDENTITY_EXEMPT_PREFIXES)} "
             "are exempt from the identity rules (credential rules still apply) "
             "-- see IDENTITY_EXEMPT_PREFIXES in this script")

    findings = scan(root, rules, files)
    if not findings and armed:
        note("clean -- nothing that looks like a secret or a personal identity.")
        return 0
    if not findings:
        # Nothing found is not the same statement as nothing looked for,
        # and until task-166 they printed the same word and the same exit
        # status -- in a spawn worktree, which is where all the code is
        # written, that was every run.
        note("no secret or personal identity found -- but the private-name "
             "rules were never armed, so NOTHING here checked this tree for "
             "the names of repos that must not be published.")
        note("that is not a clean verdict. If this tree genuinely has no "
             "private names to check for, say so and the scan is armed "
             f"with an empty list: {CONFIG_VAR}=\"\" python3 "
             "scripts/scan_release.py")
        note(f"otherwise set {CONFIG_VAR} in {CONFIG_BASENAME} -- see the "
             "search order in this script's header.")
        note(f"UNARMED -- exit {EXIT_UNARMED}.")
        return EXIT_UNARMED

    print()
    for finding in findings:
        matched = finding.matched
        if len(matched) > 60:
            matched = matched[:57] + "..."
        print(f"  {finding.path}:{finding.lineno}: [{finding.rule.name}] "
              f"{matched}   ({finding.rule.description})")
    print()
    note(f"FAILED -- {len(findings)} finding(s). This must not be published.")
    note("If a hit is genuinely safe, add it to ALLOWLIST in this script "
         "with the reason it is safe.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
