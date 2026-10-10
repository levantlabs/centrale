"""Remote machines (doc-3, task-206): the `machines` config, the
run-on-machine transport, refresh batching, moving a project, and --check
per machine. Hermetic: ssh is never run -- the run_ssh boundary is faked,
at most by running the generated worker script under a local `sh`."""

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import machines  # noqa: E402
import server  # noqa: E402
import settings  # noqa: E402

M = machines

BOX = {"name": "box-b", "ssh": "user@box-b", "repoRoot": "~/centrale-repos",
       "worktreeRoot": "~/centrale-work", "maxAgents": 8}
OTHER = {"name": "box-c", "ssh": "box-c", "repoRoot": "/srv/repos",
         "worktreeRoot": "/srv/work", "maxAgents": None}


def _config(*machine_list, projects=(), **extra):
    config = {"machines": [dict(m) for m in machine_list], "projects": [dict(p) for p in projects],
              "agents": {"claude": {"cmd": ["claude"]}, "codex": {"cmd": ["codex"]}},
              "refreshIntervalSeconds": 10}
    config.update(extra)
    return config


def _porcelain(*branches):
    blocks = ["worktree /repo\nHEAD abc\nbranch refs/heads/main\n"]
    blocks += [f"worktree /wt/{b}\nHEAD def\nbranch refs/heads/{b}\n" for b in branches]
    return "\n".join(blocks)


def _local_sh(argv, input=None, timeout=None):
    """A run_ssh stand-in that runs the worker's command line under a local
    sh instead of ssh: the generated script is exercised for real, and no
    connection is ever made."""
    assert argv[0] == "ssh" and argv[1] == "-F", argv
    proc = subprocess.run(["sh", "-c", argv[-1]], input=input, capture_output=True,
                          text=True, timeout=30)
    return proc.returncode, proc.stdout, proc.stderr, False


class SshDirTestCase(unittest.TestCase):
    def setUp(self):
        # Short on purpose: ssh's socket-path limit is the point of the check.
        self.ssh_dir = tempfile.mkdtemp(prefix="c206-", dir="/tmp")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.ssh_dir]))
        patcher = mock.patch.dict(os.environ, {M.SSH_DIR_ENV: os.path.join(self.ssh_dir, "s")})
        patcher.start()
        self.addCleanup(patcher.stop)


class ConfigTests(unittest.TestCase):
    def _load(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "projects.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return server.load_config(path)

    def test_machines_and_project_machines_load_with_paths_never_expanded(self):
        config = self._load({"machines": [BOX, OTHER], "projects": [
            {"name": "remote", "path": "~/src/remote", "machines": ["box-b", "box-c"],
             "defaultMachine": "box-c"},
            {"name": "hubdefault", "path": "~/src/h", "machines": ["box-b"], "defaultMachine": "hub"},
            {"name": "local", "path": "~/src/local"}]})
        self.assertEqual(config["machines"], [BOX, OTHER])
        self.assertEqual(config["machines"][0]["repoRoot"], "~/centrale-repos")
        self.assertEqual(config["machines"][0]["worktreeRoot"], "~/centrale-work")
        by_name = {p["name"]: p for p in config["projects"]}
        self.assertEqual((by_name["remote"]["machines"], by_name["remote"]["defaultMachine"]),
                         (["box-b", "box-c"], "box-c"))
        self.assertEqual((by_name["hubdefault"]["machines"], by_name["hubdefault"]["defaultMachine"]),
                         (["box-b"], None))
        self.assertEqual((by_name["local"]["machines"], by_name["local"]["defaultMachine"]), ([], None))
        self.assertNotIn("machine", by_name["remote"])
        # The hub's own checkout path is still the hub's to expand.
        self.assertEqual(by_name["remote"]["path"], os.path.expanduser("~/src/remote"))

    def test_no_machines_key_means_none_and_every_project_on_the_hub(self):
        config = self._load({"projects": [{"name": "a", "path": "/a"}]})
        self.assertEqual(config["machines"], [])
        self.assertEqual(config["projects"][0]["machines"], [])
        self.assertIsNone(config["projects"][0]["defaultMachine"])

    def test_malformed_machines_are_refused_naming_the_key(self):
        cases = {
            "not a list": ({"machines": {"box-b": BOX}}, "'machines' must be an array"),
            "bad name": ({"machines": [dict(BOX, name="-x")]}, "machines[0].name"),
            "duplicate": ({"machines": [BOX, BOX]}, "used twice"),
            "option as target": ({"machines": [dict(BOX, ssh="-oProxyCommand=x")]}, "machines.box-b.ssh"),
            "space in target": ({"machines": [dict(BOX, ssh="a b")]}, "machines.box-b.ssh"),
            "no repoRoot": ({"machines": [{k: v for k, v in BOX.items() if k != "repoRoot"}]},
                            "machines.box-b.repoRoot"),
            "bad cap": ({"machines": [dict(BOX, maxAgents=0)]}, "machines.box-b.maxAgents"),
            "unknown machine": ({"machines": [BOX], "projects": [
                {"name": "p", "path": "/p", "machines": ["box-z"]}]}, "projects.p.machines"),
            "machines not a list": ({"machines": [BOX], "projects": [
                {"name": "p", "path": "/p", "machines": "box-b"}]}, "projects.p.machines"),
            "named twice": ({"machines": [BOX], "projects": [
                {"name": "p", "path": "/p", "machines": ["box-b", "box-b"]}]}, "twice"),
            "machines without machines": ({"projects": [
                {"name": "p", "path": "/p", "machines": ["box-b"]}]}, "projects.p.machines"),
            "default not listed": ({"machines": [BOX, OTHER], "projects": [
                {"name": "p", "path": "/p", "machines": ["box-b"], "defaultMachine": "box-c"}]},
                "projects.p.defaultMachine must be one of hub, box-b"),
            "revision 4's machine key": ({"machines": [BOX], "projects": [
                {"name": "p", "path": "/p", "machine": "box-b"}]}, "replaced by 'machines'"),
        }
        for label, (data, needle) in cases.items():
            with self.subTest(label):
                with self.assertRaises(server.ConfigError) as caught:
                    self._load(data)
                self.assertIn(needle, str(caught.exception))


class SshConfigTests(SshDirTestCase):
    def test_generated_config_puts_the_users_values_first_and_every_option_after(self):
        path = M.ssh_config_path()
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        body = [line.strip() for line in lines if not line.startswith("#")]
        user = body.index(f"Include {os.path.expanduser('~/.ssh/config')}")
        host = body.index("Host *")
        system = body.index("Include /etc/ssh/ssh_config")
        self.assertLess(user, host)
        self.assertLess(host, system)
        for option in ("BatchMode yes", "ConnectTimeout 5", "ServerAliveInterval 5",
                       "ServerAliveCountMax 1", "ControlMaster auto", "ControlPersist 60",
                       f"ControlPath {M.ssh_dir()}/%C"):
            self.assertIn(option, body[host:system])
        self.assertEqual(stat.S_IMODE(os.stat(M.ssh_dir()).st_mode), 0o700)

    def test_every_ssh_and_git_invocation_carries_the_generated_config(self):
        self.assertEqual(M.ssh_command(), ["ssh", "-F", M.ssh_config_path()])
        self.assertEqual(shlex.split(M.git_ssh_env()["GIT_SSH_COMMAND"]), M.ssh_command())

    def test_a_directory_too_long_for_socket_paths_is_refused_and_nothing_runs(self):
        with mock.patch.dict(os.environ, {M.SSH_DIR_ENV: "/tmp/" + "x" * 60}), \
             mock.patch.object(M, "run_ssh", side_effect=AssertionError("ssh ran")):
            result = M.run_on_machine(_config(BOX), "box-b", ["true"])
        self.assertEqual(result.status, M.UNREACHED)
        self.assertIn(M.SSH_DIR_ENV, result.stderr)

    def test_default_directory_is_under_the_runtime_dir(self):
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/7"}, clear=True):
            self.assertEqual(M.ssh_dir(), "/run/user/7/centrale-ssh")


class TransportTests(SshDirTestCase):
    def test_remote_command_line_is_ssh_target_and_a_quoted_sh_script_under_a_worker_timeout(self):
        seen = []

        def fake(argv, input=None, timeout=None):
            seen.append((argv, timeout))
            return _local_sh(argv, input, timeout)

        with mock.patch.object(M, "run_ssh", side_effect=fake):
            M.run_on_machine(_config(BOX), "box-b", ["true"], timeout=7)
        argv, timeout = seen[0]
        self.assertEqual(argv[:4], ["ssh", "-F", M.ssh_config_path(), "user@box-b"])
        self.assertEqual(len(argv), 5)
        words = shlex.split(argv[4])
        self.assertEqual(words[:6], ["timeout", "-k", "2", "7", "sh", "-c"])
        self.assertEqual(timeout, 7)

    def test_arguments_survive_quoting_byte_for_byte(self):
        prompt = "Line one\nit's \"quoted\" $HOME `uname` ; rm -rf / *\n\ttabbed"
        with mock.patch.object(M, "run_ssh", side_effect=_local_sh):
            result = M.run_on_machine(_config(BOX), "box-b", ["printf", "%s", prompt])
        self.assertEqual(result.status, M.ANSWERED)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, prompt)

    def test_a_machine_path_is_expanded_by_the_workers_shell_and_nothing_else_is(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", home]))
        os.mkdir(os.path.join(home, "my repos"))
        with mock.patch.dict(os.environ, {"HOME": home}), \
             mock.patch.object(M, "run_ssh", side_effect=_local_sh):
            results = M.run_batch(_config(BOX), "box-b", [
                (["pwd"], M.MachinePath("~/my repos")),
                (["printf", "%s", "~/my repos"], None),
                (["printf", "%s", M.MachinePath("~")], None),
            ])
        self.assertEqual([r.status for r in results], [M.ANSWERED] * 3)
        self.assertEqual(results[0].stdout.strip(), os.path.join(home, "my repos"))
        self.assertEqual(results[1].stdout, "~/my repos")
        self.assertEqual(results[2].stdout, home)

    def test_one_round_trip_answers_every_command_with_its_own_output_and_exit(self):
        with mock.patch.object(M, "run_ssh", side_effect=_local_sh) as ssh:
            results = M.run_batch(_config(BOX), "box-b", [
                (["sh", "-c", "printf 'out\\n'; printf 'err\\n' >&2; exit 3"], None),
                (["printf", "%s", "no newline"], None),
                (["centrale-no-such-command"], None),
            ])
        self.assertEqual(ssh.call_count, 1)
        self.assertEqual([(r.status, r.returncode) for r in results],
                         [(M.ANSWERED, 3), (M.ANSWERED, 0), (M.ANSWERED, 127)])
        self.assertEqual((results[0].stdout, results[0].stderr), ("out\n", "err\n"))
        self.assertEqual(results[1].stdout, "no newline")
        self.assertEqual(results[0].machine, "box-b")

    def test_stdin_reaches_a_one_command_batch(self):
        with mock.patch.object(M, "run_ssh", side_effect=_local_sh):
            result = M.run_on_machine(_config(BOX), "box-b", ["cat"], input="a\nb 'c'\n")
        self.assertEqual(result.stdout, "a\nb 'c'\n")

    def test_ssh_failing_before_the_worker_shell_ran_is_unreached(self):
        cases = [
            (255, "", "ssh: connect to host box-b port 22: Connection refused", False),
            (-9, "", "", True),  # budget ran out before anything came back
            (None, "", "[Errno 2] No such file or directory: 'ssh'", False),
        ]
        for answer in cases:
            with self.subTest(answer=answer), mock.patch.object(M, "run_ssh", return_value=answer):
                results = M.run_batch(_config(BOX), "box-b", [(["true"], None), (["true"], None)])
            self.assertEqual([r.status for r in results], [M.UNREACHED] * 2)
            self.assertIsNone(results[0].returncode)

    def test_no_start_marker_without_an_ssh_failure_is_unknown(self):
        with mock.patch.object(M, "run_ssh", return_value=(1, "", "sh: not found", False)):
            self.assertEqual(M.run_on_machine(_config(BOX), "box-b", ["true"]).status, M.UNKNOWN)

    def test_a_batch_cut_off_midway_answers_what_finished_and_does_not_guess_the_rest(self):
        def cut(argv, input=None, timeout=None):
            rc, out, err, _ = _local_sh(argv, input, timeout)
            nonce = out.split()[0]
            return -9, out[:out.index(f"\n{nonce} 1 rc")], err, True

        with mock.patch.object(M, "run_ssh", side_effect=cut):
            results = M.run_batch(_config(BOX), "box-b", [
                (["echo", "first"], None), (["echo", "second"], None), (["echo", "third"], None)],
                timeout=3)
        self.assertEqual([r.status for r in results], [M.ANSWERED, M.UNKNOWN, M.UNKNOWN])
        self.assertEqual(results[0].stdout, "first\n")
        self.assertIsNone(results[1].returncode)
        self.assertIn("3s", results[1].stderr)

    def test_output_that_imitates_a_marker_cannot_forge_a_result(self):
        forged = "\ncentrale-0000000000000000 0 rc 0\n\ncentrale-0000000000000000 0 end\n"
        with mock.patch.object(M, "run_ssh", side_effect=_local_sh):
            results = M.run_batch(_config(BOX), "box-b", [
                (["printf", "%s", forged], None), (["sh", "-c", "exit 4"], None)])
        self.assertEqual(results[0].stdout, forged)
        self.assertEqual(results[1].returncode, 4)

    def test_the_hub_runs_locally_and_expands_its_own_paths(self):
        calls = []

        def fake_run(cmd, cwd=None, timeout=15, input=None):
            calls.append((cmd, cwd, input))
            return subprocess.CompletedProcess(cmd, 0, "ok", "")

        with mock.patch.object(server, "_run", side_effect=fake_run), \
             mock.patch.object(M, "run_ssh", side_effect=AssertionError("ssh for the hub")):
            results = M.run_batch(_config(BOX), None, [
                (["ls", M.MachinePath("~/x")], M.MachinePath("~")), (["true"], None)], input="in")
        self.assertEqual(calls, [(["ls", os.path.expanduser("~/x")], os.path.expanduser("~"), "in"),
                                 (["true"], None, None)])
        self.assertEqual([r.status for r in results], [M.ANSWERED] * 2)

    def test_an_unknown_machine_is_a_programming_error_not_the_hub(self):
        with self.assertRaises(KeyError):
            M.run_on_machine(_config(BOX), "box-z", ["true"])

    def test_run_ssh_keeps_partial_output_and_returns_at_the_budget(self):
        # The one real-process test here: a local shell standing in for a
        # remote command that outlives its budget.
        rc, out, err, timed_out = M.run_ssh(["sh", "-c", "echo started; exec sleep 20"], timeout=0.5)
        self.assertTrue(timed_out)
        self.assertEqual(out, "started\n")


class RefreshTests(unittest.TestCase):
    def _counting(self, answer=M.ANSWERED):
        calls = []
        lock = threading.Lock()

        def fake(config, name, commands, timeout=None, input=None):
            with lock:
                calls.append((name, list(commands), timeout))
            return [M.MachineResult(answer, 0 if answer == M.ANSWERED else None, "", "down" if answer != M.ANSWERED else "", name)
                    for _ in commands]

        return calls, fake

    def test_one_batched_round_trip_per_machine_however_many_reads_and_projects(self):
        config = _config(BOX, OTHER, projects=[
            {"name": "a", "path": "/a", "machines": ["box-b"]},
            {"name": "b", "path": "/b", "machines": ["box-b"]},
            {"name": "c", "path": "/c", "machines": ["box-c"]}])
        calls, fake = self._counting()
        reads = {"box-b": [(["git", "status"], "/x"), (["tmux", "list-panes", "-a"], None), (["true"], None)]}
        with mock.patch.object(M, "run_batch", side_effect=fake):
            report = M.refresh(config, reads)
        self.assertEqual(sorted(c[0] for c in calls), ["box-b", "box-c"])
        self.assertEqual(len(next(c for c in calls if c[0] == "box-b")[1]), 3)
        self.assertEqual([r["name"] for r in report], ["box-b", "box-c"])
        self.assertEqual({r["reach"] for r in report}, {"reachable"})

    def test_budget_stays_inside_the_refresh_interval(self):
        config = _config(BOX, refreshIntervalSeconds=5)
        calls, fake = self._counting()
        with mock.patch.object(M, "run_batch", side_effect=fake):
            M.refresh(config)
        self.assertLess(calls[0][2], 5)
        self.assertLessEqual(M.refresh_budget(_config(BOX, refreshIntervalSeconds=600)),
                             server._subprocess_timeout_seconds)

    def test_unreachable_machine_reports_unreachable_with_the_reason(self):
        calls, fake = self._counting(M.UNREACHED)
        with mock.patch.object(M, "run_batch", side_effect=fake):
            report = M.refresh(_config(BOX))
        self.assertEqual(M.reach_summary(report),
                         [{"name": "box-b", "reach": "unreachable", "error": "down"}])

    def test_no_machines_means_no_call_at_all(self):
        with mock.patch.object(M, "run_batch", side_effect=AssertionError("called")):
            self.assertEqual(M.refresh(_config()), [])

    def test_each_board_refresh_is_one_round_trip_per_machine(self):
        config = _config(BOX, OTHER, projects=[
            {"name": "a", "path": "/a", "machines": ["box-b"]},
            {"name": "b", "path": "/b", "machines": ["box-b"]},
            {"name": "c", "path": "/c"}])
        calls, fake = self._counting()
        stub = lambda config, project, report=None: {"name": project["name"], "tasks": []}  # noqa: E731
        with mock.patch.object(M, "run_batch", side_effect=fake), \
             mock.patch.object(server, "_load_project_board", side_effect=stub):
            board = server.get_board(config)
            server.get_board(config)
        self.assertEqual(sorted(c[0] for c in calls), ["box-b", "box-b", "box-c", "box-c"])
        self.assertEqual([m["name"] for m in board["machines"]], ["box-b", "box-c"])

    def test_each_fleet_refresh_is_one_round_trip_per_machine(self):
        import fleet
        config = _config(BOX, OTHER, projects=[{"name": "a", "path": "/a", "machines": ["box-b"]}])
        calls, fake = self._counting(M.UNREACHED)
        with mock.patch.object(M, "run_batch", side_effect=fake), \
             mock.patch.object(server, "fleet_history", fleet.Journal()), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)):
            snapshot = server.get_fleet(config)
        self.assertEqual(sorted(c[0] for c in calls), ["box-b", "box-c"])
        self.assertEqual({m["reach"] for m in snapshot["machines"]}, {"unreachable"})

    def test_board_and_fleet_carry_no_machines_key_without_machines(self):
        import fleet
        config = {"projects": [{"name": "a", "path": "/a"}], "machines": []}
        stub = lambda config, project, report=None: {"name": project["name"], "tasks": []}  # noqa: E731
        with mock.patch.object(M, "run_batch", side_effect=AssertionError("called")), \
             mock.patch.object(server, "_load_project_board", side_effect=stub), \
             mock.patch.object(server, "fleet_history", fleet.Journal()), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)):
            self.assertNotIn("machines", server.get_board(config))
            self.assertNotIn("machines", server.get_fleet(config))


def _answer(stdout="", returncode=0, status=M.ANSWERED, stderr=""):
    return M.MachineResult(status, returncode if status == M.ANSWERED else None, stdout, stderr, "box-b")


class _GitFake:
    """server.run_git for the hub's evidence: local task branches, the hub's
    worktree list, fetched refs per machine and its named git remotes."""

    def __init__(self, local=(), checkouts=(), fetched=None, remotes=()):
        self.local = set(local)
        self.checkouts = list(checkouts)
        self.fetched = {k: set(v) for k, v in (fetched or {}).items()}
        self.remotes = list(remotes)
        self.calls = []

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        out, rc = "", 0
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            ref = args[3]
            if ref.startswith("refs/heads/"):
                rc = 0 if ref[len("refs/heads/"):] in self.local else 1
            else:
                _r, _remotes, machine, branch = ref.split("/", 3)
                rc = 0 if branch in self.fetched.get(machine, ()) else 1
        elif args[:3] == ["worktree", "list", "--porcelain"]:
            out = _porcelain(*self.checkouts)
        elif args[:1] == ["remote"]:
            out = "".join(f"{r}\n" for r in self.remotes)
        elif args[:1] == ["for-each-ref"]:
            prefix = args[-1]
            if prefix == "refs/remotes":
                out = "".join(f"refs/remotes/{m}/{b}\n" for m, bs in self.fetched.items() for b in sorted(bs))
            elif prefix.startswith("refs/remotes/"):
                machine = prefix.split("/")[2]
                out = "".join(f"refs/remotes/{machine}/{b}\n" for b in sorted(self.fetched.get(machine, ())))
            elif prefix == "refs/heads/task":
                out = "".join(f"{b}\n" for b in sorted(self.local))
        return subprocess.CompletedProcess(args, rc, out, "")


def _worker(worktrees=(), branches=(), status=M.ANSWERED):
    """[checkouts_command, branches_command] answers for one machine."""
    if status != M.ANSWERED:
        return [_answer(status=status, stderr="timed out")] * 2
    return [_answer(_porcelain(*worktrees)),
            _answer("".join(f"refs/heads/{b}\n" for b in branches))]


class LocationTests(unittest.TestCase):
    """doc-3 revision 5, "Where a task is" (task-224)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.mkdir(self.repo)
        self.config = _config(BOX, OTHER, projects=[
            {"name": "p", "path": self.repo, "machines": ["box-b", "box-c"]}])
        self.project = self.config["projects"][0]

    def locate(self, git, answers):
        asked = []

        def machines_call(config, batches, timeout=None):
            asked.append(sorted(batches))
            return {name: answers[name] for name in batches}

        with mock.patch.object(server, "run_git", git), \
             mock.patch.object(M, "run_on_machines", side_effect=machines_call):
            location = M.locate(self.config, self.project, "TASK-7")
        return location, asked

    def test_the_worktree_or_branch_decides_and_each_machine_is_one_round_trip(self):
        cases = {
            "hub branch": (_GitFake(local=["task/task-7"]), {}, "hub"),
            "hub worktree": (_GitFake(checkouts=["task/task-7"]), {}, "hub"),
            "machine worktree": (_GitFake(), {"box-c": _worker(worktrees=["task/task-7"])}, "box-c"),
            "machine branch only": (_GitFake(), {"box-b": _worker(branches=["task/task-7"])}, "box-b"),
        }
        for label, (git, worker, where) in cases.items():
            with self.subTest(label):
                answers = {"box-b": _worker(), "box-c": _worker(), **worker}
                location, asked = self.locate(git, answers)
                self.assertEqual(asked, [["box-b", "box-c"]])
                self.assertEqual(location.as_json()["machine"], where)
                self.assertFalse(location.conflict)

    def test_two_places_is_a_conflict_named_with_both_never_a_guess(self):
        location, _ = self.locate(_GitFake(local=["task/task-7"]),
                                  {"box-b": _worker(worktrees=["task/task-7"]), "box-c": _worker()})
        self.assertTrue(location.conflict)
        self.assertIsNone(location.machine)
        self.assertEqual(location.as_json(), {"machine": None, "conflict": ["hub", "box-b"], "reach": None})
        self.assertIn("the hub and machine box-b", location.conflict_reason())

    def test_an_unreachable_machine_is_located_by_its_fetched_ref(self):
        location, _ = self.locate(_GitFake(fetched={"box-c": ["task/task-7"]}),
                                  {"box-b": _worker(), "box-c": _worker(status=M.UNREACHED)})
        self.assertEqual(location.as_json(), {"machine": "box-c", "conflict": None, "reach": "unreachable"})
        location, _ = self.locate(_GitFake(), {"box-b": _worker(), "box-c": _worker(status=M.UNREACHED)})
        self.assertFalse(location.found)
        self.assertEqual(location.as_json(), {"machine": None, "conflict": None, "reach": None})

    def test_a_reachable_machine_answers_for_itself_not_a_stale_fetched_ref(self):
        location, _ = self.locate(_GitFake(local=["task/task-7"], fetched={"box-b": ["task/task-7"]}),
                                  {"box-b": _worker(), "box-c": _worker()})
        self.assertEqual(location.as_json()["machine"], "hub")

    def test_without_live_only_the_hubs_refs_are_read(self):
        with mock.patch.object(server, "run_git", _GitFake(fetched={"box-b": ["task/task-7"]})), \
             mock.patch.object(M, "run_on_machines", side_effect=AssertionError("ssh")):
            location = M.locate(self.config, self.project, "TASK-7", live=False)
        self.assertEqual(location.machine, "box-b")

    def test_a_project_without_machines_is_on_the_hub_with_nothing_run(self):
        project = {"name": "q", "path": self.repo}
        with mock.patch.object(server, "run_git", side_effect=AssertionError("git")), \
             mock.patch.object(M, "run_on_machines", side_effect=AssertionError("ssh")):
            self.assertIsNone(M.locate(self.config, project, "TASK-7").machine)
            self.assertIsNone(server.task_location(self.config, project, "TASK-7"))


class RemovalTests(unittest.TestCase):
    """A machine leaves a project only with none of its tasks there (task-224)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.mkdir(self.repo)

    def _machines(self, answers):
        def run(config, batches, timeout=None):
            return {name: answers[name] for name in batches}
        return mock.patch.object(M, "run_on_machines", side_effect=run)

    def test_removing_a_machine_with_a_task_worktree_or_branch_there_is_refused(self):
        config = _config(BOX, projects=[{"name": "p", "path": self.repo, "machines": ["box-b"]}])
        project = config["projects"][0]
        for label, worker in (("worktree", _worker(worktrees=["task/task-9"])),
                              ("branch", _worker(branches=["task/task-9"]))):
            with self.subTest(label), mock.patch.object(server, "run_git", _GitFake()), \
                    self._machines({"box-b": worker}):
                err = M.machines_removal_refusal(config, project, ["box-b"])
            self.assertIn("machine box-b still has this project's tasks (task/task-9)", err)
        with mock.patch.object(server, "run_git", _GitFake()), self._machines({"box-b": _worker()}):
            self.assertIsNone(M.machines_removal_refusal(config, project, ["box-b"]))
        # The hub's fetched copy counts too.
        with mock.patch.object(server, "run_git", _GitFake(fetched={"box-b": ["task/task-4"]})), \
                self._machines({"box-b": _worker()}):
            self.assertIn("task/task-4", M.machines_removal_refusal(config, project, ["box-b"]))

    def test_a_machine_that_cannot_be_asked_refuses_the_removal(self):
        config = _config(BOX, projects=[{"name": "p", "path": self.repo, "machines": ["box-b"]}])
        for status in (M.UNREACHED, M.UNKNOWN):
            with self.subTest(status=status), mock.patch.object(server, "run_git", _GitFake()), \
                    self._machines({"box-b": _worker(status=status)}):
                err = M.machines_removal_refusal(config, config["projects"][0], ["box-b"])
            self.assertIn("cannot tell", err)
        with mock.patch.object(server, "run_git", _GitFake()), \
                self._machines({"box-b": [_answer(returncode=128, stderr="fatal: not a git repository"),
                                          _answer("")]}):
            self.assertIn("cannot tell", M.machines_removal_refusal(config, config["projects"][0], ["box-b"]))

    def test_the_listings_tolerate_a_missing_bare_repository(self):
        # Nothing spawned there yet: both scripts exit 0 with no output.
        with mock.patch.object(M, "run_ssh", side_effect=_local_sh), \
             mock.patch.dict(os.environ, {M.SSH_DIR_ENV: tempfile.mkdtemp(prefix="c206-", dir="/tmp")}), \
             mock.patch.object(server, "run_git", _GitFake()):
            config = _config(dict(BOX, repoRoot="/nonexistent-c206"),
                             projects=[{"name": "p", "path": self.repo, "machines": ["box-b"]}])
            self.assertIsNone(M.machines_removal_refusal(config, config["projects"][0], ["box-b"]))
            location = M.locate(config, config["projects"][0], "TASK-1")
        self.assertFalse(location.found)

    def _settings_file(self, config):
        path = os.path.join(self.tmp.name, "projects.json")
        raw = {"machines": config["machines"],
               "projects": [{k: v for k, v in p.items() if k in ("name", "path", "machines", "defaultMachine")
                             and v not in (None, [])} for p in config["projects"]]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        return path

    def test_settings_refuses_the_removal_and_writes_nothing(self):
        config = _config(BOX, projects=[{"name": "p", "path": self.repo, "machines": ["box-b"],
                                         "defaultMachine": None}])
        path = self._settings_file(config)
        with mock.patch.object(server, "run_git", _GitFake()), \
                self._machines({"box-b": _worker(worktrees=["task/task-7"])}), \
                self.assertRaises(settings.ValidationError) as caught:
            settings.apply_settings(config, {"projectMachines": {"p": []}}, path=path)
        self.assertIn("task/task-7", caught.exception.fields["projectMachines.p"])
        self.assertEqual(config["projects"][0]["machines"], ["box-b"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["projects"][0]["machines"], ["box-b"])

    def test_settings_adds_and_removes_machines_and_sets_the_default(self):
        config = _config(BOX, projects=[{"name": "p", "path": self.repo, "machines": [],
                                         "defaultMachine": None}])
        path = self._settings_file(config)
        with mock.patch.object(M, "run_on_machines", side_effect=AssertionError("adding asks nothing")):
            result = settings.apply_settings(
                config, {"projectMachines": {"p": ["box-b"]}, "defaultMachine": {"p": "box-b"}}, path=path)
        self.assertEqual((config["projects"][0]["machines"], config["projects"][0]["defaultMachine"]),
                         (["box-b"], "box-b"))
        self.assertEqual((result["projectMachines"], result["defaultMachine"]), ({"p": ["box-b"]}, {"p": "box-b"}))
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)["projects"][0]
        self.assertEqual((saved["machines"], saved["defaultMachine"]), (["box-b"], "box-b"))
        # The default has to go with the machine, in the same save.
        with self.assertRaises(settings.ValidationError) as caught:
            settings.apply_settings(config, {"projectMachines": {"p": []}}, path=path)
        self.assertIn("pick another default", caught.exception.fields["defaultMachine.p"])
        with mock.patch.object(server, "run_git", _GitFake()), self._machines({"box-b": _worker()}):
            settings.apply_settings(config, {"projectMachines": {"p": []}, "defaultMachine": {"p": "hub"}},
                                    path=path)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)["projects"][0]
        self.assertNotIn("machines", saved)
        self.assertNotIn("defaultMachine", saved)
        self.assertEqual(config["projects"][0]["machines"], [])

    def test_settings_rejects_unknown_machines_and_projects(self):
        config = _config(BOX, projects=[{"name": "p", "path": self.repo, "machines": []}])
        with self.assertRaises(settings.ValidationError) as caught:
            settings.apply_settings(config, {"projectMachines": {"p": ["box-z"], "q": []},
                                             "defaultMachine": {"p": "box-b"}},
                                    path=self._settings_file(config))
        self.assertEqual(set(caught.exception.fields), {"projectMachines.p", "projectMachines.q"})
        with self.assertRaises(settings.ValidationError) as caught:
            settings.apply_settings(config, {"defaultMachine": {"p": "box-b"}}, path=self._settings_file(config))
        self.assertIn("must be one of hub", caught.exception.fields["defaultMachine.p"])

    def test_settings_without_machines_shows_nothing_new(self):
        config = {"projects": [{"name": "p", "path": self.repo}], "machines": []}
        for key in ("machine", "machines", "projectMachines", "defaultMachine"):
            self.assertNotIn(key, settings.current_settings(config))

    def test_a_hand_edit_that_strands_tasks_is_refused(self):
        config = _config(BOX, OTHER, projects=[
            {"name": "moved", "path": self.repo, "machines": ["box-b"]},
            {"name": "home", "path": self.repo}])

        def remote(config, batches, timeout=None):
            answers = {"box-b": [_answer("")] * len(batches["box-b"]),
                       "box-c": [_answer(_porcelain("task/task-3")), _answer("")] + [_answer("")] * (
                           len(batches["box-c"]) - 2)}
            return {name: answers[name] for name in batches}

        # The hub holds fetched copies from box-c (no longer listed by
        # "moved") and from "gone" (no longer configured at all); "origin"
        # is a real git remote, the owner's own, and never counts.
        git = _GitFake(fetched={"box-c": ["task/task-5"], "gone": ["task/task-6"], "origin": ["task/task-8"]},
                       remotes=["origin"])
        with mock.patch.object(server, "run_git", git), \
             mock.patch.object(M, "run_on_machines", side_effect=remote):
            refusals, warnings = M.stranded_checkouts(config)
        joined = "\n".join(refusals)
        self.assertIn("fetched copies of its task branches from machine box-c (task/task-5)", joined)
        self.assertIn("machine gone (task/task-6) -- put machine gone back in 'machines'", joined)
        self.assertIn("moved does not list machine box-c in its 'machines', but machine box-c still has "
                      "its tasks (task/task-3)", joined)
        self.assertNotIn("origin", joined)
        self.assertEqual(warnings, [])

    def test_a_hand_edit_that_removed_every_machine_is_still_held_by_the_fetched_refs(self):
        config = {"machines": [], "projects": [{"name": "p", "path": self.repo}]}
        with mock.patch.object(server, "run_git", _GitFake(fetched={"box-b": ["task/task-2"]})), \
             mock.patch.object(M, "run_on_machines", side_effect=AssertionError("ssh")):
            refusals, warnings = M.stranded_checkouts(config)
        self.assertEqual(len(refusals), 1)
        self.assertIn("task/task-2", refusals[0])

    def test_a_hand_edit_check_that_cannot_reach_a_machine_warns_and_refuses_nothing(self):
        config = _config(BOX, projects=[{"name": "home", "path": self.repo}])
        down = lambda config, batches, timeout=None: {  # noqa: E731
            n: [_answer(status=M.UNREACHED, stderr="timed out")] * len(c) for n, c in batches.items()}
        with mock.patch.object(server, "run_git", _GitFake()), mock.patch.object(M, "run_on_machines", side_effect=down):
            refusals, warnings = M.stranded_checkouts(config)
        self.assertEqual(refusals, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("box-b", warnings[0])

    def test_startup_exits_before_serving_when_a_hand_edit_strands_worktrees(self):
        with mock.patch.object(sys, "argv", ["server.py"]), \
             mock.patch.object(server, "install_terminate_handlers"), \
             mock.patch.object(server, "load_config", return_value=dict(
                 _config(BOX), subprocessTimeoutSeconds=30, zeroConfig=False)), \
             mock.patch.object(M, "stranded_checkouts", return_value=(["p is stranded"], [])), \
             mock.patch.object(server, "CentraleHTTPServer", side_effect=AssertionError("served")), \
             mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 1)


class DoctorTests(unittest.TestCase):
    """--check per machine, over a run_batch stand-in that answers each
    probe from a table keyed on what the probe runs."""

    PATHS = {"claude": "/home/user/.npm-global/bin/claude", "codex": "/home/user/.npm-global/bin/codex",
             "backlog": "/home/user/.npm-global/bin/backlog"}

    def setUp(self):
        self.answers = {
            "tmux -V": _answer("tmux 3.4\n"),
            "git --version": _answer("git version 2.43.0\n"),
            "git name": _answer("Ada Example\n"),
            "git email": _answer("ada@example.invalid\n"),
            "filter": _answer("/usr/bin/python3\n"),
            "control": _answer("%begin 1\ncentrale-control-ok\n%end 1\n%exit\n"),
            "date": None,  # filled with the hub's own clock by default
            "repo": _answer("parked\n"),
            "link": _answer(""),
            "ignored": _answer(""),
            "version claude": _answer("2.1.290 (Claude Code)\n"),
            "version codex": _answer("codex-cli 0.160.0\n"),
            "version backlog": _answer(f"{server.version.TESTED_BACKLOG_VERSION}\n"),
            "login claude": _answer('{"loggedIn": true}'),
            "login codex": _answer("Logged in using ChatGPT\n"),
            "trust claude": _answer("trusted\n"),
            "trust codex": _answer("trusted\n"),
            "codex sandbox": _answer(""),
            "codex review": _answer("none\n"),
        }
        for name, path in self.PATHS.items():
            # Interactive shells print noise on both streams; the path wins.
            self.answers[f"resolve {name}"] = _answer(f"welcome!\n{path}\n",
                                                      stderr="bash: no job control in this shell\n")
        self.skew = 0.0
        self.batches = []

    def _key(self, argv):
        joined = " ".join(map(str, argv))
        if argv[0] == "bash":
            return f"resolve {argv[-1].split()[-1]}"
        if argv[:2] == ["tmux", "-V"] or argv[:2] == ["git", "--version"]:
            return joined
        if argv[:3] == ["git", "config", "--global"]:
            return "git name" if argv[-1] == "user.name" else "git email"
        if argv[0] == "python3" and "centrale-codex-review-probe" in joined:
            return "codex review"
        if argv[0] == "python3" and "centrale-trust-probe" in joined:
            return f"trust {argv[-1]}"
        if "python3" in joined:
            return "filter"
        if "centrale-control-ok" in joined:
            return "control"
        if argv[0] == "date":
            return "date"
        if argv[:2] == ["sh", "-c"] and "centrale-repo-probe" in argv[2]:
            return "repo"
        if argv[0] == "test":
            return "ignored" if str(argv[-1]).endswith(".release-remote") else "link"
        name = next(n for n, p in self.PATHS.items() if argv[0] == p)
        if name == "codex" and argv[1:] == ["sandbox", "true"]:
            return "codex sandbox"
        return f"{'version' if argv[1] == '--version' else 'login'} {name}"

    def _run_batch(self, config, name, commands, timeout=None, input=None):
        import time as _time
        self.batches.append(commands)
        out = []
        for argv, _ in commands:
            key = self._key(argv)
            answer = self.answers[key]
            if key == "date" and answer is None:
                answer = _answer(f"{_time.time() + self.skew:.6f}\n")
            out.append(answer)
        return out

    def _check(self, config=None):
        config = config or _config(BOX, projects=[
            {"name": "p", "path": "/p", "machines": ["box-b"], "worktreeLinks": [".venv"]}])
        with mock.patch.object(M, "run_batch", side_effect=self._run_batch), \
             mock.patch.object(M, "hub_tool_version", side_effect=lambda exe: {
                 "claude": "2.1.290", "codex": "0.160.0"}.get(exe)):
            return M.doctor_check(config)

    def test_declared_checks_run_for_each_placed_worker_and_count_results(self):
        import remote
        config = _config(BOX, OTHER, projects=[
            {"name": "p", "path": "/p", "machines": ["box-b", "box-c"],
             "workerCheck": {"command": "check.sh", "doc": "setup.md"}},
            {"name": "hub-only", "path": "/hub", "workerCheck": {"command": "check.sh", "doc": "setup.md"}},
            {"name": "plain", "path": "/plain", "machines": ["box-b"]}])
        def check(config, machine, project):
            return ("PASS" if machine["name"] == "box-b" else "FAIL"), "readiness " + machine["name"]
        with mock.patch.object(remote, "doctor_worker_readiness", side_effect=check) as readiness:
            lines = self._check(config)
        self.assertEqual([(c.args[1]["name"], c.args[2]["name"]) for c in readiness.call_args_list],
                         [("box-b", "p"), ("box-c", "p")])
        self.assertEqual([level for level, text in lines if text.startswith("readiness")], ["PASS", "FAIL"])
        self.assertEqual(server.doctor_counts([(level, text) for level, text in lines if text.startswith("readiness")]),
                         {"PASS": 1, "WARN": 0, "FAIL": 1})

    def _find(self, lines, needle):
        found = [line for line in lines if needle in line[1]]
        self.assertTrue(found, f"no line with {needle!r} in {lines}")
        return found[0]

    def test_a_ready_machine_passes_every_check_in_two_round_trips(self):
        lines = self._check()
        self.assertEqual({level for level, _ in lines}, {"PASS"}, lines)
        self.assertEqual(len(self.batches), 2)
        for needle in ("user@box-b reachable over ssh (BatchMode)", "tmux 3.4 (>= 3.2)",
                       "git version 2.43.0", "python3 at /usr/bin/python3",
                       "tmux control mode works", "clock within 2s",
                       "parked project repository ~/centrale-repos/p present",
                       "worktreeLinks source ~/centrale-repos/p/.venv present"):
            self._find(lines, needle)
        claude = self._find(lines, "claude at")
        self.assertIn("/home/user/.npm-global/bin/claude, version 2.1.290 (hub 2.1.290), logged in", claude[1])
        backlog = self._find(lines, "backlog at")
        self.assertIn(f"tested against {server.version.TESTED_BACKLOG_VERSION}", backlog[1])
        self._find(lines, "p: claude trust recorded")
        self._find(lines, "p: codex trust recorded")
        self._find(lines, "git author identity Ada Example <ada@example.invalid>")
        self._find(lines, "codex sandbox can run a command")

    def test_unused_machine_warns_once_even_when_unreachable(self):
        for unreachable in (False, True):
            with self.subTest(unreachable=unreachable):
                if unreachable:
                    self.answers = {key: _answer(status=M.UNREACHED) for key in self.answers}
                    self.answers["date"] = _answer(status=M.UNREACHED)
                lines = self._check(_config(BOX))
                warnings = [message for level, message in lines
                            if level == "WARN" and "no project" in message]
                self.assertEqual(len(warnings), 1)
                self.assertIn("tick", warnings[0])
                self.assertIn("Settings", warnings[0])
                self.assertIn("box-b", warnings[0])

    def test_missing_global_git_identity_warns(self):
        self.answers["git email"] = _answer("", returncode=1)
        line = self._find(self._check(), "git author identity")
        self.assertEqual(line[0], "WARN")
        self.assertIn("user.email", line[1])
        self.assertIn("placeholder", line[1])

    def test_failed_codex_sandbox_fails_with_error_and_docs_pointer(self):
        self.answers["codex sandbox"] = _answer("", stderr="bwrap: loopback: Failed RTM_NEWADDR", returncode=1)
        line = self._find(self._check(), "codex sandbox cannot run a command")
        self.assertEqual(line[0], "FAIL")
        self.assertIn("bwrap: loopback: Failed RTM_NEWADDR", line[1])
        self.assertIn("docs/operations.md#troubleshooting", line[1])
        self.assertIn(([self.PATHS["codex"], "sandbox", "true"], None), self.batches[1])

    def test_failed_codex_sandbox_with_auto_review_warns(self):
        self.answers["codex sandbox"] = _answer("", stderr="bwrap: setting up uid map: Permission denied", returncode=1)
        self.answers["codex review"] = _answer("auto_review\n")
        line = self._find(self._check(), "codex sandbox cannot run a command")
        self.assertEqual(line[0], "WARN")
        self.assertIn("unsandboxed after automatic review", line[1])

    def test_agent_clis_are_resolved_by_an_interactive_shell(self):
        self._check()
        probes = [argv for argv, _ in self.batches[0] if argv[0] == "bash"]
        self.assertEqual(sorted(p[-1] for p in probes),
                         ["command -v backlog", "command -v claude", "command -v codex"])
        self.assertTrue(all(p[1] == "-ic" for p in probes))
        launched = [argv[0] for argv, _ in self.batches[1]]
        self.assertTrue(set(launched) <= set(self.PATHS.values()) | {"python3"})

    def test_unreachable_machine_is_one_failure_and_nothing_else(self):
        down = _answer(status=M.UNREACHED, stderr="ssh: connect to host box-b: timed out")
        with mock.patch.object(M, "run_batch", side_effect=lambda c, n, cmds, **k: [down] * len(cmds)):
            lines = M.doctor_check(_config(BOX, projects=[{"name": "p", "path": "/p", "machines": ["box-b"]}]))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][0], "FAIL")
        self.assertIn("timed out", lines[0][1])
        self.assertIn("ssh user@box-b true", lines[0][1])

    def test_each_missing_prerequisite_names_itself(self):
        cases = [
            ("tmux -V", _answer("tmux 3.1c\n"), "FAIL", "too old"),
            ("tmux -V", _answer("", returncode=127), "FAIL", "tmux not found"),
            ("git --version", _answer("", returncode=127), "FAIL", "git not found"),
            ("filter", _answer("", returncode=1), "FAIL", "python3 not found"),
            ("control", _answer("open terminal failed", returncode=1), "FAIL", "control mode failed"),
            ("repo", _answer("", returncode=1), "FAIL", "git init -b parking"),
            ("link", _answer("", returncode=1), "WARN", "source ~/centrale-repos/p/.venv is missing"),
            ("resolve codex", _answer("", returncode=1), "WARN", "codex not found by an interactive shell"),
            ("resolve backlog", _answer("", returncode=1), "FAIL", "backlog not found"),
            ("version codex", _answer("codex-cli 0.161.0\n"), "WARN", "0.161.0 (hub 0.160.0), logged in -- versions differ; this is only a warning"),
            ("login claude", _answer("", returncode=1), "FAIL", "claude at /home/user/.npm-global/bin/claude is not logged in"),
            ("version backlog", _answer("1.0.0\n"), "WARN", "version 1.0.0 (tested against"),
        ]
        for key, answer, level, needle in cases:
            with self.subTest(key=key, needle=needle):
                self.setUp()
                self.answers[key] = answer
                line = self._find(self._check(), needle)
                self.assertEqual(line[0], level, line)

    def test_legacy_bare_repository_names_migration_in_check(self):
        self.answers["repo"] = _answer("legacy\n")
        line = self._find(self._check(), "~/centrale-repos/p.git")
        self.assertEqual(line[0], "FAIL")
        self.assertIn("migrate", line[1])
        self.assertIn("docs/operations.md#setting-up-a-worker", line[1])

    def test_missing_repository_prints_park_and_hub_push_with_real_paths(self):
        self.answers["repo"] = _answer("", returncode=1)
        line = self._find(self._check(), "no parked project repository")
        self.assertIn("run this on box-b:\ngit init -b parking", line[1])
        self.assertIn("commit --allow-empty -m parked", line[1])
        self.assertIn("checkout --detach HEAD", line[1])
        self.assertIn("run this on hub:\ngit -C /p push 'user@box-b:~/centrale-repos/p/.git' main:main", line[1])

    def test_missing_venv_uses_hub_requirements_and_worker_venv(self):
        self.answers["link"] = _answer("", returncode=1)
        line = self._find(self._check(), "worktreeLinks source ~/centrale-repos/p/.venv is missing")
        self.assertIn("run this on hub:\n", line[1])
        self.assertIn("/p/.venv/bin/python -m pip freeze --exclude-editable", line[1])
        self.assertIn("python3 -m venv", line[1])
        self.assertIn("pip install -r", line[1])
        self.assertIn("mktemp", line[1])

    def test_missing_node_modules_copies_lockfile_and_runs_npm_ci(self):
        self.answers["link"] = _answer("", returncode=1)
        config = _config(BOX, projects=[{"name": "web", "path": "/web", "machines": ["box-b"],
                                         "worktreeLinks": ["node_modules"]}])
        line = self._find(self._check(config), "worktreeLinks source ~/centrale-repos/web/node_modules is missing")
        self.assertIn("tar -C /web -cf - package.json package-lock.json", line[1])
        self.assertIn("npm ci", line[1])
        self.assertIn("~/centrale-repos/web", line[1])

    def test_nested_node_modules_uses_its_parent_for_manifest_and_npm(self):
        self.answers["link"] = _answer("", returncode=1)
        config = _config(BOX, projects=[{"name": "demo", "path": "/demo", "machines": ["box-b"],
                                         "worktreeLinks": ["tests/browser/node_modules"]}])
        line = self._find(self._check(config),
                          "worktreeLinks source ~/centrale-repos/demo/tests/browser/node_modules is missing")
        self.assertIn("tar -C /demo/tests/browser -cf - package.json package-lock.json", line[1])
        self.assertIn('cd "$HOME"/centrale-repos/demo/tests/browser && npm ci', line[1])
        self.assertNotIn("-cf - tests/browser/node_modules", line[1])

    def test_nested_venv_freezes_and_installs_at_nested_path(self):
        self.answers["link"] = _answer("", returncode=1)
        config = _config(BOX, projects=[{"name": "demo", "path": "/demo", "machines": ["box-b"],
                                         "worktreeLinks": ["tools/python/.venv"]}])
        line = self._find(self._check(config),
                          "worktreeLinks source ~/centrale-repos/demo/tools/python/.venv is missing")
        self.assertIn("/demo/tools/python/.venv/bin/python -m pip freeze --exclude-editable", line[1])
        self.assertIn('python3 -m venv "$HOME"/centrale-repos/demo/tools/python/.venv', line[1])
        self.assertIn('mkdir -p "$HOME"/centrale-repos/demo/tools/python', line[1])

    def test_missing_ignored_check_file_has_copy_command(self):
        self.answers["ignored"] = _answer("", returncode=1)
        config = _config(BOX, projects=[{"name": "p", "path": "/p", "machines": ["box-b"],
                                         "checkCommand": "sh scripts/release.sh"}])
        git = subprocess.CompletedProcess([], 0, ".release-remote\0", "")
        with mock.patch.object(server, "run_git", return_value=git):
            line = self._find(self._check(config), "ignored checkCommand file")
        self.assertIn("run this on hub:\ntar -C /p -cf - .release-remote", line[1])
        self.assertIn("~/centrale-repos/p", line[1])
        self.assertTrue(all(argv[0] in ("tmux", "git", "sh", "date", "bash", "test")
                            for argv, _ in self.batches[0]))

    def test_untrusted_project_names_the_manual_trust_command(self):
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                self.answers[f"trust {kind}"] = _answer("untrusted\n")
                line = self._find(self._check(), f"p: {kind} trust is not recorded")
                self.assertEqual(line[0], "FAIL")
                self.assertIn(f"cd ~/centrale-repos/p && {kind}", line[1])
                self.answers[f"trust {kind}"] = _answer("trusted\n")

    def test_clock_skew_beyond_tolerance_fails(self):
        self.skew = 10.0
        line = self._find(self._check(), "clock is")
        self.assertEqual(line[0], "FAIL")
        self.assertIn("+10.0s", line[1])

    def test_no_machines_means_no_lines_and_no_call(self):
        with mock.patch.object(M, "run_batch", side_effect=AssertionError("called")):
            self.assertEqual(M.doctor_check(_config()), [])

    def test_check_relays_machine_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "projects.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"machines": [BOX]}, f)
            with mock.patch.object(server, "which", return_value=None), \
                 mock.patch.object(server, "probe_running_server", return_value=None), \
                 mock.patch.object(M, "doctor_check", return_value=[("FAIL", "machine box-b: x")]), \
                 mock.patch.object(M, "stranded_checkouts", return_value=([], ["w"])):
                lines, ok = server.run_doctor_check(path)
        self.assertIn("[FAIL] machine box-b: x", lines)
        self.assertIn("[WARN] w", lines)
        self.assertFalse(ok)


class SingleMachineTests(unittest.TestCase):
    """doc-3's single-machine guarantee: with no `machines`, no ssh process
    is ever started -- through the board, the fleet, --check, settings, and
    startup's checks -- asserted on every process actually spawned."""

    def test_no_ssh_process_is_ever_started_without_machines(self):
        spawned = []
        real_popen = subprocess.Popen

        class Recorder(real_popen):
            def __init__(self, args, *a, **k):
                argv = [args] if isinstance(args, str) else list(args)
                spawned.append(os.path.basename(str(argv[0])))
                if spawned[-1] == "ssh":
                    raise AssertionError(f"ssh started: {argv}")
                super().__init__(args, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(repo, "backlog"))
            with open(os.path.join(repo, "backlog", "config.yml"), "w") as f:
                f.write("statuses: [To Do, Done]\n")
            subprocess.run(["git", "init", "-q", repo], check=True)
            path = os.path.join(tmp, "projects.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"projects": [{"name": "p", "path": repo}]}, f)
            config = server.load_config(path)
            import fleet
            with mock.patch.object(subprocess, "Popen", Recorder), \
                 mock.patch.object(server, "run_backlog", side_effect=server.BacklogError("faked")), \
                 mock.patch.object(server, "run_backlog_raw", return_value=subprocess.CompletedProcess(
                     [], 1, "", "faked")), \
                 mock.patch.object(server, "run_tmux", return_value=subprocess.CompletedProcess(
                     [], 1, "", "no server running")), \
                 mock.patch.object(server, "run_bwrap_probe", return_value=subprocess.CompletedProcess(
                     [], 0, "", "")), \
                 mock.patch.object(server, "probe_running_server", return_value=None), \
                 mock.patch.object(server, "fleet_history", fleet.Journal()), \
                 mock.patch.object(server, "read_delivery_log", return_value=([], 0)), \
                 mock.patch.object(M, "run_batch", side_effect=AssertionError("run_batch")), \
                 mock.patch.object(M, "run_ssh", side_effect=AssertionError("run_ssh")):
                server.get_board(config)
                server.get_fleet(config)
                server.run_doctor_check(path)
                self.assertEqual(M.stranded_checkouts(config), ([], []))
                settings.apply_settings(config, {"maxAgents": {"p": 2}}, path=path)
                with mock.patch.object(server, "run_git", side_effect=server.run_git) as git:
                    self.assertIsNone(M.machines_removal_refusal(config, config["projects"][0], []))
                    self.assertEqual(M.locate(config, config["projects"][0], "TASK-1").as_json(),
                                     {"machine": "hub", "conflict": None, "reach": None})
                    self.assertEqual(server.task_location(config, config["projects"][0], "TASK-1"), None)
                self.assertEqual(git.call_count, 0)
        self.assertIn("git", spawned)  # the recorder saw real processes
        self.assertNotIn("ssh", spawned)


if __name__ == "__main__":
    unittest.main()
