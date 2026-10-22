"""Exercise actual export-table parsing and vendor ABI rejection cases."""

import importlib.util
import contextlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "verify_kernel_abi", Path(__file__).parents[1] / "scripts" / "verify_kernel_abi.py"
)
abi = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = abi
SPEC.loader.exec_module(abi)


def fixture():
    image = bytearray(1024)
    image[56:60] = b"ARM\x64"
    base = 0xFFFFFFC008000000
    symbols = {"_text": base}
    for suffix in abi.GROUPS:
        symbols["__start___ksymtab" + suffix] = base + 128
        symbols["__stop___ksymtab" + suffix] = base + 128
        symbols["__start___kcrctab" + suffix] = base + 256
        symbols["__stop___kcrctab" + suffix] = base + 256
    symbols.update({"__stop___ksymtab": base + 152, "__stop___kcrctab": base + 264})
    for index, (name, crc) in enumerate([("first", 0x12345678), ("second", 0x87654321)]):
        offset, string_offset = 128 + 12 * index, 512 + 32 * index
        image[string_offset:string_offset + len(name) + 1] = name.encode() + b"\0"
        struct.pack_into("<iii", image, offset, -offset, string_offset - offset - 4, 600 - offset - 8)
        struct.pack_into("<I", image, 256 + 4 * index, crc)
        symbols["__ksymtab_" + name] = base + offset
    return image, symbols


class ExtractTests(unittest.TestCase):
    def test_prel32_export_names_pair_with_exact_crc_values(self):
        image, symbols = fixture()
        found = abi.extract_exports(image, symbols)
        self.assertEqual(found["first"], abi.Export(0x12345678, "EXPORT_SYMBOL"))
        self.assertEqual(found["second"].crc, 0x87654321)

    def test_missing_boundary_is_not_an_empty_success(self):
        image, symbols = fixture()
        del symbols["__stop___kcrctab"]
        with self.assertRaisesRegex(abi.AbiError, "missing table boundaries"):
            abi.extract_exports(image, symbols)

    def test_table_count_mismatch_is_rejected(self):
        image, symbols = fixture()
        symbols["__stop___kcrctab"] -= 4
        with self.assertRaisesRegex(abi.AbiError, "count differs"):
            abi.extract_exports(image, symbols)

    def test_out_of_image_name_pointer_is_rejected(self):
        image, symbols = fixture()
        struct.pack_into("<i", image, 132, 100000)
        with self.assertRaisesRegex(abi.AbiError, "outside"):
            abi.extract_exports(image, symbols)

    def test_kallsyms_disagreement_is_rejected(self):
        image, symbols = fixture()
        symbols["__ksymtab_first"] += 12
        with self.assertRaisesRegex(abi.AbiError, "disagrees"):
            abi.extract_exports(image, symbols)

    def test_zero_original_crc_is_rejected(self):
        image, symbols = fixture()
        struct.pack_into("<I", image, 256, 0)
        with self.assertRaisesRegex(abi.AbiError, "zero/unresolved"):
            abi.extract_exports(image, symbols)

    def test_export_not_covered_by_tables_is_rejected(self):
        image, symbols = fixture()
        symbols["__ksymtab_omitted"] = symbols["_text"] + 900
        with self.assertRaisesRegex(abi.AbiError, "do not cover"):
            abi.extract_exports(image, symbols)


class ComparisonTests(unittest.TestCase):
    def test_added_exports_are_allowed_but_originals_must_match(self):
        old = {"first": abi.Export(1, "EXPORT_SYMBOL")}
        new = dict(old, next_feature=abi.Export(2, "EXPORT_SYMBOL"))
        result = abi.compare_exports(old, new)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["additional_export_count"], 1)

    def test_missing_crc_license_and_namespace_drift_are_rejected(self):
        old = {
            "missing": abi.Export(1, "EXPORT_SYMBOL"),
            "crc": abi.Export(1, "EXPORT_SYMBOL"),
            "license": abi.Export(1, "EXPORT_SYMBOL"),
            "namespace": abi.Export(1, "EXPORT_SYMBOL", "OLD"),
        }
        new = {
            "crc": abi.Export(2, "EXPORT_SYMBOL"),
            "license": abi.Export(1, "EXPORT_SYMBOL_GPL"),
            "namespace": abi.Export(1, "EXPORT_SYMBOL", "NEW"),
        }
        result = abi.compare_exports(old, new)
        self.assertFalse(result["compatible"])
        for key in ["missing_exports", "crc_mismatches", "export_class_mismatches", "namespace_mismatches"]:
            self.assertEqual(len(result[key]), 1)

    def test_module_only_export_cannot_satisfy_boot_kernel_export(self):
        new = abi.parse_symvers("0x12345678\tfirst\tvmlinux\tEXPORT_SYMBOL\n0x87654321\tsecond\tdrivers/test\tEXPORT_SYMBOL\n")
        self.assertNotIn("second", new)

    def test_ambiguous_duplicate_crc_rows_rejected(self):
        with self.assertRaisesRegex(abi.AbiError, "duplicate"):
            abi.parse_symvers("0x12345678\tfirst\tvmlinux\tEXPORT_SYMBOL\n0x87654321\tfirst\tvmlinux\tEXPORT_SYMBOL\n")

    def test_empty_symvers_is_rejected(self):
        with self.assertRaisesRegex(abi.AbiError, "no vmlinux exports"):
            abi.parse_symvers("")


class CliModeTests(unittest.TestCase):
    def test_baseline_extraction_writes_complete_sorted_whitelist_without_claiming_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image, report, whitelist = root / "Image", root / "report.json", root / "symbols.txt"
            image.write_bytes(b"original-image")
            original = {"second": abi.Export(2, "EXPORT_SYMBOL"), "first": abi.Export(1, "EXPORT_SYMBOL")}
            arguments = ["verify_kernel_abi.py", "--original-image", str(image), "--report", str(report), "--export-whitelist", str(whitelist)]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(abi, "load_original_exports", return_value=original), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(abi.main(), 0)
            result = json.loads(report.read_text())
            self.assertEqual(whitelist.read_text(), "first\nsecond\n")
            self.assertEqual(result["mode"], "baseline_extraction")
            self.assertTrue(result["baseline_extracted"])
            self.assertFalse(result["compatible"])
            self.assertEqual(result["original_export_count"], 2)

    def test_baseline_extraction_failure_has_nonzero_exit_and_no_whitelist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image, report, whitelist = root / "Image", root / "report.json", root / "symbols.txt"
            image.write_bytes(b"bad-image")
            arguments = ["verify_kernel_abi.py", "--original-image", str(image), "--report", str(report), "--export-whitelist", str(whitelist)]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(abi, "load_original_exports", side_effect=abi.AbiError("no CRC baseline")), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(abi.main(), 1)
            result = json.loads(report.read_text())
            self.assertFalse(result["baseline_extracted"])
            self.assertFalse(result["compatible"])
            self.assertFalse(whitelist.exists())
            self.assertIn("no CRC baseline", result["error"])

    def test_comparison_still_requires_matching_symvers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image, report, symvers = root / "Image", root / "report.json", root / "Module.symvers"
            image.write_bytes(b"original-image")
            symvers.write_text("0x00000002\tfirst\tvmlinux\tEXPORT_SYMBOL\n")
            original = {"first": abi.Export(1, "EXPORT_SYMBOL")}
            arguments = ["verify_kernel_abi.py", "--original-image", str(image), "--report", str(report), "--symvers", str(symvers)]
            with mock.patch.object(sys, "argv", arguments), mock.patch.object(abi, "load_original_exports", return_value=original), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(abi.main(), 1)
            result = json.loads(report.read_text())
            self.assertEqual(result["mode"], "abi_comparison")
            self.assertTrue(result["baseline_extracted"])
            self.assertFalse(result["compatible"])
            self.assertEqual(len(result["crc_mismatches"]), 1)

    def test_missing_comparison_and_whitelist_arguments_is_usage_error(self):
        arguments = ["verify_kernel_abi.py", "--original-image", "Image", "--report", "report.json"]
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                abi.main()
        self.assertEqual(exc.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
