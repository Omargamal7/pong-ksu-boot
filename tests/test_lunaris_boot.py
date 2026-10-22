"""Boot payload bounds and metadata preservation checks."""

import gzip
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lunaris_boot import HEADER_SIZE, PAGE_SIZE, inspect_boot, sha256
from repack_lunaris_boot import validate_repack


def boot_image(kernel_extra=b"", ramdisk=b"original ramdisk", os_version=123):
    kernel = bytearray(64)
    kernel[56:60] = b"ARM\x64"
    kernel += b"Linux version 5.10.237-Meteoric (test)\0"
    kernel += b"IKCFG_ST" + gzip.compress(b"CONFIG_MODVERSIONS=y\n") + b"IKCFG_ED"
    kernel += kernel_extra
    header = bytearray(PAGE_SIZE)
    header[:8] = b"ANDROID!"
    struct.pack_into("<4I", header, 8, len(kernel), len(ramdisk), os_version, HEADER_SIZE)
    struct.pack_into("<I", header, 40, 4)
    image = header + kernel
    image += bytes((-len(image)) % PAGE_SIZE)
    image += ramdisk
    return bytes(image), bytes(kernel)


class BootTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.base = self.root / "base.img"
        self.output = self.root / "output.img"
        self.kernel = self.root / "Image"
        self.base.write_bytes(boot_image()[0])

    def test_payload_extraction(self):
        metadata, kernel, ramdisk = inspect_boot(self.base)
        self.assertEqual(ramdisk, b"original ramdisk")
        self.assertEqual(metadata["kernel_sha256"], sha256(kernel))

    def test_truncated_payload_rejected(self):
        self.base.write_bytes(self.base.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, "beyond"):
            inspect_boot(self.base)

    def test_stale_signature_rejected(self):
        data = bytearray(self.base.read_bytes())
        struct.pack_into("<I", data, 1580, 4096)
        self.base.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "signature"):
            inspect_boot(self.base)

    def validate(self, image, kernel, size=100663296):
        self.output.write_bytes(image)
        self.kernel.write_bytes(kernel)
        with mock.patch("repack_lunaris_boot.verified_original", side_effect=inspect_boot):
            return validate_repack(self.base, self.kernel, self.output, size)

    def test_kernel_only_replacement_accepted(self):
        metadata = self.validate(*boot_image(b"KernelSU Next"))
        self.assertEqual(metadata["kernel_release"], "5.10.237-Meteoric")

    def test_ramdisk_change_rejected(self):
        with self.assertRaisesRegex(ValueError, "header|ramdisk"):
            self.validate(*boot_image(b"Next", ramdisk=b"modified ramdisk"))

    def test_os_metadata_change_rejected(self):
        with self.assertRaisesRegex(ValueError, "header"):
            self.validate(*boot_image(b"Next", os_version=124))

    def test_partition_overflow_rejected(self):
        with self.assertRaisesRegex(ValueError, "partition"):
            self.validate(*boot_image(b"Next"), size=10)

    def test_unchanged_kernel_rejected(self):
        with self.assertRaisesRegex(ValueError, "identical"):
            self.validate(*boot_image())


if __name__ == "__main__":
    unittest.main()
