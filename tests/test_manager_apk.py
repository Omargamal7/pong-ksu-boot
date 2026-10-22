"""The manager APK check must accept and reject what the kernel's apk_sign.c does."""

import hashlib
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_manager_apk import V2_BLOCK_ID, VERITY_PADDING_ID, signing_certificate

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_manager_apk.py"
CERT = bytes(range(256)) * 3 + b"cert"


def lp(payload: bytes) -> bytes:
    return struct.pack("<I", len(payload)) + payload


def v2_block(cert: bytes = CERT) -> bytes:
    signed_data = lp(b"") + lp(lp(cert))
    return lp(lp(lp(signed_data)))


def apk(*pairs: tuple[int, bytes]) -> bytes:
    body = b"".join(struct.pack("<Q", 4 + len(p)) + struct.pack("<I", i) + p for i, p in pairs)
    size = len(body) + 8 + 16
    block = struct.pack("<Q", size) + body + struct.pack("<Q", size) + b"APK Sig Block 42"
    entries, central = b"\0" * 0x20, b"CD"
    offset = len(entries) + len(block)
    eocd = b"PK\x05\x06" + bytes(8) + struct.pack("<II", len(central), offset) + b"\0\0"
    return entries + block + central + eocd


class ManagerApkTests(unittest.TestCase):
    def check(self, data, accept):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.apk"
            path.write_bytes(data)
            return subprocess.run([sys.executable, str(SCRIPT), "--apk", str(path), "--accept", accept],
                                  capture_output=True, text=True)

    def test_matching_v2_certificate_is_accepted(self):
        accept = f"{len(CERT):#x}:{hashlib.sha256(CERT).hexdigest()}"
        result = self.check(apk((V2_BLOCK_ID, v2_block()), (VERITY_PADDING_ID, b"\0" * 8)), accept)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_other_certificate_is_rejected(self):
        accept = f"{len(CERT):#x}:{'0' * 64}"
        self.assertEqual(self.check(apk((V2_BLOCK_ID, v2_block())), accept).returncode, 1)

    def test_v3_block_is_rejected_like_the_kernel(self):
        with self.assertRaisesRegex(ValueError, "0xf05368c0"):
            signing_certificate(apk((V2_BLOCK_ID, v2_block()), (0xF05368C0, b"v3")))

    def test_exactly_one_v2_block_is_required(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            signing_certificate(apk((V2_BLOCK_ID, v2_block()), (V2_BLOCK_ID, v2_block())))

    def test_first_certificate_is_returned(self):
        self.assertEqual(signing_certificate(apk((V2_BLOCK_ID, v2_block()))), CERT)


if __name__ == "__main__":
    unittest.main()
