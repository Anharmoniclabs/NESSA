import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentharness.dev import DevProcessManager
from agentharness.workspace import Workspace


@unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "requires /proc")
class DevRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        project = self.tmp / "project"; project.mkdir()
        (project / "app.py").write_text("pass\n")
        self.ws = Workspace.create(project, self.tmp / "work")
        self.evidence = self.tmp / "evidence"

    def test_restart_reconciles_surviving_process(self):
        first = DevProcessManager(self.ws, self.evidence)
        first.start("server", [sys.executable, "-u", "-c",
                              "import time; print('ready'); time.sleep(30)"])
        pid = first.processes["server"].pid
        identity = first.processes["server"].identity
        second = DevProcessManager(self.ws, self.evidence)
        self.assertIsNone(second.processes["server"].proc)
        self.assertIn("running/reconciled", second.status("server"))
        self.assertIn("ready", second.logs("server"))
        self.assertIn("stopped", second.stop("server"))
        deadline = time.time() + 2
        while time.time() < deadline and second._alive(pid, identity):
            time.sleep(.05)
        self.assertFalse(second._alive(pid, identity))

    def test_identity_mismatch_is_not_owned(self):
        manager = DevProcessManager(self.ws, self.evidence)
        manager.start("server", [sys.executable, "-c", "import time; time.sleep(30)"])
        manager.stop("server")
        path = self.evidence / "dev" / "processes.json"
        state = json.loads(path.read_text())
        row = state["processes"][0]
        row.update(pid=os.getpid(), identity="not-this-process", returncode=None)
        path.write_text(json.dumps(state))
        recovered = DevProcessManager(self.ws, self.evidence)
        self.assertNotIn("running", recovered.status("server"))
        self.assertIn("already exited", recovered.stop("server"))

    def test_state_has_version_and_identity(self):
        manager = DevProcessManager(self.ws, self.evidence)
        manager.start("short", [sys.executable, "-c", "print('ok')"])
        state = json.loads((self.evidence / "dev" / "processes.json").read_text())
        self.assertEqual(state["version"], 2)
        self.assertIn("identity", state["processes"][0])


if __name__ == "__main__":
    unittest.main()
