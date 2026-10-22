#!/usr/bin/env python3
"""Reject incompatible kernel candidates using three pre-build CRC probes.

This is only an early failure gate. A successful probe never substitutes for
the complete original-export comparison after the full kernel is linked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Mapping

from verify_kernel_abi import load_original_exports


ANCHORS = ("module_layout", "I_BDEV", "__alloc_skb")
ASSIGNMENT = re.compile(r"\s*__crc_([A-Za-z_][A-Za-z0-9_]*)\s*=\s*0x([0-9a-fA-F]{8})\s*;")


class ProbeError(ValueError):
    """The probe input cannot establish a trustworthy partial comparison."""


def strip_comments(text: str) -> str:
    """Keep line positions while accepting only ordinary linker-file comments."""
    pattern = re.compile(r"/\*.*?\*/|//[^\n]*|\#[^\n]*", re.DOTALL)
    return pattern.sub(lambda match: "\n" * match.group().count("\n"), text)


def parse_symversions(text: str, source: str = "symversions") -> dict[str, int]:
    cleaned = strip_comments(text)
    result: dict[str, int] = {}
    position = 0
    while position < len(cleaned):
        if not cleaned[position:].strip():
            break
        match = ASSIGNMENT.match(cleaned, position)
        if match is None:
            line = cleaned.count("\n", 0, position) + 1
            raise ProbeError(f"unparsed linker text in {source} near line {line}; expected absolute __crc_NAME assignments")
        name, value = match.groups()
        crc = int(value, 16)
        if name in result and result[name] != crc:
            raise ProbeError(f"conflicting duplicate CRC for {name} in {source}")
        result[name] = crc
        position = match.end()
    if not result:
        raise ProbeError(f"no absolute CRC assignments found in {source}")
    return result


def merge_probe_exports(parts: list[dict[str, int]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for part in parts:
        for name, crc in part.items():
            if name in result and result[name] != crc:
                raise ProbeError(f"conflicting duplicate CRC for {name} across probe files")
            result[name] = crc
    return result


def compare_probe(original: Mapping[str, int], probed: Mapping[str, int]) -> dict:
    intersection = set(original) & set(probed)
    missing_anchors = {
        "original": [name for name in ANCHORS if name not in original],
        "probe": [name for name in ANCHORS if name not in probed],
    }
    mismatches = [
        {"symbol": name, "original": f"0x{original[name]:08x}", "probed": f"0x{probed[name]:08x}"}
        for name in sorted(intersection)
        if original[name] != probed[name]
    ]
    return {
        "mode": "partial_abi_probe",
        "full_abi_verified": False,
        "probe_passed": bool(intersection) and not mismatches and not any(missing_anchors.values()),
        "original_export_count": len(original),
        "probe_export_count": len(probed),
        "compared_export_count": len(intersection),
        "missing_anchors": missing_anchors,
        "mismatches": mismatches,
        "extra_exports": sorted(set(probed) - set(original)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-image", required=True, type=Path)
    parser.add_argument("--symversions", required=True, type=Path, action="append")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "mode": "partial_abi_probe",
        "full_abi_verified": False,
        "probe_passed": False,
        "symversions_files": [],
    }
    try:
        if len(args.symversions) != 3 or len(set(args.symversions)) != 3:
            raise ProbeError("exactly three distinct --symversions files are required")
        image = args.original_image.read_bytes()
        report["original_image_sha256"] = hashlib.sha256(image).hexdigest()
        original = {name: export.crc for name, export in load_original_exports(image).items()}
        parts = []
        for path in args.symversions:
            content = path.read_bytes()
            parsed = parse_symversions(content.decode("ascii"), str(path))
            parts.append(parsed)
            report["symversions_files"].append({
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "export_count": len(parsed),
            })
        report.update(compare_probe(original, merge_probe_exports(parts)))
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not report["probe_passed"]:
        print(f"FAIL: partial kernel ABI probe rejected; see {args.report}", file=sys.stderr)
        if "error" in report:
            print(report["error"], file=sys.stderr)
        return 1
    print(f"Partial ABI probe passed for {report['compared_export_count']} original exports; full ABI verification still required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
