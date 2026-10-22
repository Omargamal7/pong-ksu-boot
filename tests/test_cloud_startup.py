"""A duplicate Codespace launch must preserve the active build's diagnostics."""

import fcntl
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class CloudStartupTests(unittest.TestCase):
    def test_background_relaunch_keeps_running_build_log_and_pid(self):
        source = Path(__file__).resolve().parents[1] / "scripts/build_lunaris_cloud.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            runner = root / "scripts/build_lunaris_cloud.sh"
            shutil.copyfile(source, runner)
            work = root / "work/cloud"
            work.mkdir(parents=True)
            (work / "session.log").write_text("active build diagnostics\n")
            (work / "build.pid").write_text("12345\n")
            with (work / "build.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["bash", str(runner), "--background"],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already holds", result.stderr)
                self.assertEqual((work / "session.log").read_text(), "active build diagnostics\n")
                self.assertEqual((work / "build.pid").read_text(), "12345\n")


if __name__ == "__main__":
    unittest.main()
