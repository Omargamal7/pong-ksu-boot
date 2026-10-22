#!/usr/bin/env python3
"""Check a manager APK's v2 signing certificate the way KernelSU Next's kernel does.

Mirrors kernel/manager/apk_sign.c: the APK Signing Block may contain only one
v2 block (0x7109871a) plus verity padding (0x42726577); the first signer's
first certificate must match an accepted size and SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

V2_BLOCK_ID = 0x7109871A
VERITY_PADDING_ID = 0x42726577
SIGNING_BLOCK_MAGIC = b"APK Sig Block 42"


def length_prefixed(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """Return (start, end) of a uint32 length-prefixed field at pos."""
    if pos + 4 > end:
        raise ValueError("truncated length prefix")
    (size,) = struct.unpack_from("<I", data, pos)
    start = pos + 4
    if size > end - start:
        raise ValueError("length prefix exceeds its container")
    return start, start + size


def signing_certificate(data: bytes) -> bytes:
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0 or eocd + 22 > len(data):
        raise ValueError("no ZIP end of central directory")
    cd_size, cd_offset = struct.unpack_from("<II", data, eocd + 12)
    if cd_offset + cd_size != eocd or cd_offset < 0x20:
        raise ValueError("central directory does not end at the EOCD")
    if data[cd_offset - 16:cd_offset] != SIGNING_BLOCK_MAGIC:
        raise ValueError("no APK Signing Block")
    (block_size,) = struct.unpack_from("<Q", data, cd_offset - 24)
    block_start = cd_offset - block_size - 8
    if block_start < 0 or struct.unpack_from("<Q", data, block_start)[0] != block_size:
        raise ValueError("inconsistent APK Signing Block size")
    pos, pairs_end = block_start + 8, cd_offset - 24
    certificates = []
    while pos < pairs_end:
        (pair_size,) = struct.unpack_from("<Q", data, pos)
        pos += 8
        if pair_size < 4 or pair_size > pairs_end - pos:
            raise ValueError("malformed signing block pair")
        pair_end = pos + pair_size
        (block_id,) = struct.unpack_from("<I", data, pos)
        if block_id == V2_BLOCK_ID:
            signers = length_prefixed(data, pos + 4, pair_end)
            signer = length_prefixed(data, signers[0], signers[1])
            signed_data = length_prefixed(data, signer[0], signer[1])
            digests = length_prefixed(data, signed_data[0], signed_data[1])
            certs = length_prefixed(data, digests[1], signed_data[1])
            cert = length_prefixed(data, certs[0], certs[1])
            certificates.append(data[cert[0]:cert[1]])
        elif block_id != VERITY_PADDING_ID:
            raise ValueError(f"signing block 0x{block_id:08x} would make the kernel reject this APK")
        pos = pair_end
    if len(certificates) != 1:
        raise ValueError(f"expected exactly one v2 signing block, found {len(certificates)}")
    return certificates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--accept", action="append", required=True,
                        help="accepted SIZE:SHA256 pair, e.g. 0x3e6:79e5...")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    certificate = signing_certificate(args.apk.read_bytes())
    found = f"{len(certificate):#x}:{hashlib.sha256(certificate).hexdigest()}"
    accepted = {f"{int(size, 16):#x}:{digest.lower()}" for size, digest in
                (pair.split(":", 1) for pair in args.accept)}
    report = {"apk": args.apk.name, "certificate": found, "accepted": found in accepted}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))
    if not report["accepted"]:
        print("FAIL: manager signing certificate is not accepted by this kernel", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
