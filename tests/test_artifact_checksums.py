"""Persistent builds must refresh checksums without hashing their old manifest."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ArtifactChecksumTests(unittest.TestCase):
    def test_repeated_publication_has_current_checksums_and_no_self_entry(self):
        script = Path(__file__).resolve().parents[1] / "scripts/write_artifact_checksums.py"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "boot image.img").write_bytes(b"first boot candidate")
            (directory / "manager.apk").write_bytes(b"matching manager")
            (directory / "SHA256SUMS").write_text("stale previous checksums\n")
            (directory / ".SHA256SUMS-interrupted.tmp").write_text("interrupted generation")
            command = [sys.executable, str(script), "--directory", str(directory)]
            subprocess.run(command, check=True, capture_output=True)
            first = (directory / "SHA256SUMS").read_text()
            (directory / "boot image.img").write_bytes(b"second boot candidate")
            subprocess.run(command, check=True, capture_output=True)
            latest = (directory / "SHA256SUMS").read_text()
            self.assertNotEqual(first, latest)
            self.assertEqual(len(latest.splitlines()), 2)
            self.assertNotIn("SHA256SUMS", latest)
            check = subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=directory,
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)


if __name__ == "__main__":
    unittest.main()
