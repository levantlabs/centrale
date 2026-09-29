"""Real git/backlog coverage of the opt-in project setup (task-175)."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

from tests_integration import base
import server
import settings


def tearDownModule():
    base.assert_test_tmux_footprint_gone()


@base.require_tools("git", "backlog")
class ProjectSetupTests(base.IntegrationCase):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp_dir) / "project"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Integration Test")
        self.git("config", "user.email", "integration@example.invalid")
        (self.repo / "unrelated.txt").write_text("original\n")
        self.commit_all()
        self.project = {"name": "scratch", "path": str(self.repo)}
        self.config = {"port": 17420, "projects": [self.project]}

    def git(self, *args):
        return base.run(["git", *args], cwd=self.repo).stdout

    def commit_all(self):
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")

    def seed_backlog(self):
        base.run(["backlog", "init", "scratch", "--defaults", "--integration-mode", "cli",
                  "--agent-instructions", "claude,agents"], cwd=self.repo)

    def seed_pointer(self):
        for name in ("CLAUDE.md", "AGENTS.md"):
            path = self.repo / name
            prefix = path.read_bytes() if path.exists() else b""
            path.write_bytes(prefix + settings.agent_pointer(self.config))

    def test_all_four_states_and_noop_rerun(self):
        # Wrong early return when Backlog exists, or unconditionally running
        # init, breaks one of these real-repository starting states.
        for backlog_present, pointer_present in ((False, False), (True, False),
                                                  (False, True), (True, True)):
            with self.subTest(backlog=backlog_present, pointer=pointer_present):
                self.git("reset", "--hard", self.git("rev-list", "--max-parents=0", "HEAD").strip())
                self.git("clean", "-fd")
                if backlog_present:
                    self.seed_backlog()
                if pointer_present:
                    self.seed_pointer()
                if backlog_present or pointer_present:
                    self.commit_all()
                before = self.git("rev-parse", "HEAD")
                result = settings.setup_project(self.config, self.project)
                self.assertTrue((self.repo / "backlog/config.yml").is_file())
                for name in ("CLAUDE.md", "AGENTS.md"):
                    data = (self.repo / name).read_bytes()
                    self.assertEqual(data.count(b"<!-- CENTRALE GUIDELINES START -->"), 1)
                    self.assertIn(b"http://127.0.0.1:17420/api/agent-guide", data)
                self.assertEqual(self.git("status", "--porcelain"), "")
                if backlog_present and pointer_present:
                    self.assertEqual(self.git("rev-parse", "HEAD"), before)
                    self.assertEqual(result["writtenFiles"], [])
                else:
                    committed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
                    self.assertEqual(sorted(result["writtenFiles"]), sorted(committed))
                before = self.git("rev-parse", "HEAD")
                contents = {name: (self.repo / name).read_bytes() for name in ("CLAUDE.md", "AGENTS.md")}
                self.assertEqual(settings.setup_project(self.config, self.project)["writtenFiles"], [])
                self.assertEqual(self.git("rev-parse", "HEAD"), before)
                self.assertEqual(contents, {name: (self.repo / name).read_bytes() for name in contents})

    def test_scoped_commit_preserves_other_staged_and_unstaged_changes(self):
        # Both the init and pointer-only routes must exclude unrelated staging.
        for backlog_present in (False, True):
            with self.subTest(backlog=backlog_present):
                self.git("reset", "--hard", self.git("rev-list", "--max-parents=0", "HEAD").strip())
                self.git("clean", "-fd")
                if backlog_present:
                    self.seed_backlog()
                    self.commit_all()
                (self.repo / "unrelated.txt").write_text("staged\n")
                self.git("add", "unrelated.txt")
                (self.repo / "unrelated.txt").write_text("unstaged\n")
                (self.repo / "untracked.txt").write_text("also untouched\n")
                staged = self.git("diff", "--cached", "--binary")
                unstaged = self.git("diff", "--binary")
                settings.setup_project(self.config, self.project)
                self.assertEqual(self.git("diff", "--cached", "--binary"), staged)
                self.assertEqual(self.git("diff", "--binary"), unstaged)
                self.assertEqual((self.repo / "untracked.txt").read_text(), "also untouched\n")
                self.assertNotIn("unrelated.txt", self.git("show", "--format=", "--name-only", "HEAD"))

    def test_replacement_preserves_every_byte_outside_markers(self):
        self.seed_backlog()
        prefix, suffix = b"owner\r\n\xff\r\n", b"\r\nTAIL without newline\xfe"
        for name in ("CLAUDE.md", "AGENTS.md"):
            (self.repo / name).write_bytes(prefix + b"<!-- CENTRALE GUIDELINES START -->\r\nold\r\n<!-- CENTRALE GUIDELINES END -->" + suffix)
        self.commit_all()
        settings.setup_project(self.config, self.project)
        for name in ("CLAUDE.md", "AGENTS.md"):
            data = (self.repo / name).read_bytes()
            self.assertEqual(data.split(b"<!-- CENTRALE GUIDELINES START -->")[0], prefix)
            self.assertEqual(data.split(b"<!-- CENTRALE GUIDELINES END -->")[1], suffix)
            self.assertEqual(data.count(b"<!-- CENTRALE GUIDELINES START -->"), 1)

    def test_refuses_dirty_target_without_writing_anything(self):
        self.seed_backlog()
        self.commit_all()
        with (self.repo / "CLAUDE.md").open("ab") as f:
            f.write(b"owner change\n")
        before = {name: (self.repo / name).read_bytes() for name in ("CLAUDE.md", "AGENTS.md")}
        with self.assertRaises(settings.SettingsError) as raised:
            settings.setup_project(self.config, self.project)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(before, {name: (self.repo / name).read_bytes() for name in before})

    def test_existing_project_setup_over_http_and_guide(self):
        httpd = server.CentraleHTTPServer(("127.0.0.1", 0), server.Handler, self.config)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            httpd.shutdown()
            httpd.server_close()
            thread.join(5)
            self.assertFalse(base.port_listening(httpd.server_address[1]))
        self.addCleanup(cleanup)
        origin = f"http://127.0.0.1:{httpd.server_address[1]}"
        request = urllib.request.Request(origin + "/api/setup-project", method="POST",
                                         data=b'{"project":"scratch"}',
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request)
        except urllib.error.HTTPError as error:
            self.fail(f"setup returned {error.code}: {error.read()!r}")
        with response:
            result = json.load(response)
        self.assertEqual(result["project"], "scratch")
        self.assertIn((origin + "/api/agent-guide").encode(), (self.repo / "AGENTS.md").read_bytes())
        with urllib.request.urlopen(origin + "/api/agent-guide") as response:
            self.assertEqual(response.headers.get_content_type(), "text/plain")
            self.assertIn(b"/api/spawn", response.read())
        self.assertEqual(self.git("status", "--porcelain"), "")
