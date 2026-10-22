"""Inspect the verified Pong Lunaris boot image without modifying its payloads."""

from __future__ import annotations

import gzip
import hashlib
import re
import struct
from pathlib import Path

BOOT_SHA256 = "f86ca8c2a1f731a921813cecf0de1d693ed795f684cfb10cf1140bee4781cc7d"
CONFIG_SHA256 = "a8b21ecc239eb1cbb9a72ccca8c5fa98c9833c2b003472fc840e099ef26ab778"
EXPECTED_RELEASE = "5.10.237-Meteoric"
PARTITION_SIZE = 100663296
PAGE_SIZE = 4096
HEADER_SIZE = 1584


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kernel_config(kernel: bytes) -> bytes:
    start = kernel.find(b"IKCFG_ST")
    end = kernel.find(b"IKCFG_ED", start + 8)
    if start < 0 or end < 0:
        raise ValueError("kernel has no embedded configuration")
    return gzip.decompress(kernel[start + 8:end])


def kernel_release(kernel: bytes) -> str:
    match = re.search(rb"Linux version ([^\x00\s]+)", kernel)
    if not match:
        raise ValueError("kernel has no Linux version banner")
    return match[1].decode("ascii")


def inspect_boot(path: Path) -> tuple[dict, bytes, bytes]:
    data = path.read_bytes()
    if len(data) < PAGE_SIZE or data[:8] != b"ANDROID!":
        raise ValueError("not an Android boot image")
    kernel_size, ramdisk_size, os_version, header_size = struct.unpack_from("<4I", data, 8)
    version = struct.unpack_from("<I", data, 40)[0]
    if version != 4 or header_size != HEADER_SIZE:
        raise ValueError(f"expected boot header v4/{HEADER_SIZE}; got v{version}/{header_size}")
    if not kernel_size or not ramdisk_size:
        raise ValueError("kernel and ramdisk must both be present")
    signature_size = struct.unpack_from("<I", data, 1580)[0]
    if signature_size:
        raise ValueError("v4 boot signature requires re-signing; refusing to preserve a stale signature")
    ramdisk_offset = PAGE_SIZE + ((kernel_size + PAGE_SIZE - 1) // PAGE_SIZE) * PAGE_SIZE
    if ramdisk_offset + ramdisk_size > len(data):
        raise ValueError("boot payload extends beyond the image")
    kernel = data[PAGE_SIZE:PAGE_SIZE + kernel_size]
    ramdisk = data[ramdisk_offset:ramdisk_offset + ramdisk_size]
    if len(kernel) < 64 or kernel[56:60] != b"ARM\x64":
        raise ValueError("expected an uncompressed arm64 Linux Image")
    metadata = {
        "sha256": sha256(data),
        "size": len(data),
        "header_version": version,
        "header_size": header_size,
        "kernel_size": kernel_size,
        "kernel_sha256": sha256(kernel),
        "kernel_release": kernel_release(kernel),
        "ramdisk_size": ramdisk_size,
        "ramdisk_sha256": sha256(ramdisk),
        "os_version_encoded": os_version,
        "signature_size": signature_size,
        # Every header byte except the kernel length must survive repacking.
        "preserved_header_sha256": sha256(data[:8] + data[12:HEADER_SIZE]),
        "config_sha256": sha256(kernel_config(kernel)),
    }
    return metadata, kernel, ramdisk


def verified_original(path: Path) -> tuple[dict, bytes, bytes]:
    metadata, kernel, ramdisk = inspect_boot(path)
    for key, expected in {
        "sha256": BOOT_SHA256,
        "size": PARTITION_SIZE,
        "kernel_release": EXPECTED_RELEASE,
        "config_sha256": CONFIG_SHA256,
    }.items():
        if metadata[key] != expected:
            raise ValueError(f"original {key} mismatch: {metadata[key]} != {expected}")
    return metadata, kernel, ramdisk
