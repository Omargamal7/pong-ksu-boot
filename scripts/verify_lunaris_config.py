#!/usr/bin/env python3
"""Accept only audited original-to-built Lunaris configuration transitions.

The original Image contains mitigations/legacy behavior omitted from its
embedded configuration. These exact exceptions describe the inspected binary;
they do not make an absent option equivalent to a disabled option in general.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Mapping


# Every entry is directional: changing either endpoint requires a new review.
APPROVED_TRANSITIONS: dict[str, tuple[str | None, str | None]] = {
    "CONFIG_CPU_MITIGATIONS": (None, "y"),
    "CONFIG_ARM64_ERRATUM_3194386": (None, "y"),
    "CONFIG_PROC_MEM_ALWAYS_FORCE": (None, "y"),
    "CONFIG_MEMFD_ASHMEM_SHIM": (None, "n"),
    "CONFIG_PROC_MEM_FORCE_PTRACE": (None, "n"),
    "CONFIG_PROC_MEM_NO_FORCE": (None, "n"),
    "CONFIG_ARM_SDE_INTERFACE": ("n", None),
    "CONFIG_MFD_TI_AM335X_TSCADC": ("n", None),
    "CONFIG_MMC_SDHCI_AM654": ("n", None),
    "CONFIG_MMC_SDHCI_OMAP": ("n", None),
    "CONFIG_NET_CLS_RSVP": ("n", None),
    "CONFIG_NET_CLS_RSVP6": ("n", None),
    "CONFIG_NET_SCH_CBQ": ("n", None),
    "CONFIG_NET_SCH_DSMARK": ("n", None),
    "CONFIG_KSU": (None, "y"),
    "CONFIG_KSU_DEBUG": (None, "n"),
    "CONFIG_KSU_DISABLE_MANAGER": (None, "n"),
    "CONFIG_KSU_DISABLE_POLICY": (None, "n"),
    "CONFIG_LTO_CLANG_FULL": ("y", "n"),
    "CONFIG_LTO_CLANG_THIN": ("n", "y"),
}

REVIEW_NOTES = {
    "baseline": (
        "The uploaded 5.10.237-Meteoric Image's embedded configuration is incomplete "
        "relative to its binary. Exceptions are limited to the exact reviewed transitions."
    ),
    "CONFIG_CPU_MITIGATIONS": (
        "Pinned arch/Kconfig defines this hidden arm64 option as def_bool y."
    ),
    "CONFIG_ARM64_ERRATUM_3194386": (
        "Original binary kallsyms contains erratum_spec_ssbs_list, "
        "ssbs_emulation_handler and ssbs_emulation_hook. Keep the existing SSBS workaround enabled."
    ),
    "CONFIG_PROC_MEM_ALWAYS_FORCE": (
        "Original proc_mem_force_override at Image offset 0x19ead7c stores enum 0, "
        "PROC_MEM_FORCE_ALWAYS: retain traditional /proc/pid/mem behavior."
    ),
    "disabled_options": (
        "Only the listed new disabled options and removed disabled options are accepted; "
        "missing and n are not normalized globally."
    ),
    "root_and_lto": (
        "Build exactly one KernelSU Next core into the Image, keep manager/policy support, "
        "disable debug mode, and switch full LTO to thin LTO for cloud-runner capacity."
    ),
    "abi": (
        "Configuration review does not establish vendor-module compatibility. "
        "The independent full original exported-symbol ABI comparison remains mandatory."
    ),
}

REQUIRED_BUILT = {
    "CONFIG_CPU_MITIGATIONS": "y",
    "CONFIG_ARM64_ERRATUM_3194386": "y",
    "CONFIG_PROC_MEM_ALWAYS_FORCE": "y",
    "CONFIG_MEMFD_ASHMEM_SHIM": "n",
    "CONFIG_PROC_MEM_FORCE_PTRACE": "n",
    "CONFIG_PROC_MEM_NO_FORCE": "n",
    "CONFIG_KSU": "y",
    "CONFIG_KSU_DEBUG": "n",
    "CONFIG_KSU_DISABLE_MANAGER": "n",
    "CONFIG_KSU_DISABLE_POLICY": "n",
    "CONFIG_LTO_CLANG_FULL": "n",
    "CONFIG_LTO_CLANG_THIN": "y",
    "CONFIG_CFI_CLANG": "y",
    "CONFIG_CFI_CLANG_SHADOW": "y",
    "CONFIG_SMP": "y",
    "CONFIG_PREEMPT": "y",
    "CONFIG_MODULE_UNLOAD": "y",
    "CONFIG_MODVERSIONS": "y",
    "CONFIG_TRIM_UNUSED_KSYMS": "y",
    "CONFIG_KPROBES": "y",
    "CONFIG_EXT4_FS": "y",
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
    required_failures = {
        key: {"expected": expected, "built": built.get(key)}
        for key, expected in REQUIRED_BUILT.items()
        if built.get(key) != expected
    }
    return {
        "schema_version": 1,
        "valid": not unexpected and not required_failures,
        "changes": changes,
        "unexpected": unexpected,
        "required_failures": required_failures,
        "review_notes": REVIEW_NOTES,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--built", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {"schema_version": 1, "valid": False}
    try:
        original_bytes, built_bytes = args.original.read_bytes(), args.built.read_bytes()
        report = validate_configs(
            parse_config(original_bytes.decode("utf-8")),
            parse_config(built_bytes.decode("utf-8")),
        )
        report["original_config_sha256"] = hashlib.sha256(original_bytes).hexdigest()
        report["built_config_sha256"] = hashlib.sha256(built_bytes).hexdigest()
    except (OSError, UnicodeError, ValueError) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not report["valid"]:
        details = report.get("error") or ", ".join(
            sorted(set(report.get("unexpected", [])) | set(report.get("required_failures", {})))
        )
        print(f"FAIL: unreviewed/invalid Lunaris configuration: {details}; see {args.report}", file=sys.stderr)
        return 1
    print(f"Configuration verified: {len(report['changes'])} exact reviewed transitions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
