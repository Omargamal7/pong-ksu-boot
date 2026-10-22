#!/usr/bin/env python3
"""Give the repacked boot the same AVB hash-footer layout as the verified original.

The original carries an unsigned (algorithm NONE) hash footer for partition
"boot" at the end of the 96 MiB partition image. Its digest does not match its
own payload, so this script records that fact instead of trusting it. The new
footer reuses every original field except the payload size and digest, which
avbtool computes for the new payload. Nothing here signs anything or changes
the device's vbmeta partition.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lunaris_boot import PARTITION_SIZE, verified_original

ALGORITHM_NONE = 0


def load_avbtool(path: Path):
    spec = importlib.util.spec_from_file_location("pinned_avbtool", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load avbtool from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_footer(avbtool, path: Path) -> dict:
    """Return the footer, header and descriptor fields this profile depends on."""
    image = avbtool.ImageHandler(str(path), read_only=True)
    try:
        footer, header, descriptors, image_size = avbtool.Avb()._parse_image(image)
    except avbtool.AvbError as exc:
        raise ValueError(f"{path.name} has no AVB footer: {exc}") from exc
    finally:
        # ImageHandler has no close(); release its handle before the file is replaced.
        image._image.close()
    if footer is None:
        raise ValueError(f"{path.name} has no AVB footer")
    hashes = [d for d in descriptors if isinstance(d, avbtool.AvbHashDescriptor)]
    props = [d for d in descriptors if isinstance(d, avbtool.AvbPropertyDescriptor)]
    if len(hashes) != 1 or len(hashes) + len(props) != len(descriptors):
        raise ValueError("expected exactly one hash descriptor plus property descriptors")
    descriptor = hashes[0]
    return {
        "image_size": image_size,
        "original_image_size": footer.original_image_size,
        "vbmeta_offset": footer.vbmeta_offset,
        "vbmeta_size": footer.vbmeta_size,
        "footer_version": f"{footer.version_major}.{footer.version_minor}",
        "algorithm_type": header.algorithm_type,
        "rollback_index": header.rollback_index,
        "flags": header.flags,
        "rollback_index_location": header.rollback_index_location,
        "release_string": header.release_string,
        "required_libavb_version": f"{header.required_libavb_version_major}.{header.required_libavb_version_minor}",
        "partition_name": descriptor.partition_name,
        "hash_algorithm": descriptor.hash_algorithm,
        "hash_image_size": descriptor.image_size,
        "salt": descriptor.salt.hex(),
        "digest": descriptor.digest.hex(),
        "descriptor_flags": descriptor.flags,
        "props": [[p.key, p.value.decode("utf-8")] for p in props],
    }


def payload_digest(data: bytes, fields: dict) -> str:
    salted = hashlib.new(fields["hash_algorithm"], bytes.fromhex(fields["salt"]))
    salted.update(data[:fields["hash_image_size"]])
    return salted.hexdigest()


PRESERVED = (
    "image_size", "footer_version", "algorithm_type", "rollback_index", "flags",
    "rollback_index_location", "release_string", "required_libavb_version",
    "partition_name", "hash_algorithm", "salt", "descriptor_flags", "props",
)


def validate_footer(original: dict, payload: bytes, output: Path, result: dict) -> None:
    data = output.read_bytes()
    changed = [key for key in PRESERVED if result[key] != original[key]]
    if changed:
        raise ValueError("footer fields differ from the original: " + ", ".join(changed))
    if len(data) != original["image_size"]:
        raise ValueError("footered image does not fill the original partition size")
    if result["original_image_size"] != len(payload) or result["hash_image_size"] != len(payload):
        raise ValueError("footer does not describe the repacked payload length")
    if data[:len(payload)] != payload:
        raise ValueError("payload bytes changed while adding the footer")
    if result["digest"] != payload_digest(data, result):
        raise ValueError("footer digest does not match the repacked payload")


def add_footer(base: Path, image: Path, avbtool_path: Path, output: Path,
               partition_size: int = PARTITION_SIZE) -> dict:
    avbtool = load_avbtool(avbtool_path)
    original = read_footer(avbtool, base)
    if original["image_size"] != partition_size or original["partition_name"] != "boot":
        raise ValueError("original footer is not for this Pong boot partition")
    if original["algorithm_type"] != ALGORITHM_NONE:
        raise ValueError("original vbmeta is signed; an unsigned copy would not match it")
    original_payload_matches = original["digest"] == payload_digest(base.read_bytes(), original)
    payload = image.read_bytes()
    if len(payload) >= partition_size:
        raise ValueError("repacked payload leaves no room for an AVB footer")

    command = [
        sys.executable, str(avbtool_path), "add_hash_footer",
        "--partition_size", str(original["image_size"]),
        "--partition_name", original["partition_name"],
        "--hash_algorithm", original["hash_algorithm"],
        "--salt", original["salt"],
        "--algorithm", "NONE",
        "--rollback_index", str(original["rollback_index"]),
        "--rollback_index_location", str(original["rollback_index_location"]),
        "--flags", str(original["flags"]),
    ]
    for key, value in original["props"]:
        command += ["--prop", f"{key}:{value}"]
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lunaris-avb-", dir=output.parent) as temporary:
        candidate = Path(temporary) / "boot.img"
        shutil.copyfile(image, candidate)
        subprocess.run([*command, "--image", str(candidate)], check=True)
        result = read_footer(avbtool, candidate)
        validate_footer(original, payload, candidate, result)
        # avbtool's own check; it resolves the "boot" descriptor to boot.img beside the image.
        subprocess.run([sys.executable, str(avbtool_path), "verify_image", "--image", str(candidate)],
                       check=True)
        shutil.copyfile(candidate, output)

    report = {
        "schema_version": 1,
        "avbtool_sha256": hashlib.sha256(avbtool_path.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "original_footer": original,
        "original_footer_digest_matches_its_payload": original_payload_matches,
        "new_footer": result,
        "preserved_fields": list(PRESERVED),
        "note": "Unsigned footer; the device vbmeta partition is unchanged and may record a different boot digest.",
    }
    output.with_suffix(".avb.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True, help="compact repacked boot image")
    parser.add_argument("--avbtool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verified_original(args.base)
    report = add_footer(args.base, args.image, args.avbtool, args.output)
    print(json.dumps({k: report[k] for k in ("output_sha256", "original_footer_digest_matches_its_payload")}, indent=2))


if __name__ == "__main__":
    main()
