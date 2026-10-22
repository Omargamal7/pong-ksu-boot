#!/usr/bin/env python3
"""Compare a raw ARM64 Image's exported ABI with a new kernel's symvers.

The original kernel must have CONFIG_KALLSYMS_ALL and CONFIG_MODVERSIONS.
The pinned vmlinux-to-elf dependency recovers symbol *addresses* only; this
script reads and cross-checks the actual PREL32 export and CRC tables itself.
Relative CRC tables and other architectures are deliberately unsupported.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class AbiError(ValueError):
    """The input does not provide a complete, trustworthy ABI baseline."""


@dataclass(frozen=True)
class Export:
    crc: int
    kind: str
    namespace: str = ""


GROUPS = {
    "": "EXPORT_SYMBOL",
    "_gpl": "EXPORT_SYMBOL_GPL",
    "_gpl_future": "EXPORT_SYMBOL_GPL_FUTURE",
    "_unused": "EXPORT_UNUSED_SYMBOL",
    "_unused_gpl": "EXPORT_UNUSED_SYMBOL_GPL",
}


def checked_range(start: int, stop: int, size: int, description: str) -> None:
    if not 0 <= start <= stop <= size:
        raise AbiError(f"{description} lies outside the original Image")


def read_cstring(image: bytes, offset: int, description: str) -> str:
    if not 0 <= offset < len(image):
        raise AbiError(f"{description} points outside the original Image")
    end = image.find(b"\0", offset, min(offset + 513, len(image)))
    if end < 0:
        raise AbiError(f"{description} is unterminated or too long")
    try:
        return image[offset:end].decode("ascii")
    except UnicodeDecodeError as exc:
        raise AbiError(f"{description} is not ASCII") from exc


def extract_exports(image: bytes, symbols: Mapping[str, int]) -> dict[str, Export]:
    """Read PREL32 ksymtab entries paired with absolute, 32-bit kcrctab CRCs."""
    if len(image) < 64 or image[56:60] != b"ARM\x64":
        raise AbiError("expected an uncompressed little-endian ARM64 Image")
    if "_text" not in symbols:
        raise AbiError("original kallsyms has no _text image-base symbol")
    base = symbols["_text"]
    exports: dict[str, Export] = {}
    table_ranges: list[tuple[int, int]] = []
    for suffix, kind in GROUPS.items():
        labels = [
            "__start___ksymtab" + suffix,
            "__stop___ksymtab" + suffix,
            "__start___kcrctab" + suffix,
            "__stop___kcrctab" + suffix,
        ]
        missing = [name for name in labels if name not in symbols]
        if missing:
            raise AbiError("original kallsyms missing table boundaries: " + ", ".join(missing))
        start, stop, crc_start, crc_stop = [symbols[name] - base for name in labels]
        checked_range(start, stop, len(image), "export table " + kind)
        checked_range(crc_start, crc_stop, len(image), "CRC table " + kind)
        if (stop - start) % 12 or (crc_stop - crc_start) % 4:
            raise AbiError(f"misaligned export/CRC table for {kind}")
        count = (stop - start) // 12
        if count != (crc_stop - crc_start) // 4:
            raise AbiError(f"export/CRC table count differs for {kind}")
        for a, z in [(start, stop), (crc_start, crc_stop)]:
            if a != z:
                if any(a < old_z and old_a < z for old_a, old_z in table_ranges):
                    raise AbiError("overlapping export/CRC tables")
                table_ranges.append((a, z))
        previous = ""
        for index in range(count):
            offset = start + index * 12
            _, name_relative, namespace_relative = struct.unpack_from("<iii", image, offset)
            name = read_cstring(image, offset + 4 + name_relative, "export name")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise AbiError(f"invalid exported symbol name: {name!r}")
            if name <= previous:
                raise AbiError(f"{kind} table is not strictly sorted")
            previous = name
            if symbols.get("__ksymtab_" + name) != base + offset:
                raise AbiError(f"kallsyms disagrees with export-table entry {name}")
            namespace = read_cstring(
                image, offset + 8 + namespace_relative, "export namespace"
            )
            if namespace and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", namespace):
                raise AbiError(f"invalid namespace for {name}")
            crc = struct.unpack_from("<I", image, crc_start + index * 4)[0]
            if crc == 0:
                raise AbiError(f"zero/unresolved original CRC for {name}")
            if name in exports:
                raise AbiError(f"duplicate original exported symbol {name}")
            exports[name] = Export(crc, kind, namespace)
    if not exports:
        raise AbiError("original Image contains no exported-symbol CRC baseline")
    actual_entries = {name[len("__ksymtab_"):] for name in symbols if name.startswith("__ksymtab_")}
    # __ksymtab_strings is a linker section boundary rather than an export.
    actual_entries.discard("strings")
    if set(exports) != actual_entries:
        raise AbiError("export tables do not cover every original __ksymtab_ symbol")
    return exports


def load_original_exports(image: bytes) -> dict[str, Export]:
    start = image.find(b"IKCFG_ST")
    end = image.find(b"IKCFG_ED", start + 8)
    if start < 0 or end < 0:
        raise AbiError("original Image has no embedded configuration")
    try:
        config = gzip.decompress(image[start + 8:end]).decode("ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise AbiError("original embedded configuration could not be decoded") from exc
    lines = set(config.splitlines())
    for setting in ["CONFIG_MODVERSIONS=y", "CONFIG_KALLSYMS_ALL=y", "CONFIG_KALLSYMS_BASE_RELATIVE=y"]:
        if setting not in lines:
            raise AbiError("original Image missing required " + setting)
    if "CONFIG_MODULE_REL_CRCS=y" in lines:
        raise AbiError("relative CRC tables are unsupported; refusing an ambiguous comparison")
    try:
        from vmlinux_to_elf.core.kallsyms import KallsymsFinder
    except ImportError as exc:
        raise AbiError("install the pinned vmlinux-to-elf dependency before ABI verification") from exc
    found = KallsymsFinder(image, bit_size=64)
    if found.is_big_endian or not found.is_64_bits:
        raise AbiError("original kallsyms is not little-endian 64-bit")
    symbols: dict[str, int] = {}
    for symbol in found.symbols:
        if symbol.name in symbols and symbols[symbol.name] != symbol.virtual_address:
            if symbol.name == "_text" or symbol.name.startswith(("__start___", "__stop___", "__ksymtab_")):
                raise AbiError("ambiguous original kallsyms label " + symbol.name)
        symbols[symbol.name] = symbol.virtual_address
    return extract_exports(image, symbols)


def parse_symvers(text: str) -> dict[str, Export]:
    exports: dict[str, Export] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) not in (4, 5) or not re.fullmatch(r"0x[0-9a-fA-F]{8}", fields[0]):
            raise AbiError(f"invalid symvers row {number}")
        crc, name, module, kind = fields[:4]
        if kind not in GROUPS.values():
            raise AbiError(f"unknown symvers export class at row {number}")
        # A module's export cannot satisfy an export required from the boot kernel.
        if module != "vmlinux":
            continue
        if name in exports:
            raise AbiError(f"duplicate vmlinux symbol in symvers: {name}")
        exports[name] = Export(int(crc, 16), kind, fields[4] if len(fields) == 5 else "")
    if not exports:
        raise AbiError("new symvers contains no vmlinux exports")
    return exports


def compare_exports(original: Mapping[str, Export], rebuilt: Mapping[str, Export]) -> dict:
    missing = sorted(set(original) - set(rebuilt))
    crc_mismatches = []
    export_class_mismatches = []
    namespace_mismatches = []
    for name in sorted(set(original) & set(rebuilt)):
        old, new = original[name], rebuilt[name]
        if old.crc != new.crc:
            crc_mismatches.append({"symbol": name, "original": f"0x{old.crc:08x}", "rebuilt": f"0x{new.crc:08x}"})
        if old.kind != new.kind:
            export_class_mismatches.append({"symbol": name, "original": old.kind, "rebuilt": new.kind})
        if old.namespace != new.namespace:
            namespace_mismatches.append({"symbol": name, "original": old.namespace, "rebuilt": new.namespace})
    return {
        "compatible": not (missing or crc_mismatches or export_class_mismatches or namespace_mismatches),
        "original_export_count": len(original),
        "rebuilt_vmlinux_export_count": len(rebuilt),
        "additional_export_count": len(set(rebuilt) - set(original)),
        "missing_exports": missing,
        "crc_mismatches": crc_mismatches,
        "export_class_mismatches": export_class_mismatches,
        "namespace_mismatches": namespace_mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-image", required=True, type=Path)
    parser.add_argument("--symvers", type=Path, help="rebuilt vmlinux symvers to compare")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--export-whitelist", type=Path,
        help="write all original export names, one per line, for the build whitelist",
    )
    args = parser.parse_args()
    if args.symvers is None and args.export_whitelist is None:
        parser.error("provide --symvers for comparison or --export-whitelist for baseline extraction")
    extraction_only = args.symvers is None
    report = {
        "schema_version": 1,
        "mode": "baseline_extraction" if extraction_only else "abi_comparison",
        "compatible": False,
        "baseline_extracted": False,
    }
    try:
        image = args.original_image.read_bytes()
        report["original_image_sha256"] = hashlib.sha256(image).hexdigest()
        original = load_original_exports(image)
        if args.export_whitelist is not None:
            args.export_whitelist.parent.mkdir(parents=True, exist_ok=True)
            args.export_whitelist.write_text("\n".join(sorted(original)) + "\n")
        report["baseline_extracted"] = True
        report["original_export_count"] = len(original)
        if args.symvers is not None:
            symvers_bytes = args.symvers.read_bytes()
            report["rebuilt_symvers_sha256"] = hashlib.sha256(symvers_bytes).hexdigest()
            rebuilt = parse_symvers(symvers_bytes.decode("ascii"))
            report.update(compare_exports(original, rebuilt))
    except Exception as exc:
        # Extraction errors must never become an empty "all exports match" result.
        report["error"] = f"{type(exc).__name__}: {exc}"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if extraction_only and report["baseline_extracted"] and "error" not in report:
        print(f"Extracted {report['original_export_count']} original exports; ABI comparison pending")
        return 0
    if not report["compatible"]:
        print("FAIL: original kernel ABI extraction/comparison failed; see " + str(args.report), file=sys.stderr)
        if "error" in report:
            print(report["error"], file=sys.stderr)
        return 1
    print(f"ABI verified: {report['original_export_count']} original exports preserved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
