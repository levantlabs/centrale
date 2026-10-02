"""Fleet history persistence and HTTP snapshots, with isolated files and no CLI work."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from lifecycle_harness import settled_codex_pane
import server


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'history.jsonl'
        # Each test wants fresh owner questions; the cache has its own test.
        ttl = mock.patch.object(server, 'OWNER_QUESTIONS_TTL', 0.0)
        ttl.start()
        self.addCleanup(ttl.stop)

    def journal(self, now=200000):
        import fleet
        return fleet.Journal(self.path, clock=lambda: now)

    def inbox_snapshot(self, state="waiting", task=None, mode="interact", label=None):
        config = {"projects": [{"name": "app", "path": "/repo"}],
                  "worktreeRoot": self.tmp.name, "sessionPreview": {"mode": mode}}
        journal = self.journal()
        lifecycle = dict(state=state, stateSince=199900, agent="helper", agentKind="codex")
        task = task or {"id": "TASK-1", "status": "In Progress", "finalSummary": None,
                        "labels": [label] if label else [], "updatedAt": "1970-01-03T07:30:00Z"}
        board = {"projects": [{"name": "app", "tasks": [task]}]}
        with mock.patch.object(server, "get_board", return_value=board), \
             mock.patch.object(server, "fleet_history", journal), \
             mock.patch.object(server, "list_sessions", return_value=[
                 {"name": "centrale-app-task-1", "created": "100"},
                 {"name": "master-app", "created": "100"}]), \
             mock.patch.object(server, "get_fleet_lifecycle", return_value=lifecycle), \
             mock.patch.object(server, "capture_session_pane", return_value=[
                 "Allow command?", "› 1. Yes", "  2. No", "Esc to cancel"]) as capture, \
             mock.patch.object(server, "run_git", return_value=subprocess.CompletedProcess([], 1, "", "")), \
             mock.patch.object(server, "run_backlog", return_value={
                 "schemaVersion": 1, "task": task, "tasks": [task]}) as backlog, \
             mock.patch.object(server, "parked_status", return_value=None), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)):
            result = server.get_fleet(config)
        return result, capture.call_count, backlog.call_args_list

    def test_inbox_dialog_is_fresh_and_respects_preview_off(self):
        result, captures, _ = self.inbox_snapshot()
        items = result.get("needsYou", [])
        self.assertEqual([i["kind"] for i in items], ["permission"])
        self.assertEqual(items[0]["lines"][1], "› 1. Yes")
        self.assertEqual(items[0]["agent"], "helper")
        self.assertEqual(captures, 1)
        result, captures, _ = self.inbox_snapshot(mode="off")
        self.assertEqual(result["needsYou"], [])
        self.assertEqual(captures, 0)

    def test_idle_report_and_owner_label_are_read_only_signals(self):
        result, _, calls = self.inbox_snapshot(state="idle", label="needs-owner-approval")
        self.assertEqual({i["kind"] for i in result.get("needsYou", [])}, {"idle", "owner"})
        self.assertTrue(all("edit" not in call.args[0] for call in calls))
        for task in ({"id": "TASK-1", "status": "Done", "labels": ["needs-owner-approval"]},
                     {"id": "TASK-1", "status": "In Progress", "finalSummary": "Finished",
                      "labels": ["needs-owner-approval"]}):
            result, _, _ = self.inbox_snapshot(state="idle", task=task, label="needs-owner-approval")
            self.assertEqual(result["needsYou"], [])
        result, _, _ = self.inbox_snapshot(state="finished")
        self.assertEqual(result["needsYou"], [])

    def test_owner_question_uses_latest_comment_or_title_without_configuration(self):
        for comments, expected in (([], "Choose a release date"),
                ([{"index": 1, "body": "Old question"},
                  {"index": 2, "body": "Ship Friday?"}], "Ship Friday?")):
            task = {"id": "TASK-1", "title": "Choose a release date",
                    "status": "In Progress", "labels": ["needs-owner-approval"],
                    "comments": comments}
            result, _, calls = self.inbox_snapshot(state="working", task=task)
            owners = [i for i in result["needsYou"] if i["kind"] == "owner"]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0].get("text"), expected)
            self.assertTrue(all("edit" not in call.args[0] for call in calls))

    def test_owner_label_removal_and_other_labels_do_not_request_owner(self):
        for labels in ([], ["needs-owner"], ["NEEDS-OWNER-APPROVAL"]):
            result, _, _ = self.inbox_snapshot(state="working", task={
                "id": "TASK-1", "status": "In Progress", "labels": labels})
            self.assertEqual(result["needsYou"], [])

    def test_owner_questions_use_branch_labels_and_reports_across_projects(self):
        label = "needs-owner-approval"
        config = {"projects": [{"name": "app", "path": "/app"},
                               {"name": "other", "path": "/other", "ownerLabel": "legacy"}],
                  "worktreeRoot": self.tmp.name}
        for name in ("app", "other"):
            (Path(self.tmp.name) / (name + "-task-1")).mkdir()
        board = {"projects": [{"name": name, "tasks": [{"id": "TASK-1",
            "title": "Main title", "status": "In Progress", "labels": [],
            "hasSpawnBranch": True}]} for name in ("app", "other")]}
        report = {"id": "TASK-1", "status": "In Progress", "labels": [label],
                  "comments": [{"index": 1, "body": "Which option?"}]}
        snapshot = {"agents": [{"project": name, "taskId": "TASK-1",
                    "state": "finished", "agent": "claude"} for name in ("app", "other")],
                    "timestamp": 200000}
        with (mock.patch.object(server, "get_board", return_value=board),
              mock.patch.object(server, "fleet_history", self.journal()),
              mock.patch.object(server, "run_backlog", return_value={
                  "schemaVersion": 1, "task": report}) as backlog,
              mock.patch.object(server, "run_git", side_effect=AssertionError("unexpected git"))):
            items, errors = server.fleet_inbox(config, snapshot, [])
            self.assertEqual(errors, [])
            self.assertEqual([(i["project"], i["text"]) for i in items],
                             [("app", "Which option?"), ("other", "Which option?")])
            for update in ({"labels": []}, {"status": "Done"}, {"finalSummary": "Complete"}):
                with self.subTest(update=update):
                    backlog.return_value = {"schemaVersion": 1, "task": {**report, **update}}
                    self.assertEqual(server.fleet_inbox(config, snapshot, [])[0], [])
            self.assertTrue(all(c.args[0] == ["task", "view", "TASK-1", "--json"]
                                for c in backlog.call_args_list))
            # A spawn branch with no live worker is never read: only a
            # running worker can be asking now, and reading every old
            # branch on each poll made /api/fleet take minutes.
            backlog.reset_mock()
            backlog.return_value = {"schemaVersion": 1, "task": report}
            self.assertEqual(server.fleet_inbox(config, {"agents": [], "timestamp": 200000}, []),
                             ([], []))
            backlog.assert_not_called()

    def test_owner_question_text_is_the_latest_comment_that_asks(self):
        # Seen live: the worker asked, then an orchestrator relayed a
        # partial ruling ("no code until the owner rules"). The card must
        # still show the question, not the reply.
        config = {"projects": [{"name": "app", "path": "/repo"}], "worktreeRoot": self.tmp.name}
        board = {"projects": [{"name": "app", "tasks": [
            {"id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"]}]}]}
        report = {"id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"],
                  "comments": [{"index": 1, "body": "Plan recorded."},
                               {"index": 2, "body": "Should it land in the open schematic (A) or be refused (B)?"},
                               {"index": 3, "body": "[1/2] Approved with conditions. AC#1: no code until the owner rules."}]}
        with (mock.patch.object(server, "get_board", return_value=board),
              mock.patch.object(server, "fleet_history", self.journal()),
              mock.patch.object(server, "run_backlog", return_value={"schemaVersion": 1, "task": report})):
            items, _ = server.fleet_inbox(config, {"agents": [], "timestamp": 200000}, [])
            self.assertEqual([i["text"] for i in items],
                             ["Should it land in the open schematic (A) or be refused (B)?"])
            report["comments"] = [{"index": 1, "body": "Plan recorded; waiting for a decision."}]
            items, _ = server.fleet_inbox(config, {"agents": [], "timestamp": 200000}, [])
            self.assertEqual([i["text"] for i in items], ["Plan recorded; waiting for a decision."])

    def test_owner_questions_are_recomputed_at_most_once_per_ttl(self):
        config = {"projects": [{"name": "app", "path": "/repo"}], "worktreeRoot": self.tmp.name}
        board = {"projects": [{"name": "app", "tasks": [
            {"id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"]}]}]}
        report = {"id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"],
                  "comments": [{"index": 1, "body": "Which option?"}]}
        with (mock.patch.object(server, "OWNER_QUESTIONS_TTL", 30.0),
              mock.patch.dict(server._owner_cache, {"at": None, "items": [], "errors": []}),
              mock.patch.object(server, "get_board", return_value=board),
              mock.patch.object(server, "fleet_history", self.journal()),
              mock.patch.object(server, "run_backlog", return_value={
                  "schemaVersion": 1, "task": report}) as backlog):
            snapshot = {"agents": [], "timestamp": 200000}
            first = server.fleet_inbox(config, snapshot, [])
            second = server.fleet_inbox(config, snapshot, [])
            self.assertEqual([i["text"] for i in first[0]], ["Which option?"])
            self.assertEqual(first, second)
            self.assertEqual(backlog.call_count, 1)

    def test_owner_questions_reuse_cached_board_without_listing_tasks(self):
        worktree = Path(self.tmp.name) / "app-task-1"
        worktree.mkdir()
        config = {"projects": [{"name": "app", "path": "/repo"}],
                  "worktreeRoot": self.tmp.name}
        board = {"projects": [{"name": "app", "tasks": [
            {"id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"]},
            {"id": "TASK-2", "status": "In Progress", "labels": []},
        ]}]}
        calls = []
        def backlog(args, cwd):
            calls.append((args, cwd))
            self.assertEqual(args, ["task", "view", "TASK-1", "--json"],
                             "Fleet owner questions must reuse the cached board, not list tasks")
            self.assertEqual(cwd, str(worktree))
            return {"schemaVersion": 1, "task": {
                "id": "TASK-1", "status": "In Progress", "finalSummary": None,
                "labels": ["needs-owner-approval"]}}
        with (
            mock.patch.object(server, "fleet_history", self.journal()),
            mock.patch.object(server, "get_board", return_value=board),
            mock.patch.object(server, "list_sessions", return_value=[]),
            mock.patch.object(server, "run_backlog", side_effect=backlog),
            mock.patch.object(server, "run_git", side_effect=AssertionError("unexpected git")),
            mock.patch.object(server, "read_delivery_log", return_value=([], 0)),
        ):
            result = server.get_fleet(config)
        self.assertEqual([(i["kind"], i["taskId"]) for i in result["needsYou"]],
                         [("owner", "TASK-1")])
        self.assertEqual(len(calls), 1)  # Only the labelled task's branch report.

    def test_ended_worker_report_still_excludes_owner_question(self):
        worktree = Path(self.tmp.name) / "app-task-1"
        worktree.mkdir()
        config = {"projects": [{"name": "app", "path": "/repo"}],
                  "worktreeRoot": self.tmp.name}
        board = {"projects": [{"name": "app", "tasks": [{
            "id": "TASK-1", "status": "In Progress", "labels": ["needs-owner-approval"]}]}]}
        def backlog(args, cwd):
            self.assertEqual(args, ["task", "view", "TASK-1", "--json"])
            self.assertEqual(cwd, str(worktree))
            return {"schemaVersion": 1, "task": {
                "id": "TASK-1", "status": "Done", "finalSummary": "Complete"}}
        with mock.patch.object(server, "get_board", return_value=board), \
             mock.patch.object(server, "fleet_history", self.journal()), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "run_backlog", side_effect=backlog), \
             mock.patch.object(server, "run_git", side_effect=AssertionError("unexpected git")), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)):
            result = server.get_fleet(config)
        self.assertEqual(result["needsYou"], [])

    def test_merge_signal_does_not_expire_with_timeline_window(self):
        journal = self.journal()
        journal.append("app", "TASK-1", "helper", "merge blocked", timestamp=190000,
                       harvest={"reason": "worktreeClean: dirty"})
        with mock.patch.object(server, "fleet_history", journal), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "read_delivery_log", return_value=([], 0)):
            result = server.get_fleet({"projects": [{"name": "app"}]}, window=10)
        self.assertEqual(result["history"], [])
        self.assertEqual([i["taskId"] for i in result["needsYou"]], ["TASK-1"])

    def test_inbox_delivery_recovery_expiry_and_latest_merge_attempt(self):
        journal = self.journal()
        for tid, state, stamp in [("TASK-1", "merge blocked", 199900),
                                  ("TASK-2", "merge blocked", 199900),
                                  ("TASK-2", "working", 199950)]:
            journal.append("app", tid, "helper", state, timestamp=stamp,
                           harvest={"reason": "worktreeClean: dirty"})
        rows = [
            {"project": "app", "task": "TASK-3", "time": "1970-01-03T07:30:00Z", "outcome": "dialog"},
            {"project": "app", "task": "task-3", "time": "1970-01-03T07:31:00Z", "outcome": "delivered"},
            {"project": "app", "task": "TASK-4", "time": "1970-01-01T00:00:01Z", "outcome": "no-echo"},
            {"project": "app", "task": "TASK-5", "time": "1970-01-03T07:30:00Z", "outcome": "no-echo"},
            {"project": "app", "task": "TASK-5", "time": "1970-01-03T07:31:00Z", "outcome": "dialog"},
        ]
        with mock.patch.object(server, "fleet_history", journal), \
             mock.patch.object(server, "list_sessions", return_value=[]), \
             mock.patch.object(server, "read_delivery_log", return_value=(rows, 0)):
            result = server.get_fleet({"projects": [{"name": "app"}]})
        items = result.get("needsYou", [])
        self.assertEqual([(i["kind"], i["taskId"]) for i in items],
                         [("merge", "TASK-1"), ("message", "TASK-5")])
        self.assertIn("worktreeClean", items[0]["signal"])
        self.assertIn("last", items[0]["signal"].lower())

    def test_append_reload_and_corrupt_lines(self):
        journal = self.journal()
        journal.append('app', 'task-1', 'helper', 'working')
        with self.path.open('ab') as stream:
            stream.write(b'null\n{"timestamp":"bad"}\n\xff\n{"truncated":')
        restored = self.journal()
        self.assertEqual(restored.snapshot()['skippedLines'], 4)
        restored.append('app', 'TASK-1', 'helper', 'waiting')
        rows = self.journal().snapshot()['history']
        self.assertEqual([r['state'] for r in rows], ['working', 'waiting'])
        self.assertEqual(rows[0], dict(project='app', taskId='TASK-1', agent='helper',
                                      state='working', timestamp=200000))

    def test_retention_prunes_disk_even_without_new_events(self):
        self.journal(now=1).append('app', 'TASK-1', 'codex', 'working')
        journal = self.journal(now=200000)
        self.assertEqual(journal.snapshot()['history'], [])
        self.assertEqual(self.path.read_text(), '')

    @settled_codex_pane()
    def test_hooks_deduplicate_and_keep_state_start(self):
        journal = self.journal(now=200100)
        with mock.patch.object(server, 'fleet_history', journal):
            server._reset_agent_events()
            self.addCleanup(server._reset_agent_events)
            with mock.patch.object(server.time, 'time', return_value=200001):
                server.record_agent_event('app', 'TASK-1', 'working', 'codex', agent_name='helper')
            with mock.patch.object(server.time, 'time', return_value=200010):
                server.record_agent_event('app', 'TASK-1', 'working', 'codex')
            self.assertEqual(server.get_fleet_lifecycle('app', 'TASK-1')['stateSince'], 200001)
            with mock.patch.object(server.time, 'time', return_value=200020):
                server.record_agent_event('app', 'TASK-1', 'finished', 'codex')
        self.assertEqual([r['state'] for r in journal.snapshot()['history']], ['working', 'idle'])
        self.assertEqual(journal.snapshot()['history'][-1]['agent'], 'helper')

    def test_snapshot_http_shape_window_cost_and_restart_unknown(self):
        journal = self.journal()
        journal.append('app', 'TASK-1', 'helper', 'working', timestamp=199000)
        journal.append('app', 'TASK-2', 'claude', 'merged', timestamp=199999)
        config = {'projects': [{'name': 'app', 'maxAgents': 3}, {'name': 'empty'}]}
        httpd = server.CentraleHTTPServer(('127.0.0.1', 0), server.Handler, config)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        delivery = Path(self.tmp.name) / 'deliveries.jsonl'
        delivery.write_text('\n'.join(json.dumps(x) for x in [
            {'project': 'app', 'task': 'TASK-1', 'outcome': 'dialog'},
            {'project': 'app', 'task': 'TASK-2', 'outcome': 'delivered'},
            {'project': 'other', 'outcome': 'no-echo'},
        ]))
        def request(query=''):
            conn = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            try:
                conn.request('GET', '/api/fleet' + query)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        server._reset_agent_events()
        with mock.patch.object(server, 'fleet_history', journal), \
             mock.patch.dict(os.environ, {'CENTRALE_DELIVERY_LOG': str(delivery)}), \
             mock.patch.object(server, 'run_tmux', return_value=subprocess.CompletedProcess([], 0,
                 'centrale-app-task-1\t100\t0\n', '')) as tmux, \
             mock.patch.object(server, 'run_git', side_effect=AssertionError('fleet must not run git')):
            status, data = request()
            self.assertEqual(status, 200)
            self.assertEqual(data['window'], 7200)
            self.assertEqual(data['projects'], [{'name': 'app', 'maxAgents': 3, 'agentCount': 1},
                                                {'name': 'empty', 'maxAgents': None, 'agentCount': 0}])
            self.assertEqual(data['agents'][0]['state'], 'unknown')
            self.assertIsNone(data['agents'][0]['stateSince'])
            self.assertEqual(data['agents'][0]['agent'], 'helper')
            self.assertEqual(len(data['merges']), 1)
            self.assertEqual(len(data['messages']), 1)
            # One list-sessions, plus one pane capture: this agent is unknown
            # and ancient, so it is a parked candidate (task-170.3). The
            # capture shows no dialog and the log row has no time, so it is
            # not parked.
            self.assertEqual(tmux.call_count, 2)
            self.assertIsNone(data['agents'][0]['parked'])
            status, data = request('?window=10')
            self.assertEqual(status, 200)
            self.assertEqual([r['state'] for r in data['history']], ['merged'])
            for invalid in ('0', '-1', 'nan', 'inf', '172801', 'abc', ''):
                self.assertEqual(request('?window=' + invalid)[0], 400)

    def test_failed_survey_does_not_end_live_session(self):
        journal = self.journal()
        with mock.patch.object(server, 'fleet_history', journal):
            server.record_session_started('app', 'TASK-1', 'helper')
            with mock.patch.object(server, 'run_tmux', return_value=subprocess.CompletedProcess(
                    [], 124, '', 'timed out')):
                with self.assertRaises(server.BacklogError):
                    server.get_fleet({'projects': [{'name': 'app'}]})
        self.assertEqual([r['state'] for r in journal.snapshot()['history']], ['spawn'])

    def test_stale_survey_cannot_end_new_session(self):
        import fleet
        journal = fleet.Journal(self.path)
        with mock.patch.object(server, 'fleet_history', journal):
            server.record_session_started('app', 'TASK-1', 'helper')
            server.record_agent_event('app', 'TASK-1', 'working', 'claude')
            server.observe_fleet_sessions([], {'projects': [{'name': 'app'}]}, surveyed_at=0)
            self.assertEqual(server.get_agent_state('app', 'TASK-1'), 'working')
            server.observe_fleet_sessions([], {'projects': [{'name': 'app'}]})
            self.assertEqual(server.get_agent_state('app', 'TASK-1'), 'unknown')
        self.assertEqual([r['state'] for r in journal.rows], ['spawn', 'working', 'session ended'])
        server._reset_agent_events()

    def test_invalid_timestamp_and_restart_disappearance(self):
        row = dict(project='app', taskId='TASK-1', agent='helper', state='spawn', timestamp=10**400)
        self.path.write_text(json.dumps(row) + '\n')
        journal = self.journal()
        self.assertEqual(journal.snapshot()['skippedLines'], 1)
        journal.append('app', 'TASK-1', 'helper', 'spawn')
        restored = self.journal()
        with mock.patch.object(server, 'fleet_history', restored):
            server.observe_fleet_sessions([], {'projects': [{'name': 'app'}]})
        self.assertEqual([r['state'] for r in restored.rows], ['spawn', 'session ended'])

    def test_bad_delivery_bytes_do_not_break_snapshot(self):
        delivery = Path(self.tmp.name) / 'deliveries.jsonl'
        delivery.write_bytes(b'\xff\n')
        with mock.patch.object(server, 'fleet_history', self.journal()), \
             mock.patch.dict(os.environ, {'CENTRALE_DELIVERY_LOG': str(delivery)}), \
             mock.patch.object(server, 'list_sessions', return_value=[]):
            snapshot = server.get_fleet({'projects': []})
        self.assertEqual(snapshot['agents'], [])
        self.assertTrue(snapshot['deliveryError'])

    def test_write_failure_is_visible_and_recovers(self):
        journal = self.journal()
        with mock.patch('pathlib.Path.open', side_effect=OSError('disk full')):
            journal.append('app', 'TASK-1', 'helper', 'working')
        self.assertIn('disk full', journal.snapshot()['historyError'])
        journal.append('app', 'TASK-1', 'helper', 'waiting')
        self.assertIsNone(journal.snapshot()['historyError'])
        self.assertEqual([r['state'] for r in self.journal().rows], ['working', 'waiting'])

    def test_harvest_records_failure_success_and_agent_identity(self):
        import harvest
        journal = self.journal(now=time.time())
        journal.append('unique-fleet-test', 'TASK-1', 'helper', 'finished')
        with mock.patch.object(server, 'fleet_history', journal):
            harvest._record_event('unique-fleet-test', {
                'taskId': 'TASK-1', 'gates': [{'name': 'clean', 'passed': False, 'reason': 'dirty'}]
            }, 'click')
            harvest._record_event('unique-fleet-test', {'taskId': 'TASK-1', 'merged': True}, 'click')
        self.assertEqual([r['state'] for r in journal.rows], ['finished', 'merge blocked', 'merged'])
        self.assertEqual(journal.rows[-1]['agent'], 'helper')

    def test_spawn_and_resume_record_success_only_with_configured_name(self):
        import fleet
        import spawn
        from test_spawn import FakeGit, make_config, task_view, backlog_raw_proc
        config = make_config('/worktrees', [{'name': 'my-app', 'path': '/repos/my-app'}])
        config['agents'] = server.normalize_agents_map({'helper': ['claude']})
        config['defaultAgent'] = 'helper'
        journal = fleet.Journal()
        fake_git = FakeGit(branch_exists=True, worktree_registered=True,
                           wt_dir='/worktrees/my-app-task-2')
        with mock.patch.object(server, 'fleet_history', journal), \
             mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.object(server, 'run_git', side_effect=fake_git), \
             mock.patch.object(server, 'run_tmux', return_value=subprocess.CompletedProcess([], 0, '', '')) as tmux, \
             mock.patch.object(server, 'list_sessions', return_value=[]), \
             mock.patch.object(server, 'run_backlog', return_value=task_view(['@helper'])), \
             mock.patch.object(server, 'run_backlog_raw', return_value=backlog_raw_proc()), \
             mock.patch('os.path.isdir', return_value=True), mock.patch('os.makedirs'):
            os.environ.pop('CENTRALE_SPAWN_CMD', None)
            spawn.spawn(config, 'my-app', 'TASK-2')
            spawn.resume(config, 'my-app', 'TASK-2')
            tmux.return_value = subprocess.CompletedProcess([], 1, '', 'failure')
            with self.assertRaises(spawn.SpawnError):
                spawn.spawn(config, 'my-app', 'TASK-2')
        self.assertEqual([(r['state'], r['agent']) for r in journal.rows],
                         [('spawn', 'helper'), ('session ended', 'helper'),
                          ('spawn', 'helper'), ('session ended', 'helper')])
        server._reset_agent_events()

    def test_explicit_end_records_after_successful_kill(self):
        import fleet
        journal = fleet.Journal()
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(centrale_config={
            'projects': [{'name': 'app'}], 'worktreeRoot': self.tmp.name})
        handler._read_json_body = lambda: {'project': 'app', 'taskId': 'TASK-1'}
        handler._send_json = mock.Mock()
        handler._send_error_json = mock.Mock()
        with mock.patch.object(server, 'fleet_history', journal), \
             mock.patch.object(server, 'list_sessions', return_value=[{'name': 'centrale-app-task-1'}]), \
             mock.patch.object(server, 'run_tmux', return_value=subprocess.CompletedProcess([], 0, '', '')), \
             mock.patch.object(server, 'kill_pids_with_cwd_under', return_value=[]):
            server.record_agent_event('app', 'TASK-1', 'waiting', 'claude', 'helper')
            server.record_agent_event('app', 'TASK-1', 'finished', 'claude')
            handler._handle_end_session()
            self.assertEqual(server.get_agent_state('app', 'TASK-1'), 'unknown')
        self.assertEqual([r['state'] for r in journal.rows], ['waiting', 'finished', 'session ended'])
        self.assertEqual(journal.rows[-1]['agent'], 'helper')
        handler._send_json.assert_called_once_with(200, {
            'ok': True, 'session': 'centrale-app-task-1', 'killedOrphans': []})

    def test_survey_started_before_end_cannot_resurrect_observation(self):
        import fleet
        journal = fleet.Journal()
        config = {'projects': [{'name': 'app'}]}
        seen = [{'name': 'centrale-app-task-1', 'created': '100'}]
        with mock.patch.object(server, 'fleet_history', journal):
            server.observe_fleet_sessions(seen, config)
            before_end = time.monotonic()
            server.record_session_ended('app', 'TASK-1')
            server.observe_fleet_sessions(seen, config, before_end)
            server.observe_fleet_sessions([], config)
        self.assertEqual([r['state'] for r in journal.rows], ['session ended'])

    def test_poll_and_explicit_end_record_one_transition(self):
        import fleet
        journal = fleet.Journal()
        config = {'projects': [{'name': 'app'}]}
        with mock.patch.object(server, 'fleet_history', journal):
            server.record_session_started('app', 'TASK-1', 'helper')
            server.observe_fleet_sessions([], config)  # kill succeeded; handler is still returning
            server.record_session_ended('app', 'TASK-1')
        self.assertEqual([r['state'] for r in journal.rows], ['spawn', 'session ended'])

    def test_poll_during_launch_does_not_clear_new_hook(self):
        import fleet
        journal = fleet.Journal()
        config = {'projects': [{'name': 'app'}]}
        with mock.patch.object(server, 'fleet_history', journal):
            server.record_session_started('app', 'TASK-1', 'helper')
            server.clear_agent_event('app', 'TASK-1', agent_name='helper')
            server.record_agent_event('app', 'TASK-1', 'working', 'claude')
            server.observe_fleet_sessions([], config)  # sampled while tmux was creating the session
            server.record_session_started('app', 'TASK-1', 'helper')
            self.assertEqual(server.get_agent_state('app', 'TASK-1'), 'working')
        server._reset_agent_events()
