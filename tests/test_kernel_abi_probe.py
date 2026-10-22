"""Verify pre-build CRC screening is strict and cannot claim full ABI success."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("probe_kernel_abi", SCRIPTS / "probe_kernel_abi.py")
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
with mock.patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
    SPEC.loader.exec_module(probe)

ORIGINAL = {"module_layout": 0x5B043B0C, "I_BDEV": 0x41E76AD2, "__alloc_skb": 0x9D717AEB}
KNOWN_BAD = {"module_layout": 0x7C24B32D, "I_BDEV": 0xB49D4A82, "__alloc_skb": 0xBCF85BC0}


class SymversionsParsingTests(unittest.TestCase):
    def test_absolute_assignment_and_comments(self):
        text = "/* generated */\n__crc_module_layout = 0x5b043b0c; // end\n# note\n__crc_I_BDEV=0x41e76ad2;\n"
        self.assertEqual(probe.parse_symversions(text), {key: ORIGINAL[key] for key in ["module_layout", "I_BDEV"]})

    def test_unparsed_and_relative_linker_text_is_rejected(self):
        for text in [
            "SECTIONS { __crc_first = 0x12345678; }",
            "__crc_first = . + 0x12345678;",
            "__crc_first = 0x12345678; discarded text",
            "__crc_first = 0x12345678",  # A missing semicolon is malformed.
            "__crc_first = 0x123456789;",
            "/* unterminated comment",
        ]:
            with self.subTest(text=text), self.assertRaises(probe.ProbeError):
                probe.parse_symversions(text)

    def test_empty_or_comments_only_cannot_pass(self):
        for text in ["", "/* no exports */\n"]:
            with self.subTest(text=text), self.assertRaises(probe.ProbeError):
                probe.parse_symversions(text)

    def test_conflicting_duplicate_within_or_between_files_is_rejected(self):
        with self.assertRaisesRegex(probe.ProbeError, "conflicting duplicate"):
            probe.parse_symversions("__crc_first = 0x12345678; __crc_first = 0x87654321;")
        with self.assertRaisesRegex(probe.ProbeError, "conflicting duplicate"):
            probe.merge_probe_exports([{"first": 1}, {"first": 2}])


class ProbeComparisonTests(unittest.TestCase):
    def test_matching_probe_is_partial_and_extra_symbols_are_reported(self):
        result = probe.compare_probe(ORIGINAL, dict(ORIGINAL, additional_probe_export=123))
        self.assertTrue(result["probe_passed"])
        self.assertFalse(result["full_abi_verified"])
        self.assertEqual(result["compared_export_count"], 3)
        self.assertEqual(result["extra_exports"], ["additional_probe_export"])

    def test_known_bad_ack_candidate_is_rejected_for_all_three_anchors(self):
        result = probe.compare_probe(ORIGINAL, KNOWN_BAD)
        self.assertFalse(result["probe_passed"])
        self.assertEqual({entry["symbol"] for entry in result["mismatches"]}, set(probe.ANCHORS))

    def test_every_original_intersection_is_checked_not_only_anchors(self):
        result = probe.compare_probe(dict(ORIGINAL, other=1), dict(ORIGINAL, other=2))
        self.assertFalse(result["probe_passed"])
        self.assertEqual(result["mismatches"][0]["symbol"], "other")

    def test_missing_anchor_from_either_side_is_rejected(self):
        for side in ["original", "probe"]:
            old, new = dict(ORIGINAL), dict(ORIGINAL)
            del (old if side == "original" else new)["module_layout"]
            result = probe.compare_probe(old, new)
            self.assertFalse(result["probe_passed"])
            self.assertEqual(result["missing_anchors"][side], ["module_layout"])

    def test_zero_intersection_is_not_success(self):
        result = probe.compare_probe({}, {})
        self.assertFalse(result["probe_passed"])
        self.assertEqual(result["compared_export_count"], 0)

    def test_cli_persists_failure_report_for_bad_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Image").write_bytes(b"original")
            paths = []
            for index, (name, crc) in enumerate(KNOWN_BAD.items()):
                path = root / f"{index}.o.symversions"
                path.write_text(f"__crc_{name} = 0x{crc:08x};\n")
                paths.append(path)
            arguments = ["probe_kernel_abi.py", "--original-image", str(root / "Image"), "--report", str(root / "report.json")]
            for path in paths:
                arguments.extend(["--symversions", str(path)])
            originals = {name: mock.Mock(crc=crc) for name, crc in ORIGINAL.items()}
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(probe, "load_original_exports", return_value=originals), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            result = json.loads((root / "report.json").read_text())
            self.assertFalse(result["probe_passed"])
            self.assertFalse(result["full_abi_verified"])
            self.assertEqual(len(result["mismatches"]), 3)
            self.assertEqual(len(result["symversions_files"]), 3)

    def test_cli_requires_three_distinct_probe_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = ["probe_kernel_abi.py", "--original-image", str(root / "Image"), "--report", str(root / "report.json"), "--symversions", "only-one"]
            with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            result = json.loads((root / "report.json").read_text())
            self.assertFalse(result["probe_passed"])
            self.assertIn("three distinct", result["error"])


if __name__ == "__main__":
    unittest.main()
