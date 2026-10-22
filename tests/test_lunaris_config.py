"""Guard reviewed Kconfig migrations without accepting unrelated changes."""

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


# These are the twenty transitions observed in the failed old inline gate.
OBSERVED_CHANGES = {
    "CONFIG_ARM64_ERRATUM_3194386": (None, "y"),
    "CONFIG_ARM_SDE_INTERFACE": ("n", None),
    "CONFIG_CPU_MITIGATIONS": (None, "y"),
    "CONFIG_KSU": (None, "y"),
    "CONFIG_KSU_DEBUG": (None, "n"),
    "CONFIG_KSU_DISABLE_MANAGER": (None, "n"),
    "CONFIG_KSU_DISABLE_POLICY": (None, "n"),
    "CONFIG_LTO_CLANG_FULL": ("y", "n"),
    "CONFIG_LTO_CLANG_THIN": ("n", "y"),
    "CONFIG_MEMFD_ASHMEM_SHIM": (None, "n"),
    "CONFIG_MFD_TI_AM335X_TSCADC": ("n", None),
    "CONFIG_MMC_SDHCI_AM654": ("n", None),
    "CONFIG_MMC_SDHCI_OMAP": ("n", None),
    "CONFIG_NET_CLS_RSVP": ("n", None),
    "CONFIG_NET_CLS_RSVP6": ("n", None),
    "CONFIG_NET_SCH_CBQ": ("n", None),
    "CONFIG_NET_SCH_DSMARK": ("n", None),
    "CONFIG_PROC_MEM_ALWAYS_FORCE": (None, "y"),
    "CONFIG_PROC_MEM_FORCE_PTRACE": (None, "n"),
    "CONFIG_PROC_MEM_NO_FORCE": (None, "n"),
}


def observed_pair():
    original = dict(config.REQUIRED_BUILT)
    built = dict(original)
    for key, (before, after) in OBSERVED_CHANGES.items():
        if before is None:
            original.pop(key, None)
        else:
            original[key] = before
        if after is None:
            built.pop(key, None)
        else:
            built[key] = after
    return original, built


def serialize(values):
    return "".join(
        f"# {key} is not set\n" if value == "n" else f"{key}={value}\n"
        for key, value in sorted(values.items())
    )


class ConfigValidationTests(unittest.TestCase):
    def test_observed_failed_gate_pair_passes_after_exact_review(self):
        original, built = observed_pair()
        result = config.validate_configs(original, built)
        self.assertTrue(result["valid"])
        self.assertEqual(len(result["changes"]), 20)
        self.assertEqual(result["unexpected"], [])
        self.assertIn("original", result["review_notes"]["CONFIG_ARM64_ERRATUM_3194386"].lower())

    def test_unrelated_absent_to_n_is_not_normalized(self):
        original, built = observed_pair()
        built["CONFIG_UNREVIEWED_FEATURE"] = "n"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_UNREVIEWED_FEATURE", result["unexpected"])

    def test_unrelated_n_to_absent_is_not_normalized(self):
        original, built = observed_pair()
        original["CONFIG_UNREVIEWED_DISABLED"] = "n"
        self.assertIn("CONFIG_UNREVIEWED_DISABLED", config.validate_configs(original, built)["unexpected"])

    def test_reviewed_option_with_wrong_transition_is_rejected(self):
        original, built = observed_pair()
        built["CONFIG_MEMFD_ASHMEM_SHIM"] = "y"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_MEMFD_ASHMEM_SHIM", result["unexpected"])

    def test_ssbs_workaround_cannot_be_disabled(self):
        original, built = observed_pair()
        built["CONFIG_ARM64_ERRATUM_3194386"] = "n"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_ARM64_ERRATUM_3194386", result["required_failures"])

    def test_root_must_be_built_in_and_keep_manager_and_policy(self):
        for key, wrong in [("CONFIG_KSU", "m"), ("CONFIG_KSU_DISABLE_MANAGER", "y"), ("CONFIG_KSU_DISABLE_POLICY", "y")]:
            with self.subTest(key=key):
                original, built = observed_pair()
                built[key] = wrong
                result = config.validate_configs(original, built)
                self.assertFalse(result["valid"])
                self.assertIn(key, result["required_failures"])

    def test_unknown_ksu_prefixed_feature_is_not_blanket_allowed(self):
        original, built = observed_pair()
        built["CONFIG_KSU_SUSFS"] = "y"
        result = config.validate_configs(original, built)
        self.assertFalse(result["valid"])
        self.assertIn("CONFIG_KSU_SUSFS", result["unexpected"])

    def test_cfi_and_abi_config_cannot_drift(self):
        for key in ["CONFIG_CFI_CLANG", "CONFIG_MODVERSIONS", "CONFIG_TRIM_UNUSED_KSYMS"]:
            with self.subTest(key=key):
                original, built = observed_pair()
                built[key] = "n"
                self.assertFalse(config.validate_configs(original, built)["valid"])

    def test_duplicate_and_empty_config_are_rejected(self):
        for text in ["CONFIG_KSU=y\nCONFIG_KSU=m\n", "# comments only\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                config.parse_config(text)

    def test_cli_writes_diagnostic_report_on_unexpected_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, built = observed_pair()
            built["CONFIG_SURPRISE"] = "y"
            (root / "original").write_text(serialize(original))
            (root / "built").write_text(serialize(built))
            arguments = ["verify_lunaris_config.py", "--original", str(root / "original"), "--built", str(root / "built"), "--report", str(root / "report.json")]
            with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(config.main(), 1)
            result = json.loads((root / "report.json").read_text())
            self.assertFalse(result["valid"])
            self.assertEqual(result["unexpected"], ["CONFIG_SURPRISE"])
            self.assertIn("original_config_sha256", result)


if __name__ == "__main__":
    unittest.main()
