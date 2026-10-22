#!/usr/bin/env python3
"""Compare the resolved Meteoric ROM baseline with its Next-only rebuild.

--original must be generated from the pinned vendor/meteoric_defconfig using
its original compiler before integrating Next. The Image's deliberately
unrelated embedded stock_defconfig is input provenance, never the baseline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Mapping

PROFILE = "lunaris-pong-meteoric-7ccc-clang20"
APPROVED_TRANSITIONS: dict[str, tuple[str | None, str | None]] = {
    "CONFIG_KSU": (None, "y"),
    "CONFIG_KSU_DEBUG": (None, "n"),
    "CONFIG_KSU_DISABLE_MANAGER": (None, "n"),
    "CONFIG_KSU_DISABLE_POLICY": (None, "n"),
}
REQUIRED_BASELINE = {
    "CONFIG_SMP": "y",
    "CONFIG_PREEMPT": "y",
    "CONFIG_MODULES": "y",
    "CONFIG_MODULE_UNLOAD": "y",
    "CONFIG_MODVERSIONS": "y",
    "CONFIG_KPROBES": "y",
    "CONFIG_EXT4_FS": "y",
    "CONFIG_KASAN": "y",
    "CONFIG_KASAN_HW_TAGS": "y",
    "CONFIG_SHADOW_CALL_STACK": "y",
    "CONFIG_LTO_NONE": "y",
    "CONFIG_LOCALVERSION": '"-Meteoric"',
    "CONFIG_LOCALVERSION_AUTO": "n",
}
REQUIRED_BUILT = {
    **REQUIRED_BASELINE,
    "CONFIG_KSU": "y",
    "CONFIG_KSU_DEBUG": "n",
    "CONFIG_KSU_DISABLE_MANAGER": "n",
    "CONFIG_KSU_DISABLE_POLICY": "n",
}
FORBIDDEN_ENABLED = {
    "CONFIG_CFI_CLANG", "CONFIG_CFI_CLANG_SHADOW", "CONFIG_CFI_PERMISSIVE",
    "CONFIG_LTO", "CONFIG_LTO_CLANG", "CONFIG_LTO_CLANG_FULL", "CONFIG_LTO_CLANG_THIN",
}
REVIEW_NOTES = {
    "baseline": (
        "Resolve pinned Pong 7cccdf9b vendor/meteoric_defconfig with original Android Clang 20 "
        "before Next integration. This source deliberately embeds unrelated stock_defconfig; "
        "the uploaded config is provenance only."
    ),
    "binary_evidence": (
        "Original kallsyms contains KASAN HW-tag helpers and scs_alloc/scs_prepare; "
        "__cfi_check, __cfi_slowpath and cfi_shadow are absent. Preserve KASAN HW tags, "
        "shadow call stack and the actual non-CFI/non-LTO profile."
    ),
    "root_only": (
        "Only four exact absent-to-Next transitions are allowed. Preserve all effective baseline "
        "mitigations, proc-mem behavior, disabled options, local version and other settings. "
        "Missing and disabled are never normalized globally."
    ),
    "abi": (
        "This config gate cannot establish vendor-module compatibility. The independent "
        "full comparison against all 14,523 original exported symbols remains mandatory."
    ),
}


def parse_config(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        assignment = re.fullmatch(r"(CONFIG_[A-Za-z0-9_]+)=(.+)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Za-z0-9_]+) is not set", line)
        if assignment:
            key, value = assignment.groups()
        elif disabled:
            key, value = disabled.group(1), "n"
        elif line.startswith("CONFIG_"):
            raise ValueError(f"malformed configuration at line {number}")
        else:
            continue
        if key in result:
            raise ValueError(f"duplicate configuration option {key} at line {number}")
        result[key] = value
    if not result:
        raise ValueError("configuration contains no options")
    return result


def validate_configs(original: Mapping[str, str], built: Mapping[str, str]) -> dict:
    changes = {
        key: {"original": original.get(key), "built": built.get(key)}
        for key in sorted(original.keys() | built.keys())
        if original.get(key) != built.get(key)
    }
    unexpected = [
        key for key, change in changes.items()
        if APPROVED_TRANSITIONS.get(key) != (change["original"], change["built"])
    ]
    baseline_failures = {
        key: {"expected": expected, "original": original.get(key)}
        for key, expected in REQUIRED_BASELINE.items()
        if original.get(key) != expected
    }
    required_failures = {
        key: {"expected": expected, "built": built.get(key)}
        for key, expected in REQUIRED_BUILT.items()
        if built.get(key) != expected
    }
    # Kconfig can omit known disabled features. Their spelling is still compared
    # above; this is a feature assertion, not n/None normalization.
    forbidden_original = sorted(key for key in FORBIDDEN_ENABLED if original.get(key) in ("y", "m"))
    forbidden_built = sorted(key for key in FORBIDDEN_ENABLED if built.get(key) in ("y", "m"))
    baseline_root_options = sorted(key for key in original if key.startswith("CONFIG_KSU"))
    valid = not any((unexpected, baseline_failures, required_failures, forbidden_original, forbidden_built, baseline_root_options))
    return {
        "schema_version": 2,
        "profile": PROFILE,
        "valid": valid,
        "embedded_config_used_as_baseline": False,
        "changes": changes,
        "unexpected": unexpected,
        "baseline_failures": baseline_failures,
        "required_failures": required_failures,
        "forbidden_original_enabled": forbidden_original,
        "forbidden_built_enabled": forbidden_built,
        "baseline_root_options": baseline_root_options,
        "review_notes": REVIEW_NOTES,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path, help="resolved effective Meteoric baseline.config")
    parser.add_argument("--built", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"schema_version": 2, "profile": PROFILE, "valid": False}
    try:
        original_bytes, built_bytes = args.original.read_bytes(), args.built.read_bytes()
        report = validate_configs(
            parse_config(original_bytes.decode("utf-8")),
            parse_config(built_bytes.decode("utf-8")),
        )
        report["baseline_config_sha256"] = hashlib.sha256(original_bytes).hexdigest()
        report["built_config_sha256"] = hashlib.sha256(built_bytes).hexdigest()
    except (OSError, UnicodeError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not report["valid"]:
        details = report.get("error") or ", ".join(sorted(
            set(report.get("unexpected", [])) | set(report.get("baseline_failures", {})) |
            set(report.get("required_failures", {})) | set(report.get("forbidden_original_enabled", [])) |
            set(report.get("forbidden_built_enabled", [])) | set(report.get("baseline_root_options", []))
        ))
        print(f"FAIL: unreviewed/invalid effective Meteoric configuration: {details}; see {args.report}", file=sys.stderr)
        return 1
    print(f"Configuration verified: {len(report['changes'])} exact Next-only transitions; original non-LTO profile preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
