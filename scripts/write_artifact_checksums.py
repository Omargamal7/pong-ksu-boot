#!/usr/bin/env python3
"""Atomically refresh SHA256SUMS for the visible files published as artifacts."""

import argparse
import hashlib
from pathlib import Path
import tempfile


def write_checksums(directory: Path) -> None:
    files = sorted(path for path in directory.iterdir()
                   if path.is_file() and not path.name.startswith(".") and path.name != "SHA256SUMS")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".SHA256SUMS-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for path in files:
                with path.open("rb") as artifact:
                    digest = hashlib.file_digest(artifact, "sha256").hexdigest()
                name = path.name.replace("\\", "\\\\").replace("\n", "\\n")
                escaped = "\\" if name != path.name else ""
                stream.write(f"{escaped}{digest}  {name}\n")
        temporary.replace(directory / "SHA256SUMS")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    write_checksums(parser.parse_args().directory)
