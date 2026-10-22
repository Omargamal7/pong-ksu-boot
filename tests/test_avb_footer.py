"""AVB footer copying against the pinned avbtool (set AVBTOOL to its path)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from add_lunaris_avb_footer import add_footer, load_avbtool, read_footer

AVBTOOL = os.environ.get("AVBTOOL")
PARTITION = 1024 * 1024
SALT = "4f81eae16570be86d67e0ddff1d60b9322fc3dd6033eac08a10d2a874bd1b686"
FINGERPRINT = "Nothing/lineage_Pong/Pong:16/BP2A.250805.005/eng.androi.20251209.055610:user/release-keys"


@unittest.skipUnless(AVBTOOL, "AVBTOOL is not set; the build runs these after fetching the pinned avbtool")
class AvbFooterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.avbtool = Path(AVBTOOL)
        self.base = self.root / "base.img"
        self.base.write_bytes(b"original payload" * 1024)
        self.footer(self.base, "--algorithm", "NONE", "--salt", SALT,
                    "--prop", "com.android.build.boot.os_version:16",
                    "--prop", f"com.android.build.boot.fingerprint:{FINGERPRINT}")
        self.image = self.root / "compact.img"
        self.image.write_bytes(b"rebuilt payload!" * 1100)
        self.output = self.root / "dist/boot.img"

    def footer(self, path, *extra):
        subprocess.run([sys.executable, str(self.avbtool), "add_hash_footer", "--image", str(path),
                        "--partition_size", str(PARTITION), "--partition_name", "boot", *extra],
                       check=True, capture_output=True)

    def test_new_footer_preserves_original_fields(self):
        report = add_footer(self.base, self.image, self.avbtool, self.output, PARTITION)
        original, new = report["original_footer"], report["new_footer"]
        for key in report["preserved_fields"]:
            self.assertEqual(new[key], original[key], key)
        self.assertEqual(new["props"][1][1], FINGERPRINT)
        self.assertEqual(new["hash_image_size"], self.image.stat().st_size)
        self.assertNotEqual(new["digest"], original["digest"])
        self.assertTrue(report["original_footer_digest_matches_its_payload"])
        data = self.output.read_bytes()
        self.assertEqual(len(data), PARTITION)
        self.assertEqual(data[:self.image.stat().st_size], self.image.read_bytes())
        verify = subprocess.run([sys.executable, str(self.avbtool), "verify_image", "--image", str(self.output)],
                                capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_stale_original_digest_is_reported_not_trusted(self):
        data = bytearray(self.base.read_bytes())
        data[0] ^= 1
        self.base.write_bytes(data)
        report = add_footer(self.base, self.image, self.avbtool, self.output, PARTITION)
        self.assertFalse(report["original_footer_digest_matches_its_payload"])

    def test_original_without_footer_rejected(self):
        self.base.write_bytes(b"no footer" * 100)
        with self.assertRaisesRegex(ValueError, "no AVB footer"):
            add_footer(self.base, self.image, self.avbtool, self.output, PARTITION)

    def test_wrong_partition_size_rejected(self):
        with self.assertRaisesRegex(ValueError, "Pong boot partition"):
            add_footer(self.base, self.image, self.avbtool, self.output, PARTITION * 2)

    def test_payload_that_fills_partition_rejected(self):
        self.image.write_bytes(bytes(PARTITION))
        with self.assertRaisesRegex(ValueError, "no room"):
            add_footer(self.base, self.image, self.avbtool, self.output, PARTITION)
        self.assertFalse(self.output.exists())

    def test_reader_rejects_extra_descriptor_types(self):
        self.footer(self.base, "--algorithm", "NONE", "--salt", SALT, "--kernel_cmdline", "quiet")
        with self.assertRaisesRegex(ValueError, "exactly one hash descriptor"):
            read_footer(load_avbtool(self.avbtool), self.base)


if __name__ == "__main__":
    unittest.main()
