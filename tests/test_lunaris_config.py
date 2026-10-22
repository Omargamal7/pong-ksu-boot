"""Preserve the effective Meteoric baseline while adding exactly one Next core."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "verify_lunaris_config", Path(__file__).parents[1] / "scripts" / "verify_lunaris_config.py"
)
config = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config
SPEC.loader.exec_module(config)


def profile_pair():
    original = {
        **config.REQUIRED_BASELINE,
        "CONFIG_CFI_CLANG": "n",
        "CONFIG_LTO_CLANG_FULL": "n",
        "CONFIG_LTO_CLANG_THIN": "n",
        "CONFIG_TRIM_UNUSED_KSYMS": "n",
        "CONFIG_CPU_MITIGATIONS": "y",
        "CONFIG_ARM64_ERRATUM_3194386": "y",
        "CONFIG_PROC_MEM_ALWAYS_FORCE": "y",
        "CONFIG_PROC_MEM_FORCE_PTRACE": "n",
        "CONFIG_PROC_MEM_NO_FORCE": "n",
    }
    built = dict(original)
    built.update({"CONFIG_KSU": "y", "CONFIG_KSU_DEBUG": "n", "CONFIG_KSU_DISABLE_MANAGER": "n", "CONFIG_KSU_DISABLE_POLICY": "n"})
    return original, built


def serialize(values):
    return "".join(
        f"# {key} is not set\n" if value == "n" else f"{key}={value}\n"
        for key, value in sorted(values.items())
    )


class ConfigValidationTests(unittest.TestCase):
    def test_only_four_next_transitions_pass_and_preserve_non_lto_profile(self):
        original, built = profile_pair()
        result = config.validate_configs(original, built)
        self.assertTrue(result["valid"])
        self.assertEqual(set(result["changes"]), set(config.APPROVED_TRANSITIONS))
        self.assertEqual(len(result["changes"]), 4)
        self.assertFalse(result["embedded_config_used_as_baseline"])

    def test_missing_and_disabled_are_not_normalized_in_either_direction(self):
        for direction in ["add", "remove"]:
            with self.subTest(direction=direction):
                original, built = profile_pair()
                (built if direction == "add" else original)["CONFIG_UNREVIEWED_DISABLED"] = "n"
                result = config.validate_configs(original, built)
                self.assertFalse(result["valid"])
                self.assertIn("CONFIG_UNREVIEWED_DISABLED", result["unexpected"])

    def test_baseline_security_features_and_legacy_proc_mem_cannot_drift(self):
        for key in ["CONFIG_KASAN_HW_TAGS", "CONFIG_SHADOW_CALL_STACK", "CONFIG_CPU_MITIGATIONS", "CONFIG_ARM64_ERRATUM_3194386", "CONFIG_PROC_MEM_ALWAYS_FORCE"]:
            with self.subTest(key=key):
                original, built = profile_pair()
                built[key] = "n"
                result = config.validate_configs(original, built)
                self.assertFalse(result["valid"])
                self.assertIn(key, result["unexpected"])

    def test_saved_stock_cfi_profile_is_not_an_acceptable_baseline(self):
        original, built = profile_pair()
        for values in [original, built]:
            values["CONFIG_CFI_CLANG"] = "y"
            values["CONFIG_LTO_NONE"] = "n"
            values["CONFIG_KASAN_HW_TAGS"] = "n"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_CFI_CLANG", result["forbidden_original_enabled"])
        self.assertIn("CONFIG_KASAN_HW_TAGS", result["baseline_failures"])

    def test_root_must_be_built_in_and_keep_manager_and_policy(self):
        for key, wrong in [("CONFIG_KSU", "m"), ("CONFIG_KSU_DISABLE_MANAGER", "y"), ("CONFIG_KSU_DISABLE_POLICY", "y"), ("CONFIG_KSU_DEBUG", "y")]:
            with self.subTest(key=key):
                original, built = profile_pair()
                built[key] = wrong
                result = config.validate_configs(original, built)
                self.assertFalse(result["valid"])
                self.assertIn(key, result["required_failures"])

    def susfs_pair(self):
        original, built = profile_pair()
        built.update({key: value for key, (_, value) in config.SUSFS_TRANSITIONS.items()})
        return original, built

    def test_susfs_profile_accepts_only_its_reviewed_transitions(self):
        original, built = self.susfs_pair()
        result = config.validate_configs(original, built, "next-susfs")
        self.assertTrue(result["valid"], result)
        self.assertEqual(set(result["changes"]), set(config.SUSFS_TRANSITIONS))

    def test_susfs_profile_rejects_samsung_helpers_left_enabled(self):
        original, built = self.susfs_pair()
        built["CONFIG_KSU_SAMSUNG_KDP"] = "y"
        result = config.validate_configs(original, built, "next-susfs")
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_KSU_SAMSUNG_KDP", result["unexpected"])

    def test_susfs_profile_requires_every_susfs_feature(self):
        original, built = self.susfs_pair()
        built["CONFIG_KSU_SUSFS_SUS_MAP"] = "n"
        self.assertFalse(config.validate_configs(original, built, "next-susfs")["valid"])

    def test_default_profile_still_rejects_susfs(self):
        original, built = self.susfs_pair()
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_KSU_SUSFS", result["unexpected"])

    def test_unknown_ksu_prefixed_feature_is_not_blanket_allowed(self):
        original, built = profile_pair()
        built["CONFIG_KSU_SUSFS"] = "y"
        self.assertIn("CONFIG_KSU_SUSFS", config.validate_configs(original, built)["unexpected"])

    def test_source_baseline_cannot_already_contain_a_root_core(self):
        original, built = profile_pair()
        original["CONFIG_KSU"] = "n"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertEqual(result["baseline_root_options"], ["CONFIG_KSU"])

    def test_lto_or_whitelist_changes_are_not_authorized(self):
        for key, value in [("CONFIG_LTO_CLANG_THIN", "y"), ("CONFIG_TRIM_UNUSED_KSYMS", "y"), ("CONFIG_LOCALVERSION", '""'), ("CONFIG_UNUSED_KSYMS_WHITELIST", '"/new/path"')]:
            with self.subTest(key=key):
                original, built = profile_pair()
                built[key] = value
                self.assertIn(key, config.validate_configs(original, built)["unexpected"])

    def test_duplicate_and_empty_config_are_rejected(self):
        for text in ["CONFIG_KSU=y\nCONFIG_KSU=m\n", "# comments only\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                config.parse_config(text)

    def test_cli_writes_diagnostic_report_on_unexpected_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, built = profile_pair()
            built["CONFIG_SURPRISE"] = "y"
            (root / "baseline").write_text(serialize(original))
            (root / "built").write_text(serialize(built))
            arguments = ["verify_lunaris_config.py", "--original", str(root / "baseline"), "--built", str(root / "built"), "--report", str(root / "report.json")]
            with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(config.main(), 1)
            result = json.loads((root / "report.json").read_text())
            self.assertFalse(result["valid"])
            self.assertEqual(result["unexpected"], ["CONFIG_SURPRISE"])
            self.assertIn("baseline_config_sha256", result)


if __name__ == "__main__":
    unittest.main()
