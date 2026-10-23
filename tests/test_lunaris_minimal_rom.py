import hashlib
import io
import lzma
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import lunaris_payload, lunaris_rom_profile


class LunarisRomProfileTests(unittest.TestCase):
    def test_update_build_prop_preserves_order_and_appends_missing(self) -> None:
        original = (
            "# Header comment\n"
            "ro.lunaris.version=3.6\n"
            "dalvik.vm.heapstartsize=24m\n"
            "ro.keep.me=true\n"
        )
        updated = lunaris_rom_profile.update_build_prop_text(
            original,
            {
                "dalvik.vm.heapstartsize": "8m",
                "ro.lmk.thrashing_limit": "30",
            },
        )
        lines = updated.splitlines()
        self.assertIn("# Header comment", lines)
        self.assertIn("dalvik.vm.heapstartsize=8m", lines)
        self.assertNotIn("dalvik.vm.heapstartsize=24m", lines)
        self.assertIn("ro.keep.me=true", lines)
        self.assertIn("ro.lmk.thrashing_limit=30", lines)

    def test_write_in_place_preserves_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "build.prop"
            p.write_text("old=1\n", encoding="utf-8")
            ino_before = p.stat().st_ino
            lunaris_rom_profile.write_in_place(p, b"new=2\n")
            ino_after = p.stat().st_ino
            self.assertEqual(ino_before, ino_after)
            self.assertEqual(p.read_text(encoding="utf-8"), "new=2\n")

    def test_strip_vintf_manifest_hals_removes_targeted_only(self) -> None:
        sample_xml = """<manifest version="1.0" type="device">
    <hal format="hidl">
        <name>vendor.qti.hardware.eid</name>
        <transport>hwbinder</transport>
        <version>1.0</version>
    </hal>
    <hal format="aidl">
        <name>android.hardware.graphics.composer3</name>
        <version>2</version>
    </hal>
    <hal format="hidl">
        <name>vendor.qti.hardware.wifidisplaysession</name>
        <version>1.0</version>
    </hal>
</manifest>
"""
        cleaned, removed = lunaris_rom_profile.strip_vintf_manifest_hals(
            sample_xml,
            lunaris_rom_profile.VENDOR_MANIFEST_REMOVE_HALS,
        )
        self.assertEqual(
            removed,
            ["vendor.qti.hardware.eid", "vendor.qti.hardware.wifidisplaysession"],
        )
        self.assertNotIn("vendor.qti.hardware.eid", cleaned)
        self.assertNotIn("vendor.qti.hardware.wifidisplaysession", cleaned)
        self.assertIn("android.hardware.graphics.composer3", cleaned)

    def test_patch_vbmeta_flags(self) -> None:
        raw = bytearray(256)
        raw[:4] = b"AVB0"
        patched = lunaris_rom_profile.patch_vbmeta_flags(bytes(raw), flags=3)
        flags_val = struct.unpack_from(">I", patched, 120)[0]
        self.assertEqual(flags_val, 3)

        with self.assertRaises(ValueError):
            lunaris_rom_profile.patch_vbmeta_flags(b"BAD!", flags=3)

    def test_protected_paths_cannot_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gms = root / "priv-app/PrebuiltGmsCore"
            gms.mkdir(parents=True)
            with self.assertRaises(ValueError):
                lunaris_rom_profile.remove_tree_or_file(root, "priv-app/PrebuiltGmsCore")

    def test_apply_partition_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            # 1. Product partition test
            prod = base / "product"
            (prod / "priv-app/Velvet").mkdir(parents=True)
            (prod / "priv-app/Velvet/Velvet.apk").write_bytes(b"x" * 1024)
            (prod / "priv-app/PrebuiltGmsCore").mkdir(parents=True)
            (prod / "priv-app/PrebuiltGmsCore/GmsCore.apk").write_bytes(b"g" * 1024)
            (prod / "overlay").mkdir(parents=True)
            (prod / "overlay/AuroraSettingsOverlay.apk").write_bytes(b"f" * 128)
            (prod / "overlay/NothingAsWebcamResOverlay.apk").write_bytes(b"w" * 128)
            (prod / "etc").mkdir(parents=True)
            (prod / "etc/build.prop").write_text("ro.product.test=1\n", encoding="utf-8")

            rep_prod = lunaris_rom_profile.apply_partition_profile("product", prod)
            self.assertFalse((prod / "priv-app/Velvet").exists())
            self.assertTrue((prod / "priv-app/PrebuiltGmsCore/GmsCore.apk").is_file())
            self.assertFalse((prod / "overlay/AuroraSettingsOverlay.apk").exists())
            self.assertTrue((prod / "overlay/NothingAsWebcamResOverlay.apk").is_file())
            self.assertIn("ro.opa.eligible_device=false", (prod / "etc/build.prop").read_text())
            self.assertGreaterEqual(rep_prod["removed_count"], 2)

            # 2. System partition test
            sys_dir = base / "system"
            (sys_dir / "system/etc/init").mkdir(parents=True)
            (sys_dir / "system/etc/init/atrace.rc").write_text("service atrace\n", encoding="utf-8")
            (sys_dir / "system/build.prop").write_text(
                "ro.lunaris.version=3.6\ndalvik.vm.madvise.vdexfile.size=104857600\n",
                encoding="utf-8",
            )
            lunaris_rom_profile.apply_partition_profile("system", sys_dir)
            self.assertFalse((sys_dir / "system/etc/init/atrace.rc").exists())
            rc_file = sys_dir / "system/etc/init/init.lunaris.minimal.rc"
            self.assertTrue(rc_file.is_file())
            self.assertIn("stop vendor.diag-router", rc_file.read_text(encoding="utf-8"))
            self.assertIn(
                "dalvik.vm.madvise.vdexfile.size=0",
                (sys_dir / "system/build.prop").read_text(encoding="utf-8"),
            )


class LunarisPayloadTests(unittest.TestCase):
    def test_varint_and_proto_roundtrip(self) -> None:
        fields = [
            (1, 0, 4096),
            (2, 2, b"test_bytes"),
            (3, 5, struct.pack("<I", 256)),
        ]
        raw = lunaris_payload.serialize_proto_fields(fields)
        parsed = lunaris_payload.parse_proto_fields(raw)
        self.assertEqual(parsed, fields)

    def test_encode_partition_image_zero_and_xz_check_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "test.img"
            blob_path = Path(tmp) / "blobs.bin"
            # 2 chunks of 2 MiB: first chunk all zeros, second chunk non-zero
            chunk_bytes = lunaris_payload.CHUNK_BLOCKS * lunaris_payload.BLOCK_SIZE
            data = (b"\x00" * chunk_bytes) + (b"A" * chunk_bytes)
            img_path.write_bytes(data)

            with open(blob_path, "wb") as bf:
                size, sha, ops_proto, next_offset, cow_est = lunaris_payload.encode_partition_image(
                    img_path, bf, 0, max_workers=2
                )

            blob_bytes = blob_path.read_bytes()
            self.assertEqual(size, len(data))
            self.assertEqual(sha, hashlib.sha256(data).digest())
            self.assertEqual(len(ops_proto), 2)
            self.assertEqual(next_offset, len(blob_bytes))
            self.assertEqual(cow_est, len(blob_bytes))

            op0 = dict((k, v) for k, _, v in lunaris_payload.parse_proto_fields(ops_proto[0]))
            op1 = dict((k, v) for k, _, v in lunaris_payload.parse_proto_fields(ops_proto[1]))
            self.assertEqual(op0[1], lunaris_payload.OP_ZERO)
            self.assertNotIn(2, op0)
            self.assertEqual(op1[1], lunaris_payload.OP_REPLACE_XZ)
            self.assertEqual(op1[2], 0)
            self.assertEqual(op1[3], len(blob_bytes))

            # Verify XZ stream header uses CHECK_NONE (byte 7 == 0x00)
            self.assertEqual(blob_bytes[:6], b"\xfd7zXZ\x00")
            self.assertEqual(blob_bytes[6:8], b"\x00\x00")
            self.assertEqual(lzma.decompress(blob_bytes), b"A" * chunk_bytes)

    def test_extract_and_repack_synthetic_ota_zip(self) -> None:
        n = (1 << 2047) + 1
        d = 1
        digest = hashlib.sha256(b"hello").digest()
        sig_proto = lunaris_payload.sign_crau_digest(digest, n, d)
        self.assertEqual(len(sig_proto), 267)

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            part_a_orig = b"A" * 4096 + b"\x00" * 4096
            part_b_orig = b"B" * 8192

            part_a_file = tdir / "part_a.orig.img"
            part_b_file = tdir / "part_b.orig.img"
            part_a_file.write_bytes(part_a_orig)
            part_b_file.write_bytes(part_b_orig)

            blobs_file = tdir / "init_blobs.bin"
            with open(blobs_file, "wb") as bf:
                sz_a, hash_a, ops_a, off_a, cow_a = lunaris_payload.encode_partition_image(
                    part_a_file, bf, 0, max_workers=1
                )
                sz_b, hash_b, ops_b, off_b, cow_b = lunaris_payload.encode_partition_image(
                    part_b_file, bf, off_a, max_workers=1
                )
            data_blobs = blobs_file.read_bytes()

            pu_a = lunaris_payload.build_partition_update_proto(
                "part_a", [], sz_a, hash_a, ops_a, cow_a
            )
            pu_b = lunaris_payload.build_partition_update_proto(
                "part_b", [], sz_b, hash_b, ops_b, cow_b
            )
            manifest = lunaris_payload.serialize_proto_fields([
                (3, 0, 4096),
                (4, 0, len(data_blobs)),
                (5, 0, 267),
                (12, 0, 1765259768),
                (13, 2, pu_a),
                (13, 2, pu_b),
            ])
            hdr = struct.pack(">4sQQI", b"CrAU", 2, len(manifest), 267)
            meta_sig = lunaris_payload.sign_crau_digest(hashlib.sha256(hdr + manifest).digest(), n, d)
            pay_sig = lunaris_payload.sign_crau_digest(
                hashlib.sha256(hdr + manifest + data_blobs).digest(), n, d
            )

            src_zip = tdir / "source.zip"
            with zipfile.ZipFile(src_zip, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr(
                    "META-INF/com/android/metadata",
                    "ota-property-files=" + ("x" * 200) + "\n"
                    "ota-streaming-property-files=" + ("y" * 200) + "\n"
                    "ota-type=AB\npost-Device=Pong\n",
                )
                zf.writestr("META-INF/com/android/metadata.pb", b"\x08\x01")
                zf.writestr("apex_info.pb", b"\x08\x02")
                zf.writestr("care_map.pb", b"\x08\x03")
                zf.writestr("payload.bin", hdr + manifest + meta_sig + data_blobs + pay_sig)
                zf.writestr("payload_properties.txt", b"FILE_HASH=abc\n")
                zf.writestr("META-INF/com/android/otacert", b"-----BEGIN CERTIFICATE-----\n")

            # Extract part_a from source.zip
            ext_dir = tdir / "extracted"
            lunaris_payload.extract_partitions_from_zip(src_zip, {"part_a"}, ext_dir)
            self.assertEqual((ext_dir / "part_a.img").read_bytes(), part_a_orig)

            # Modify part_a and repack into out.zip
            part_a_mod = b"M" * 4096 + b"\x00" * 4096
            (ext_dir / "part_a.img").write_bytes(part_a_mod)
            out_zip = tdir / "out.zip"
            lunaris_payload.repack_ota_zip(
                src_zip,
                {"part_a": ext_dir / "part_a.img"},
                out_zip,
                n,
                d,
                b"-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n",
                max_workers=1,
            )

            # Extract both part_a and part_b from out.zip and verify contents
            verify_dir = tdir / "verify"
            lunaris_payload.extract_partitions_from_zip(out_zip, {"part_a", "part_b"}, verify_dir)
            self.assertEqual((verify_dir / "part_a.img").read_bytes(), part_a_mod)
            self.assertEqual((verify_dir / "part_b.img").read_bytes(), part_b_orig)


if __name__ == "__main__":
    unittest.main()
