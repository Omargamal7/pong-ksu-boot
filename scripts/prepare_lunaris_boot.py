#!/usr/bin/env python3
"""Verify and unpack the uploaded installed Lunaris image for cloud building."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lunaris_boot import kernel_config, verified_original


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    metadata, kernel, ramdisk = verified_original(args.boot)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "kernel").write_bytes(kernel)
    (args.output_dir / "ramdisk").write_bytes(ramdisk)
    (args.output_dir / "original.config").write_bytes(kernel_config(kernel))
    (args.output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
