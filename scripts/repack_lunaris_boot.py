#!/usr/bin/env python3
"""Replace the verified Lunaris boot's kernel, preserving its ramdisk and header."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lunaris_boot import EXPECTED_RELEASE, PARTITION_SIZE, inspect_boot, verified_original


def validate_repack(base: Path, kernel: Path, output: Path, partition_size: int) -> dict:
    original, _, original_ramdisk = verified_original(base)
    rebuilt, new_kernel, new_ramdisk = inspect_boot(output)
    if rebuilt["size"] > partition_size:
        raise ValueError("rebuilt image exceeds boot partition size")
    if rebuilt["kernel_release"] != EXPECTED_RELEASE:
        raise ValueError("rebuilt kernel release does not match Lunaris")
    if rebuilt["preserved_header_sha256"] != original["preserved_header_sha256"]:
        raise ValueError("boot header changed beyond the kernel size")
    if new_ramdisk != original_ramdisk:
        raise ValueError("boot ramdisk changed")
    if new_kernel != kernel.read_bytes():
        raise ValueError("boot kernel differs from the verified build output")
    if rebuilt["kernel_sha256"] == original["kernel_sha256"]:
        raise ValueError("rebuilt kernel is identical to the original")
    return rebuilt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partition-size", type=int, required=True)
    args = parser.parse_args()
    if args.partition_size != PARTITION_SIZE:
        parser.error("partition size must match this verified Pong input")
    verified_original(args.base)
    for name in ("mkbootimg.py", "unpack_bootimg.py"):
        if not (args.tools / name).is_file():
            parser.error(f"missing AOSP tool: {name}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lunaris-repack-") as temporary:
        directory = Path(temporary)
        result = subprocess.run(
            [sys.executable, str(args.tools / "unpack_bootimg.py"),
             "--boot_img", str(args.base), "--out", str(directory / "unpacked"),
             "--format=mkbootimg"],
            check=True, capture_output=True, text=True,
        )
        tokens = shlex.split(result.stdout.strip())
        if tokens.count("--kernel") != 1:
            raise ValueError("AOSP unpacker did not identify exactly one kernel")
        tokens[tokens.index("--kernel") + 1] = str(args.kernel.resolve())
        candidate = directory / "candidate.img"
        subprocess.run(
            [sys.executable, str(args.tools / "mkbootimg.py"), *tokens,
             "--output", str(candidate)], check=True,
        )
        metadata = validate_repack(args.base, args.kernel, candidate, args.partition_size)
        shutil.copyfile(candidate, args.output)
    args.output.with_suffix(".metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
