"""Agent status from remote machines (doc-3, task-209): the one generated
hook command with its two sinks, and the single-machine before/after check.

Hermetic: the worker hook snippet runs under a local `sh` with a recording
stand-in for `tmux` on PATH -- never a real tmux server, never ssh.
"""

import json
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet
import machines as M
import orchestrator
import remote_status as R
import server
import spawn

from lifecycle_harness import ManualTimer

HERE = os.path.dirname(os.path.abspath(__file__))
BOX = {"name": "box-b", "ssh": "u@box-b", "repoRoot": "~/r", "worktreeRoot": "~/w", "maxAgents": 8}


class HookSinkTests(unittest.TestCase):
    """AC #1: one generated hook command per event, two sinks."""

    def run_hook(self, state, stdin="", pane="%7", extra_path=None):
        """Run the tmux-sink hook under sh; return (exit code, tmux argv list)."""
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "tmux.log")
            fake = os.path.join(tmp, "tmux")
            with open(fake, "w") as f:
                f.write(f'#!/bin/sh\nprintf "%s\\n" "$@" > {shlex.quote(log)}\n')
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            env = {"PATH": f"{tmp}:{extra_path or os.environ['PATH']}"}
            if pane is not None:
                env["TMUX_PANE"] = pane
            command = shlex.join(server.hook_argv(state, "tmux"))
            proc = subprocess.run(["sh", "-c", command], input=stdin, text=True,
                                  env=env, capture_output=True, timeout=10)
            calls = None
            if os.path.exists(log):
                with open(log) as f:
                    calls = f.read().splitlines()
            return proc.returncode, proc.stdout + proc.stderr, calls

    def test_http_sink_is_the_unchanged_local_command(self):
        self.assertEqual(server.hook_argv("working"),
                         ["python3", server.notify_script_path(), "working"])
        self.assertEqual(server.hooks_settings_payload(), server.hooks_settings_payload("http"))
        with self.assertRaises(ValueError):
            server.hook_argv("working", "carrier-pigeon")

    def test_tmux_sink_sets_the_hooks_own_pane_option(self):
        for state in ("working", "waiting", "finished"):
            with self.subTest(state):
                code, output, calls = self.run_hook(state)
                self.assertEqual((code, output), (0, ""))
                self.assertEqual(calls[:5], ["set-option", "-p", "-t", "%7", "@centrale_state"])
                value_state, fired_at = calls[5].split(" ")
                self.assertEqual(value_state, state)
                self.assertGreater(float(fired_at), 1.7e9)

    def test_tmux_sink_keeps_the_notification_filter(self):
        for kind in sorted(__import__("centrale_notify").INPUT_NOTIFICATIONS):
            with self.subTest(kind):
                _, _, calls = self.run_hook("notification", json.dumps({"notification_type": kind}))
                self.assertTrue(calls[5].startswith("waiting "))
        for stdin in (json.dumps({"notification_type": "idle_prompt"}), "{}", "not json", ""):
            with self.subTest(stdin=stdin):
                code, output, calls = self.run_hook("notification", stdin)
                self.assertEqual((code, output, calls), (0, "", None))

    def test_tmux_sink_filter_falls_back_to_jq_without_python3(self):
        if not shutil.which("jq"):
            self.skipTest("jq not installed")
        with tempfile.TemporaryDirectory() as bin_dir:
            for tool in ("jq", "cat", "date", "sh"):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            payload = json.dumps({"notification_type": "permission_prompt"})
            code, _, calls = self.run_hook("notification", payload, extra_path=bin_dir)
        self.assertEqual(code, 0)
        self.assertTrue(calls[5].startswith("waiting "))

    def test_tmux_sink_refuses_unknown_states_and_never_fails(self):
        code, output, calls = self.run_hook("merged")
        self.assertEqual((code, output, calls), (0, "", None))
        code, output, calls = self.run_hook("working", pane=None)  # not in tmux at all
        self.assertEqual((code, output, calls), (0, "", None))
        with tempfile.TemporaryDirectory() as bin_dir:  # no tmux on PATH
            for tool in ("sh", "date"):
                os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
            command = shlex.join(server.hook_argv("working", "tmux"))
            proc = subprocess.run(["sh", "-c", command], env={"PATH": bin_dir, "TMUX_PANE": "%1"},
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_claude_and_codex_get_the_tmux_sink_on_a_worker(self):
        settings = spawn._inject_agent_hooks(["claude"], sink="tmux")
        payload = json.loads(settings[1])
        self.assertEqual(payload["hooks"], server.hooks_settings_payload("tmux")["hooks"])
        stop = payload["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(stop), server.hook_argv("finished", "tmux"))
        notification = payload["hooks"]["Notification"][0]["hooks"][0]["command"]
        self.assertEqual(shlex.split(notification)[-1], "notification")
        # A worker's codex is probed on the worker: the caller's answer is used.
        with mock.patch.object(server, "probe_codex_hook_trust",
                               side_effect=AssertionError("probed on the hub")):
            codex = spawn._inject_agent_hooks(["codex"], sink="tmux", codex_supports_hooks=True)
            old = spawn._inject_agent_hooks(["codex"], sink="tmux", codex_supports_hooks=False)
        self.assertEqual(json.loads(codex[1].split("=", 1)[1]), server.hook_argv("finished", "tmux"))
        self.assertEqual(len(old), 2)
        self.assertEqual(codex[-1], "--dangerously-bypass-hook-trust")
        for value in codex[3:-1:2]:
            point, _, toml_value = value.partition("=")
            point = point[len("hooks."):]
            command = tomllib.loads(f"{point}={toml_value}")[point][0]["hooks"][0]["command"]
            self.assertEqual(shlex.split(command)[:2], ["sh", "-c"])


class MachineTmuxTests(unittest.TestCase):
    def test_tmux_socket_is_optional_and_validated(self):
        self.assertEqual(M.tmux_argv(BOX), ["tmux"])
        self.assertEqual(M.tmux_argv(dict(BOX, tmuxSocket="centrale")), ["tmux", "-L", "centrale"])
        self.assertEqual(server.normalize_machines([dict(BOX, tmuxSocket="c-1")])[0]["tmuxSocket"], "c-1")
        self.assertNotIn("tmuxSocket", server.normalize_machines([BOX])[0])
        for bad in ("", "a b", "../x", 3):
            with self.subTest(bad=bad), self.assertRaises(server.ConfigError):
                server.normalize_machines([dict(BOX, tmuxSocket=bad)])


class SingleMachineBeforeAfterTests(unittest.TestCase):
    """AC #3 / doc-3's single-machine guarantee: what a local agent is
    launched with, every badge along a scripted claude and codex hook
    sequence through POST /api/agent-event, and every line an orchestrator
    then hears are exactly what the code before task-209 produced
    (fixtures/single_machine_events.json, captured from that code)."""

    def test_single_machine_status_events_are_unchanged(self):
        import single_machine_events

        with open(os.path.join(HERE, "fixtures", "single_machine_events.json")) as f:
            before = json.load(f)
        after = single_machine_events.run()
        self.assertEqual(after["badges"], before["badges"])
        self.assertEqual(after["orchestrator"], before["orchestrator"])


class LocalPaneRestoreTests(unittest.TestCase):
    def setUp(self):
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)
        journal = mock.patch.object(server, "fleet_history", fleet.Journal())
        journal.start()
        self.addCleanup(journal.stop)
        self.config = {"projects": [{"name": "app", "path": "/unused"}]}
        self.sessions = [{"name": "centrale-app-task-1", "created": "100", "attached": False}]

    def test_read_back_working_before_journal_fallback(self):
        result = subprocess.CompletedProcess([], 0, "working 123.5\n", "")
        with mock.patch.object(server, "run_tmux", return_value=result) as tmux, \
             mock.patch.object(server, "restore_remote_state", wraps=server.restore_remote_state):
            server.restore_local_pane_states(self.config, self.sessions)
        tmux.assert_called_once_with(["list-panes", "-t", "=centrale-app-task-1:",
                                      "-F", "#{@centrale_state}"])
        lifecycle = server.get_fleet_lifecycle("app", "task-1")
        self.assertEqual(lifecycle["state"], "working")
        self.assertEqual(lifecycle["stateSince"], 123.5)

    def test_no_note_leaves_session_for_journal_fallback(self):
        result = subprocess.CompletedProcess([], 0, "\n", "")
        with mock.patch.object(server, "run_tmux", return_value=result):
            self.assertEqual(server.restore_local_pane_states(self.config, self.sessions), [])
        self.assertEqual(server.get_agent_state("app", "task-1"), "unknown")

    def test_old_session_journal_kind_does_not_change_new_pane_claim(self):
        server.fleet_history.append("app", "task-1", "codex", "finished",
                                    timestamp=90, agentKind="codex", created="50")
        result = subprocess.CompletedProcess([], 0, "finished 123.5\n", "")
        with mock.patch.object(server, "run_tmux", return_value=result):
            server.restore_local_pane_states(self.config, self.sessions)
        self.assertEqual(server.get_agent_state("app", "task-1"), "finished")
        self.assertEqual(server.get_agent_kind("app", "task-1"), "unknown")


class FakeControl:
    """A scripted stand-in for one `tmux -C` process on a worker: a tiny
    model of panes and their options that answers the commands
    remote_status sends the way tmux 3.4 did on the probe and the spike
    (%begin/%end with flags 1, %subscription-changed, %sessions-changed),
    and records every command. Never a real tmux."""

    def __init__(self, panes=None):
        self.lock = threading.RLock()
        self.out = queue.Queue()
        self.commands = []
        self.panes = {}       # "%N" -> {"window", "session", "options", "screen"}
        self.subs = {}        # name -> (pane, format)
        self.number = 100
        self.closed = False
        self.stdin = self
        self.stdout = self._lines()
        self.out.put("%begin 1 1 0")
        self.out.put("%end 1 1 0")
        for pane, spec in (panes or {}).items():
            self.add(pane, **spec)

    # -- the test's side -------------------------------------------------
    def add(self, pane, session, window=None, options=None, screen=None):
        with self.lock:
            self.panes[pane] = {"window": window or "@" + pane[1:], "session": session,
                                "options": dict(options or {}), "screen": screen or []}

    def set_option(self, pane, option, value):
        with self.lock:
            self.panes[pane]["options"][option] = value
            for name, (sub_pane, fmt) in self.subs.items():
                if sub_pane == pane:
                    self.out.put(f"%subscription-changed {name} $0 @1 1 {pane} : {self.render(pane, fmt)}")

    def new_session(self, pane, session, options):
        self.add(pane, session, options=options)
        self.out.put("%sessions-changed")

    def kill(self, pane):
        with self.lock:
            self.panes.pop(pane)
        self.out.put("%sessions-changed")

    def drop(self):
        """The connection ends (ssh dropped, worker gone)."""
        self.closed = True
        self.out.put(None)

    # -- Popen's side ---------------------------------------------------------
    def _lines(self):
        while True:
            line = self.out.get()
            if line is None:
                return
            yield line + "\n"

    def flush(self):
        pass

    def poll(self):
        return 0 if self.closed else None

    def terminate(self):
        if not self.closed:
            self.drop()

    def wait(self, timeout=None):
        return 0

    def render(self, pane, fmt):
        spec = self.panes[pane]
        fields = {"pane_id": pane, "window_id": spec["window"], "session_name": spec["session"]}
        return re.sub(r"#\{([^}]+)\}",
                      lambda m: fields.get(m.group(1), spec["options"].get(m.group(1), "")), fmt)

    def write(self, text):
        if self.closed:
            raise OSError("broken pipe")
        for command in text.splitlines():
            with self.lock:
                self.commands.append(command)
                self.number += 1
                ok, lines, after = self.answer(command)
                self.out.put(f"%begin 2 {self.number} 1")
                for line in lines:
                    self.out.put(line)
                self.out.put(f"%{'end' if ok else 'error'} 2 {self.number} 1")
                for line in after:
                    self.out.put(line)

    def answer(self, command):
        words = shlex.split(command)
        if words[:2] == ["list-panes", "-a"]:
            fmt = words[3]
            rows = [self.render(p, fmt) for p in self.panes]
            for pane, spec in self.panes.items():
                if spec.get("linked"):
                    rows.append(self.render(pane, fmt).replace(
                        f"|{spec['session']}|", f"|{R.CONTROL_SESSION}|", 1))
            return True, rows, []
        if words[0] == "link-window":
            window = words[3]
            for spec in self.panes.values():
                if spec["window"] == window:
                    spec["linked"] = True
                    return True, [], []
            return False, ["can't find window"], []
        if words[:2] == ["refresh-client", "-B"]:
            name, _, rest = words[2].partition(":")
            if not rest:
                self.subs.pop(name, None)
                return True, [], []
            pane, _, fmt = rest.partition(":")
            self.subs[name] = (pane, fmt)
            if pane in self.panes and self.panes[pane].get("linked"):
                return True, [], [f"%subscription-changed {name} $0 @1 1 {pane} : {self.render(pane, fmt)}"]
            return True, [], []
        if words[0] == "capture-pane":
            pane = words[words.index("-t") + 1]
            if pane not in self.panes:
                return False, [f"can't find pane: {pane}"], []
            return True, self.panes[pane]["screen"] + ["", ""], []
        return True, [], []


def identity(project, task, kind="claude", state=None):
    options = {"@centrale_project": project, "@centrale_task": task, "@centrale_kind": kind}
    if state is not None:
        options["@centrale_state"] = state
    return options


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RemoteStatusCase(unittest.TestCase):
    MACHINE = dict(BOX, tmuxSocket="cw")

    def setUp(self):
        self.project = "remote-" + os.urandom(4).hex()
        self.config = {"machines": [self.MACHINE], "projects": [
            {"name": self.project, "path": "/unused", "machines": ["box-b"]},
            {"name": "elsewhere", "path": "/unused"}]}
        server._reset_agent_events()
        self.addCleanup(server._reset_agent_events)
        for target, value in ((server, "fleet_history"),):
            patcher = mock.patch.object(target, value, fleet.Journal())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(server.remote_pane_capture.clear)
        self.addCleanup(server.remote_task_pane_capture.clear)
        self.addCleanup(R.stop_all)
        self.opened = []

    def control(self, *fakes):
        """Patch open_control to hand out `fakes` in turn (then fail)."""
        fakes = list(fakes)

        def opener(machine):
            self.assertEqual(machine["name"], "box-b")
            if not fakes:
                raise OSError("ssh: connect to host box-b: Connection refused")
            fake = fakes.pop(0)
            self.opened.append(fake)
            return fake

        patcher = mock.patch.object(R, "open_control", side_effect=opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lines(self, project=None):
        cursor, out = None, []
        while True:
            line = orchestrator.wait(project or self.project, cursor, timeout=0)
            cursor, message = line.rstrip("\n").split(" ", 1)
            if message == "nothing yet":
                return out
            out.append(message)

    def state(self, task="TASK-1"):
        return server.get_agent_lifecycle(self.project, task)


class ControlConnectionTests(RemoteStatusCase):
    """AC #4: one control connection per worker, task windows linked into
    the control session and subscribed by pane id, re-armed on spawn,
    resume and reconnect; every change through record_agent_event."""

    def test_the_control_connection_attaches_to_the_persistent_session(self):
        with mock.patch.object(M, "ssh_command", return_value=["ssh", "-F", "/c"]):
            argv = R.control_argv(self.MACHINE)
        self.assertEqual(argv[:4], ["ssh", "-F", "/c", "u@box-b"])
        self.assertEqual(shlex.split(argv[4]), ["tmux", "-L", "cw", "-C", "new-session", "-A", "-s",
                                                "centrale_control", "sleep infinity"])

    def test_identity_options_are_set_at_spawn_by_pane(self):
        self.assertEqual(R.identity_commands("=centrale-p-task-1:", "p", "task-1", "codex"), [
            ["set-option", "-p", "-t", "=centrale-p-task-1:", "@centrale_project", "p"],
            ["set-option", "-p", "-t", "=centrale-p-task-1:", "@centrale_task", "TASK-1"],
            ["set-option", "-p", "-t", "=centrale-p-task-1:", "@centrale_kind", "codex"]])
        self.assertEqual(R.identity_commands("%3", "p", "TASK-2", None)[2][-1], "custom")

    def test_one_connection_links_subscribes_and_reads_each_task_pane(self):
        fake = FakeControl({
            "%0": {"session": R.CONTROL_SESSION, "window": "@0"},
            "%1": {"session": "centrale-x-task-1", "options": identity(self.project, "task-1",
                                                                        state="working 100.5")},
            "%2": {"session": "centrale-x-task-2", "options": identity(self.project, "TASK-2", "codex")},
            "%3": {"session": "user-shell"},  # no identity: never touched
        })
        self.control(fake)
        report = R.start(self.config, wait=5)
        self.assertEqual(R.open_control.call_count, 1)
        self.assertEqual(report[0]["reach"], "reachable")
        self.assertEqual(report[0]["panes"], {(self.project, "TASK-1"): ("working", "claude"),
                                              (self.project, "TASK-2"): (None, "codex")})
        commands = fake.commands
        self.assertEqual(commands[0], "refresh-client -f no-output,ignore-size")
        self.assertTrue(commands[1].startswith("list-panes -a -F '#{pane_id}|"))
        self.assertIn("link-window -d -s @1 -t centrale_control:", commands)
        self.assertIn("link-window -d -s @2 -t centrale_control:", commands)
        self.assertFalse(any("@3" in c or "@0 " in c for c in commands))
        subscribe = [c for c in commands if c.startswith("refresh-client -B")]
        self.assertEqual(subscribe, [
            "refresh-client -B 'centrale1:%1:#{@centrale_project}|#{@centrale_task}|"
            "#{@centrale_kind}|#{@centrale_state}'",
            "refresh-client -B 'centrale2:%2:#{@centrale_project}|#{@centrale_task}|"
            "#{@centrale_kind}|#{@centrale_state}'"])
        self.assertEqual(self.state(), {"agentState": "working", "agentKind": "claude"})

        # Every later change is one record_agent_event with the hook's firedAt.
        with mock.patch.object(server, "record_agent_event", wraps=server.record_agent_event) as rec:
            fake.set_option("%1", "@centrale_state", f"finished {time.time():.3f}")
            self.assertTrue(wait_for(lambda: self.state()["agentState"] == "finished"))
        args, kwargs = rec.call_args
        self.assertEqual(args, (self.project, "TASK-1", "finished"))
        self.assertEqual(kwargs["agent_kind"], "claude")
        self.assertEqual(kwargs["clock_skew"], M.CLOCK_SKEW_TOLERANCE_SECONDS)
        self.assertIn("TASK-1 finished (ready to review)", self.lines())

    def test_a_scan_never_links_a_window_twice(self):
        fake = FakeControl({"%1": {"session": "s1", "options": identity(self.project, "TASK-1")}})
        self.control(fake)
        R.start(self.config, wait=5)
        connection = R._connections["box-b"]
        connection.scan()
        connection.scan()
        self.assertEqual(sum(c.startswith("link-window") for c in fake.commands), 1)
        self.assertEqual(sum(c.startswith("refresh-client -B") for c in fake.commands), 1)

    def test_a_new_task_session_is_armed_without_being_asked(self):
        fake = FakeControl()
        self.control(fake)
        R.start(self.config, wait=5)
        fake.new_session("%5", "centrale-x-task-5", identity(self.project, "TASK-5", "codex"))
        self.assertTrue(wait_for(lambda: "link-window -d -s @5 -t centrale_control:" in fake.commands))
        self.assertTrue(wait_for(lambda: any(c.startswith("refresh-client -B 'centrale5:%5:")
                                             for c in fake.commands)))
        fake.set_option("%5", "@centrale_state", f"working {time.time():.3f}")
        self.assertTrue(wait_for(lambda: self.state("TASK-5")["agentState"] == "working"))

    def test_rearm_is_what_spawn_and_resume_call(self):
        fake = FakeControl()
        self.control(fake)
        R.start(self.config, wait=5)
        fake.add("%6", "centrale-x-task-6", options=identity(self.project, "TASK-6"))
        R.rearm(self.config, "box-b")
        self.assertTrue(wait_for(lambda: "link-window -d -s @6 -t centrale_control:" in fake.commands))
        R.rearm(self.config, None)  # the hub: nothing to re-arm, and no error
        R.rearm(self.config, "box-z")

    def test_a_pane_that_ends_is_unsubscribed_and_its_capture_dropped(self):
        fake = FakeControl({"%1": {"session": "s1", "options": identity(self.project, "TASK-1")}})
        self.control(fake)
        R.start(self.config, wait=5)
        self.assertIn((self.project, "TASK-1"), server.remote_pane_capture)
        self.assertIn(("box-b", self.project, "TASK-1"), server.remote_task_pane_capture)
        fake.kill("%1")
        self.assertTrue(wait_for(lambda: "refresh-client -B 'centrale1'" in fake.commands))
        self.assertNotIn((self.project, "TASK-1"), server.remote_pane_capture)
        self.assertNotIn(("box-b", self.project, "TASK-1"), server.remote_task_pane_capture)

    def test_a_pane_cannot_speak_for_another_machines_project_or_a_bad_task(self):
        fake = FakeControl({
            "%1": {"session": "s1", "options": identity("elsewhere", "TASK-1", state="finished 1.0")},
            "%2": {"session": "s2", "options": identity("unknown-project", "TASK-1", state="finished 1.0")},
            "%3": {"session": "s3", "options": identity(self.project, "rm -rf", state="finished 1.0")},
        })
        self.control(fake)
        report = R.start(self.config, wait=5)
        self.assertEqual(report[0]["panes"], {})
        self.assertEqual(server.get_agent_state("elsewhere", "TASK-1"), "unknown")
        self.assertFalse(any(c.startswith(("link-window", "refresh-client -B")) for c in fake.commands))

    def test_state_values_are_allow_listed(self):
        fake = FakeControl({"%1": {"session": "s1", "options": identity(self.project, "TASK-1")}})
        self.control(fake)
        R.start(self.config, wait=5)
        for bad in ("merged 5.0", "working", "working soon", "working nan", "working 1 2", "|||"):
            fake.set_option("%1", "@centrale_state", bad)
        fake.set_option("%1", "@centrale_state", f"waiting {time.time():.3f}")
        self.assertTrue(wait_for(lambda: self.state()["agentState"] == "waiting"))
        self.assertEqual(self.lines(), ["TASK-1 waiting for input"])

    def test_a_reused_pane_id_is_rescanned_not_believed(self):
        fake = FakeControl({"%1": {"session": "s1", "options": identity(self.project, "TASK-1")}})
        self.control(fake)
        R.start(self.config, wait=5)
        scans = sum(c.startswith("list-panes") for c in fake.commands)
        # The same %1, now claiming another task, before any scan saw it.
        fake.out.put(f"%subscription-changed centrale1 $0 @1 1 %1 : {self.project}|TASK-9|claude|"
                     f"finished {time.time():.3f}")
        self.assertTrue(wait_for(lambda: sum(c.startswith("list-panes") for c in fake.commands) > scans))
        self.assertEqual(server.get_agent_state(self.project, "TASK-9"), "unknown")

    def test_reconnect_re_subscribes_and_applies_only_what_changed(self):
        first = FakeControl({"%1": {"session": "s1", "options": identity(
            self.project, "TASK-1", state="working 100.0")}})
        stamp = time.time()
        second = FakeControl({"%1": {"session": "s1", "options": identity(
            self.project, "TASK-1", state=f"finished {stamp:.3f}")}})
        self.control(first, second)
        with mock.patch.object(R, "RECONNECT_DELAYS", (0.01,)):
            R.start(self.config, wait=5)
            first.drop()
            self.assertTrue(wait_for(lambda: len(self.opened) == 2 and any(
                c.startswith("refresh-client -B 'centrale1:") for c in second.commands)))
        self.assertTrue(wait_for(lambda: self.state()["agentState"] == "finished"))
        self.assertIn("link-window -d -s @1 -t centrale_control:", second.commands)
        self.assertEqual(R.reach("box-b"), "reachable")
        self.assertEqual(self.lines(), ["TASK-1 finished (ready to review)"])

    def test_disconnect_drops_capture_when_the_pane_is_gone_on_reconnect(self):
        first = FakeControl({"%1": {"session": "s1", "options": identity(self.project, "TASK-1")}})
        second = FakeControl()
        self.control(first, second)
        key = ("box-b", self.project, "TASK-1")
        with mock.patch.object(R, "RECONNECT_DELAYS", (0.01,)):
            R.start(self.config, wait=5)
            self.assertIn(key, server.remote_task_pane_capture)
            first.drop()
            self.assertTrue(wait_for(lambda: len(self.opened) == 2 and R.reach("box-b") == "reachable"))
        self.assertNotIn(key, server.remote_task_pane_capture)
        self.assertNotIn((self.project, "TASK-1"), server.remote_pane_capture)

    def test_an_unreachable_worker_is_retried_and_reported(self):
        self.control()  # every attempt fails
        with mock.patch.object(R, "RECONNECT_DELAYS", (0.01,)):
            report = R.start(self.config, wait=5)
            self.assertEqual(report[0]["reach"], "unreachable")
            self.assertIn("Connection refused", report[0]["error"])
            self.assertTrue(wait_for(lambda: R.open_control.call_count >= 3))

    def test_worker_timestamps_are_accepted_within_the_checked_skew(self):
        now = time.time()
        skew = M.CLOCK_SKEW_TOLERANCE_SECONDS
        # A worker clock slightly ahead: its stamp is not clamped away...
        server.record_agent_event("p", "TASK-1", "working", "claude", fired_at=now + 1, clock_skew=skew)
        # ...and one slightly behind is not "before" the hub-stamped clear.
        server.clear_agent_event("p", "TASK-2", agent_name="claude")
        server.record_agent_event("p", "TASK-2", "working", "claude", fired_at=time.time() - 1,
                                  clock_skew=skew)
        self.assertEqual(server.get_agent_state("p", "TASK-2"), "working")
        # Beyond the tolerance a future stamp still cannot shadow later events.
        server.record_agent_event("p", "TASK-3", "finished", "claude", fired_at=now + 3600,
                                  clock_skew=skew)
        server.record_agent_event("p", "TASK-3", "working", "claude", fired_at=time.time() + skew,
                                  clock_skew=skew)
        self.assertEqual(server.get_agent_state("p", "TASK-3"), "working")
        # A local hook (no skew) keeps the old exact clamp.
        server.clear_agent_event("p", "TASK-4", agent_name="claude")
        server.record_agent_event("p", "TASK-4", "working", "claude", fired_at=time.time() - 1)
        self.assertEqual(server.get_agent_state("p", "TASK-4"), "unknown")


class CodexCorroborationTests(RemoteStatusCase):
    """AC #4: codex's stop and waiting corroboration captures the remote
    pane over the control connection, never a local tmux session."""

    def test_codex_stop_and_wait_capture_over_the_control_connection(self):
        timers = []

        class Timer(ManualTimer):
            def start(self):
                timers.append(self)

        fake = FakeControl({"%4": {"session": "s4", "options": identity(self.project, "TASK-4", "codex"),
                                   "screen": ["› Ask Codex to do anything"]}})
        self.control(fake)
        with mock.patch.object(server, "_agent_wait_timer", Timer), \
             mock.patch.object(server, "capture_session_pane",
                               side_effect=AssertionError("local capture of a remote pane")):
            R.start(self.config, wait=5)
            fake.set_option("%4", "@centrale_state", f"finished {time.time():.3f}")
            self.assertTrue(wait_for(lambda: timers))
            timers.pop().fire()
            self.assertEqual(self.state("TASK-4"), {"agentState": "idle", "agentKind": "codex"})
            self.assertIn("capture-pane -p -t %4 -S -0", fake.commands)

            fake.panes["%4"]["screen"] = ["Allow command?", "› 1. Yes, proceed", "  2. No"]
            fake.set_option("%4", "@centrale_state", f"waiting {time.time():.3f}")
            self.assertTrue(wait_for(lambda: timers))
            with mock.patch.object(server, "detect_pane_dialog", return_value="dialog") as detect:
                timers.pop().fire()
            self.assertEqual(detect.call_args[0][0], ["Allow command?", "› 1. Yes, proceed", "  2. No"])
            self.assertEqual(self.state("TASK-4")["agentState"], "waiting")
            self.assertIn(f"capture-pane -p -t %4 -S -{server.DELIVERY_CAPTURE_LINES}", fake.commands)

    def test_a_gone_pane_reads_as_a_missing_session(self):
        fake = FakeControl({"%4": {"session": "s4", "options": identity(self.project, "TASK-4", "codex")}})
        self.control(fake)
        R.start(self.config, wait=5)
        capture = server.remote_pane_capture[(self.project, "TASK-4")]
        with fake.lock:
            fake.panes.pop("%4")
        with self.assertRaises(server.PaneCaptureError) as caught:
            capture(0)
        self.assertEqual(caught.exception.status, 404)


class BootOrderingTests(RemoteStatusCase):
    """AC #2: at startup remote pane states are read back from reachable
    workers before anything is announced, and unreachable workers are
    announced as such."""

    def persisted_worker_journal(self, task_id="TASK-1"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "fleet.jsonl")
        server.fleet_history = fleet.Journal(path)
        server.record_session_started(self.project, task_id, "claude", machine="box-b")
        server.record_agent_event(self.project, task_id, "working", "claude")
        server._reset_agent_events()
        server.fleet_history = fleet.Journal(path)
        return server.fleet_history

    def test_restart_hub_survey_keeps_worker_session_and_pane_state(self):
        journal = self.persisted_worker_journal()
        stamp = time.time() - 10
        self.control(FakeControl({"%1": {"session": "s1", "options": identity(
            self.project, "TASK-1", state=f"working {stamp}")}}))
        R.start(self.config, wait=5)
        server.observe_fleet_sessions([], self.config)
        self.assertEqual([row["state"] for row in journal.rows], ["spawn", "working"])
        self.assertEqual(self.state("TASK-1"), {"agentState": "working", "agentKind": "claude"})

    def test_restart_records_worker_session_gone_while_down_once(self):
        journal = self.persisted_worker_journal()
        self.control(FakeControl())
        R.start(self.config, wait=5)
        self.assertEqual([row["state"] for row in journal.rows],
                         ["spawn", "working", "session ended"])
        server.observe_fleet_sessions([], self.config)
        R.start(self.config, wait=5)
        self.assertEqual([row["state"] for row in journal.rows],
                         ["spawn", "working", "session ended"])
        self.assertEqual(self.state("TASK-1")["agentState"], "unknown")

    def test_restart_unreachable_worker_does_not_end_or_restore_state(self):
        journal = self.persisted_worker_journal()
        self.control()
        R.start(self.config, wait=5)
        server.observe_fleet_sessions([], self.config)
        self.assertEqual([row["state"] for row in journal.rows], ["spawn", "working"])
        self.assertEqual(self.state("TASK-1")["agentState"], "unknown")
        self.assertEqual([(row["machine"], row["reach"]) for row in R.session_rows(self.config)],
                         [("box-b", "unreachable")])

    def test_restart_legacy_worker_row_uses_live_pane_without_fetched_ref(self):
        journal = self.persisted_worker_journal()
        # A journal written before TASK-237 has no machine on its spawn row.
        for row in journal.rows:
            row.pop("machine", None)
        journal._rewrite()
        server.fleet_history = fleet.Journal(journal.path)
        self.control(FakeControl({"%1": {"session": "s1", "options": identity(
            self.project, "TASK-1", state=f"working {time.time() - 10}")}}))
        with mock.patch.object(M, "locate", return_value=mock.Mock(machine=None, conflict=False)):
            R.start(self.config, wait=5)
            server.observe_fleet_sessions([], self.config)
        self.assertEqual([row["state"] for row in server.fleet_history.rows],
                         ["spawn", "working"])

    def test_read_back_states_are_restored_silently_then_announced_once(self):
        stamp = time.time() - 50
        fake = FakeControl({
            "%1": {"session": "s1", "options": identity(self.project, "TASK-1", state=f"working {stamp}")},
            "%2": {"session": "s2", "options": identity(self.project, "TASK-2", "codex", f"finished {stamp}")},
            "%3": {"session": "s3", "options": identity(self.project, "TASK-3", state=f"waiting {stamp}")},
            "%4": {"session": "s4", "options": identity(self.project, "TASK-4")},
        })
        self.control(fake)
        report = R.start(self.config, wait=5)
        # Nothing was published while reading, and no journal row was written.
        self.assertEqual(self.lines(), [])
        self.assertEqual(server.fleet_history.rows, [])
        self.assertEqual(self.state("TASK-1"), {"agentState": "working", "agentKind": "claude"})
        self.assertEqual(self.state("TASK-2"), {"agentState": "idle", "agentKind": "codex"})
        self.assertEqual(server.get_fleet_lifecycle(self.project, "TASK-1")["stateSince"], stamp)
        R.announce(self.config, report)
        self.assertEqual(sorted(self.lines()), [
            "TASK-1 working (read back from box-b after the server restart)",
            "TASK-2 idle (turn ended, may need input) (read back from box-b after the server restart)",
            "TASK-3 waiting for input (read back from box-b after the server restart)",
            "TASK-4 state unknown after the server restart (no hook event on box-b since the "
            "session started)",
        ])
        # The subscriptions' initial values (the same values) change nothing.
        self.assertEqual(self.lines(), self.lines())
        fake.set_option("%1", "@centrale_state", f"finished {time.time():.3f}")
        self.assertTrue(wait_for(lambda: "TASK-1 finished (ready to review)" in self.lines()))

    def test_an_unreachable_worker_is_announced_for_the_tasks_last_seen_live_there(self):
        server.fleet_history.append(self.project, "TASK-7", "claude", "spawn")
        server.fleet_history.append(self.project, "TASK-7", "claude", "working")
        server.fleet_history.append(self.project, "TASK-8", "claude", "spawn")
        server.fleet_history.append(self.project, "TASK-8", "claude", "session ended")
        server.fleet_history.append("elsewhere", "TASK-9", "claude", "working")
        # task-224: TASK-6 of the same project runs on the hub (no fetched
        # ref from box-b names it), so box-b being down says nothing of it.
        server.fleet_history.append(self.project, "TASK-6", "claude", "working")
        self.control()

        def run_git(args, cwd=None):
            fetched = args[:1] == ["rev-parse"] and args[-1] == "refs/remotes/box-b/task/task-7"
            return subprocess.CompletedProcess(args, 0 if fetched else 1, "", "")

        with mock.patch.object(server, "run_git", run_git):
            report = R.start(self.config, wait=5)
            R.announce(self.config, report)
        self.assertEqual(self.lines(), [
            "TASK-7 box-b unreachable after the server restart; state unknown "
            "(ssh: connect to host box-b: Connection refused)"])
        self.assertEqual(self.lines("elsewhere"), [])

    def test_the_hubs_own_tmux_survey_ends_no_task_on_a_machine(self):
        server.fleet_history.append(self.project, "TASK-7", "claude", "spawn")
        # A session started on a machine says so (record_session_started, task-224).
        server.fleet_history.sessions[(self.project, "TASK-7")] = {"created": None, "observedAt": 0.0,
                                                                   "machine": "box-b"}
        server.fleet_history.sessions[("elsewhere", "TASK-1")] = {"created": None, "observedAt": 0.0}
        server.fleet_history.last_survey = 0.0
        server.observe_fleet_sessions([], self.config)
        self.assertIn((self.project, "TASK-7"), server.fleet_history.sessions)
        self.assertNotIn(("elsewhere", "TASK-1"), server.fleet_history.sessions)  # a hub task: ended
        self.assertEqual(server.fleet_history.latest(self.project, "TASK-7")["state"], "spawn")

    def test_the_wait_is_bounded(self):
        hung = FakeControl()
        hung.write = lambda text: None  # answers nothing, ever
        self.control(hung)
        started = time.monotonic()
        report = R.start(self.config, wait=0.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(report[0]["reach"], "unreachable")
        self.assertIn("not reached within 0.3s", report[0]["error"])

    def test_a_late_first_read_is_an_ordinary_change_not_a_silent_restore(self):
        gate = threading.Event()
        fake = FakeControl({"%1": {"session": "s1", "options": identity(
            self.project, "TASK-1", state=f"finished {time.time():.3f}")}})
        real_write = fake.write
        fake.write = lambda text: (gate.wait(5), real_write(text))
        self.control(fake)
        report = R.start(self.config, wait=0.2)
        R.announce(self.config, report)
        gate.set()
        self.assertTrue(wait_for(lambda: "TASK-1 finished (ready to review)" in self.lines()))

    def test_main_reads_workers_before_announcing(self):
        # main() itself is exercised end to end by
        # tests_integration/test_remote_status_integration.py; here, the order.
        with open(server.__file__) as f:
            main = f.read().split("\ndef main(", 1)[1]
        read = main.index("remote_report = remote_status.start(config)")
        local = main.index("announce_restart_states(config, sessions)")
        remote = main.index("remote_status.announce(config, remote_report)")
        self.assertLess(read, local)
        self.assertLess(local, remote)


class SingleMachineRemoteStatusTests(unittest.TestCase):
    """doc-3's single-machine guarantee for this module: without machines
    nothing starts -- no connection, no thread, no ssh."""

    def test_nothing_starts_without_machines(self):
        threads = threading.active_count()
        with mock.patch.object(R, "open_control", side_effect=AssertionError("connected")), \
             mock.patch.object(M, "run_ssh", side_effect=AssertionError("ssh")):
            self.assertEqual(R.start({"machines": [], "projects": []}), [])
            R.announce({"machines": [], "projects": []}, [])
            R.rearm({}, None)
        self.assertEqual(R._connections, {})
        self.assertEqual(threading.active_count(), threads)
        self.assertEqual(server.remote_pane_capture, {})
        self.assertEqual(server.remote_task_pane_capture, {})


class MachineReconciliationTests(unittest.TestCase):
    """task-228: Settings changes update the running control connections."""

    def setUp(self):
        self.addCleanup(R._connections.clear)
        self.addCleanup(server.remote_pane_capture.clear)
        self.addCleanup(server.remote_task_pane_capture.clear)
        R._connections.clear()
        self.created = []
        self.real_connection = R.Connection

        class FakeConnection:
            def __init__(inner, config, machine, booting=True):
                inner.config, inner.machine, inner.booting = config, machine, booting
                inner.name = machine["name"]
                inner.stopped = False
                self.created.append(inner)

            def start(inner):
                inner.started = True
                return inner

            def stop(inner):
                inner.stopped = True

        patcher = mock.patch.object(R, "Connection", FakeConnection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_add_starts_non_boot_connection_and_unchanged_leaves_it_alone(self):
        config = {"machines": [dict(BOX)], "projects": []}
        R.reconcile(config)
        original = R._connections["box-b"]
        self.assertTrue(original.started)
        self.assertFalse(original.booting)
        R.reconcile(config)
        self.assertIs(R._connections["box-b"], original)
        self.assertFalse(original.stopped)
        self.assertEqual(len(self.created), 1)

    def test_edit_replaces_connection_and_clears_only_its_captures(self):
        config = {"machines": [dict(BOX)], "projects": []}
        R.reconcile(config)
        original = R._connections["box-b"]
        old_capture, other_capture = object(), object()
        server.remote_task_pane_capture[("box-b", "my-app", "TASK-1")] = old_capture
        server.remote_pane_capture[("my-app", "TASK-1")] = old_capture
        server.remote_task_pane_capture[("other", "my-app", "TASK-2")] = other_capture
        server.remote_pane_capture[("my-app", "TASK-2")] = other_capture
        config["machines"] = [dict(BOX, ssh="new@box-b")]
        R.reconcile(config)
        self.assertTrue(original.stopped)
        self.assertIsNot(R._connections["box-b"], original)
        self.assertEqual(R._connections["box-b"].machine["ssh"], "new@box-b")
        self.assertNotIn(("box-b", "my-app", "TASK-1"), server.remote_task_pane_capture)
        self.assertNotIn(("my-app", "TASK-1"), server.remote_pane_capture)
        self.assertIs(server.remote_task_pane_capture[("other", "my-app", "TASK-2")], other_capture)

    def test_remove_stops_and_drops_connection_and_captures(self):
        config = {"machines": [dict(BOX)], "projects": []}
        R.reconcile(config)
        original = R._connections["box-b"]
        capture = object()
        server.remote_task_pane_capture[("box-b", "my-app", "TASK-1")] = capture
        server.remote_pane_capture[("my-app", "TASK-1")] = capture
        config["machines"] = []
        R.reconcile(config)
        self.assertTrue(original.stopped)
        self.assertEqual(R._connections, {})
        self.assertEqual(server.remote_task_pane_capture, {})
        self.assertEqual(server.remote_pane_capture, {})

    def test_retired_connection_cannot_remove_replacements_capture(self):
        config = {"machines": [dict(BOX)], "projects": []}
        old = self.real_connection(config, dict(BOX), booting=False)
        new = self.real_connection(config, dict(BOX, ssh="new@box-b"), booting=False)
        identity = ("my-app", "TASK-1", "codex")
        old._register_capture("%1", identity)
        new._register_capture("%2", identity)
        replacement = server.remote_task_pane_capture[("box-b", "my-app", "TASK-1")]
        old.stop()
        old._unregister_capture(identity)  # late disconnect after replacement
        self.assertIs(server.remote_task_pane_capture[("box-b", "my-app", "TASK-1")], replacement)
        self.assertIs(server.remote_pane_capture[("my-app", "TASK-1")], replacement)

    def test_failed_start_is_reported_without_claiming_a_connection(self):
        class CannotStart:
            def __init__(inner, config, machine, booting=True):
                pass

            def start(inner):
                raise RuntimeError("thread limit")

        with mock.patch.object(R, "Connection", CannotStart):
            warnings = R.reconcile({"machines": [dict(BOX)], "projects": []})
        self.assertEqual(R._connections, {})
        self.assertIn("restart Centrale", warnings[0])


if __name__ == "__main__":
    unittest.main()
