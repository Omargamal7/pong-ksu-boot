"""Pure-Python CrAU v2 OTA payload extractor, parallel XZ/ZERO repacker, and RSA-2048 signer."""

from __future__ import annotations

import argparse
import base64
import bz2
import concurrent.futures
import hashlib
import io
import json
import lzma
import os
import struct
import zipfile
from pathlib import Path

CRAU_MAGIC = b"CrAU"
CRAU_VERSION = 2
BLOCK_SIZE = 4096
CHUNK_BLOCKS = 512  # 2 MiB per operation chunk

# InstallOperation.Type enum values from update_metadata.proto
OP_REPLACE = 0
OP_REPLACE_BZ = 1
OP_ZERO = 6
OP_REPLACE_XZ = 8

# DigestInfo prefix for SHA-256 in EMSA-PKCS1-v1_5 (RFC 8017)
PKCS1_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(buf: bytes | memoryview, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7


def parse_proto_fields(buf: bytes | memoryview) -> list[tuple[int, int, int | bytes]]:
    pos = 0
    end = len(buf)
    fields: list[tuple[int, int, int | bytes]] = []
    while pos < end:
        tag, pos = decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(buf, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:
            length, pos = decode_varint(buf, pos)
            fields.append((field_num, wire_type, bytes(buf[pos : pos + length])))
            pos += length
        elif wire_type == 5:
            fields.append((field_num, wire_type, bytes(buf[pos : pos + 4])))
            pos += 4
        elif wire_type == 1:
            fields.append((field_num, wire_type, bytes(buf[pos : pos + 8])))
            pos += 8
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
    return fields


def serialize_proto_fields(fields: list[tuple[int, int, int | bytes]]) -> bytes:
    out = bytearray()
    for field_num, wire_type, val in fields:
        out.extend(encode_varint((field_num << 3) | wire_type))
        if wire_type == 0:
            assert isinstance(val, int)
            out.extend(encode_varint(val))
        elif wire_type == 2:
            assert isinstance(val, (bytes, bytearray))
            out.extend(encode_varint(len(val)))
            out.extend(val)
        elif wire_type in (1, 5):
            assert isinstance(val, (bytes, bytearray))
            out.extend(val)
        else:
            raise ValueError(f"unsupported protobuf wire type: {wire_type}")
    return bytes(out)


def pkcs1_v15_sha256_pad(digest: bytes, key_bytes: int = 256) -> bytes:
    if len(digest) != 32:
        raise ValueError("expected 32-byte SHA-256 digest")
    t = PKCS1_SHA256_PREFIX + digest
    ps_len = key_bytes - len(t) - 3
    if ps_len < 8:
        raise ValueError("RSA key size too small")
    return b"\x00\x01" + (b"\xff" * ps_len) + b"\x00" + t


def sign_crau_digest(digest: bytes, n: int, d: int) -> bytes:
    """Create a 267-byte CrAU Signatures protobuf message for a 32-byte SHA-256 digest."""
    em = pkcs1_v15_sha256_pad(digest, 256)
    sig_int = pow(int.from_bytes(em, "big"), d, n)
    sig_bytes = sig_int.to_bytes(256, "big")
    inner = serialize_proto_fields([
        (2, 2, sig_bytes),
        (3, 5, struct.pack("<I", 256)),
    ])
    return serialize_proto_fields([(1, 2, inner)])


def verify_crau_signature(sig_proto: bytes, digest: bytes, n: int, e: int = 65537) -> bool:
    outer = parse_proto_fields(sig_proto)
    if not outer or outer[0][0] != 1:
        return False
    inner = dict((f, v) for f, _, v in parse_proto_fields(outer[0][2]))  # type: ignore[arg-type]
    sig_bytes = inner.get(2)
    if not isinstance(sig_bytes, bytes) or len(sig_bytes) != 256:
        return False
    em = pow(int.from_bytes(sig_bytes, "big"), e, n).to_bytes(256, "big")
    return em == pkcs1_v15_sha256_pad(digest, 256)


def parse_partition_update(pu_bytes: bytes) -> dict:
    fields = parse_proto_fields(pu_bytes)
    name = ""
    info_size = 0
    info_hash = b""
    ops: list[bytes] = []
    other_fields: list[tuple[int, int, int | bytes]] = []
    for fn, wt, val in fields:
        if fn == 1 and isinstance(val, bytes):
            name = val.decode("utf-8")
        elif fn == 7 and isinstance(val, bytes):
            for ifn, _, ival in parse_proto_fields(val):
                if ifn == 1 and isinstance(ival, int):
                    info_size = ival
                elif ifn == 2 and isinstance(ival, bytes):
                    info_hash = ival
        elif fn == 8 and isinstance(val, bytes):
            ops.append(val)
        elif fn == 19:
            # estimate_cow_size handled during rebuild
            pass
        else:
            other_fields.append((fn, wt, val))
    return {
        "name": name,
        "size": info_size,
        "hash": info_hash,
        "ops": ops,
        "other_fields": other_fields,
    }


def extract_partitions_from_zip(
    zip_path: Path,
    partitions: set[str],
    output_dir: Path,
) -> dict[str, dict]:
    """Extract specified partitions from payload.bin inside an OTA zip and verify SHA-256."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        info = zf.getinfo("payload.bin")
        if info.compress_type != zipfile.ZIP_STORED:
            raise ValueError("payload.bin must be ZIP_STORED")

    with open(zip_path, "rb") as f:
        f.seek(info.header_offset)
        lhdr = f.read(30)
        fn_len, ex_len = struct.unpack("<HH", lhdr[26:30])
        payload_start = info.header_offset + 30 + fn_len + ex_len
        f.seek(payload_start)
        hdr = f.read(24)
        magic, ver, manifest_size, sig_size = struct.unpack(">4sQQI", hdr)
        if magic != CRAU_MAGIC or ver != CRAU_VERSION:
            raise ValueError(f"unexpected CrAU header: {magic!r} v{ver}")
        manifest = f.read(manifest_size)
        blob_start = payload_start + 24 + manifest_size + sig_size

        extracted: dict[str, dict] = {}
        for fn, _, val in parse_proto_fields(manifest):
            if fn != 13 or not isinstance(val, bytes):
                continue
            pu = parse_partition_update(val)
            pname = pu["name"]
            if pname not in partitions:
                continue
            out_file = output_dir / f"{pname}.img"
            with open(out_file, "wb") as out_f:
                out_f.truncate(pu["size"])
                for op_bytes in pu["ops"]:
                    op_fields = parse_proto_fields(op_bytes)
                    op_type = 0
                    data_offset = 0
                    data_length = 0
                    dst_extents: list[tuple[int, int]] = []
                    expected_op_hash = b""
                    for ofn, _, oval in op_fields:
                        if ofn == 1 and isinstance(oval, int):
                            op_type = oval
                        elif ofn == 2 and isinstance(oval, int):
                            data_offset = oval
                        elif ofn == 3 and isinstance(oval, int):
                            data_length = oval
                        elif ofn == 6 and isinstance(oval, bytes):
                            ext = dict((efn, eval_) for efn, _, eval_ in parse_proto_fields(oval))
                            dst_extents.append((int(ext[1]), int(ext[2])))  # type: ignore[arg-type]
                        elif ofn == 8 and isinstance(oval, bytes):
                            expected_op_hash = oval

                    if op_type == OP_ZERO:
                        continue
                    f.seek(blob_start + data_offset)
                    raw = f.read(data_length)
                    if expected_op_hash and hashlib.sha256(raw).digest() != expected_op_hash:
                        raise ValueError(f"corrupt operation data hash in partition {pname}")
                    if op_type == OP_REPLACE:
                        dec = raw
                    elif op_type == OP_REPLACE_XZ:
                        dec = lzma.decompress(raw)
                    elif op_type == OP_REPLACE_BZ:
                        dec = bz2.decompress(raw)
                    else:
                        raise ValueError(f"unsupported operation type {op_type} in {pname}")

                    pos = 0
                    for start_blk, num_blks in dst_extents:
                        nbytes = num_blks * BLOCK_SIZE
                        out_f.seek(start_blk * BLOCK_SIZE)
                        out_f.write(dec[pos : pos + nbytes])
                        pos += nbytes

            # Verify full partition SHA-256
            h = hashlib.sha256()
            with open(out_file, "rb") as vf:
                while chunk := vf.read(4 * 1024 * 1024):
                    h.update(chunk)
            if h.digest() != pu["hash"]:
                raise ValueError(
                    f"extracted partition {pname} hash mismatch: "
                    f"{h.hexdigest()} != {pu['hash'].hex()}"
                )
            extracted[pname] = {
                "path": str(out_file),
                "size": pu["size"],
                "sha256": h.hexdigest(),
            }
    return extracted


def _compress_chunk_worker(args: tuple[int, int, bytes]) -> tuple[int, int, int, bytes, bytes]:
    start_block, num_blocks, raw_chunk = args
    if raw_chunk == b"\x00" * len(raw_chunk):
        return start_block, num_blocks, OP_ZERO, b"", b""
    comp = lzma.compress(raw_chunk, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE, preset=3)
    if len(comp) < len(raw_chunk):
        return start_block, num_blocks, OP_REPLACE_XZ, comp, hashlib.sha256(comp).digest()
    return start_block, num_blocks, OP_REPLACE, raw_chunk, hashlib.sha256(raw_chunk).digest()


def encode_partition_image(
    image_path: Path,
    blob_out_file: io.BufferedWriter,
    current_offset: int,
    max_workers: int | None = None,
) -> tuple[int, bytes, list[bytes], int, int]:
    """Compress a partition image into CrAU operations and append blobs to blob_out_file."""
    file_size = image_path.stat().st_size
    if file_size % BLOCK_SIZE != 0:
        raise ValueError(f"{image_path} size {file_size} is not a multiple of {BLOCK_SIZE}")
    total_blocks = file_size // BLOCK_SIZE
    full_hash = hashlib.sha256()

    def chunk_generator():
        with open(image_path, "rb") as f:
            blk = 0
            while blk < total_blocks:
                nblks = min(CHUNK_BLOCKS, total_blocks - blk)
                data = f.read(nblks * BLOCK_SIZE)
                full_hash.update(data)
                yield (blk, nblks, data)
                blk += nblks

    ops_proto: list[bytes] = []
    cow_estimate = 0
    with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
        for start_blk, num_blks, op_type, blob, blob_hash in pool.map(
            _compress_chunk_worker, chunk_generator(), chunksize=4
        ):
            ext_proto = serialize_proto_fields([(1, 0, start_blk), (2, 0, num_blks)])
            if op_type == OP_ZERO:
                op_fields: list[tuple[int, int, int | bytes]] = [
                    (1, 0, OP_ZERO),
                    (6, 2, ext_proto),
                ]
            else:
                blob_out_file.write(blob)
                op_fields = [
                    (1, 0, op_type),
                    (2, 0, current_offset),
                    (3, 0, len(blob)),
                    (6, 2, ext_proto),
                    (8, 2, blob_hash),
                ]
                current_offset += len(blob)
                cow_estimate += len(blob)
            ops_proto.append(serialize_proto_fields(op_fields))

    return file_size, full_hash.digest(), ops_proto, current_offset, cow_estimate


def build_partition_update_proto(
    name: str,
    other_fields: list[tuple[int, int, int | bytes]],
    new_size: int,
    new_hash: bytes,
    ops_proto: list[bytes],
    cow_estimate: int | None,
) -> bytes:
    fields: list[tuple[int, int, int | bytes]] = [(1, 2, name.encode("utf-8"))]
    for fn, wt, val in other_fields:
        if fn in (2, 3, 4):
            fields.append((fn, wt, val))
    info_proto = serialize_proto_fields([(1, 0, new_size), (2, 2, new_hash)])
    fields.append((7, 2, info_proto))
    for op in ops_proto:
        fields.append((8, 2, op))
    for fn, wt, val in other_fields:
        if fn not in (2, 3, 4):
            fields.append((fn, wt, val))
    if cow_estimate is not None:
        fields.append((19, 0, cow_estimate))
    return serialize_proto_fields(fields)


def repack_ota_zip(
    source_zip: Path,
    replacement_images: dict[str, Path],
    output_zip: Path,
    rsa_n: int,
    rsa_d: int,
    otacert_pem: bytes,
    max_workers: int | None = None,
) -> dict:
    """Repack an A/B OTA zip with modified partition images and sign with RSA-2048."""
    with zipfile.ZipFile(source_zip, "r") as zf:
        info = zf.getinfo("payload.bin")
        orig_metadata_txt = zf.read("META-INF/com/android/metadata").decode("utf-8")
        orig_metadata_pb = zf.read("META-INF/com/android/metadata.pb")
        orig_apex_info_pb = zf.read("apex_info.pb")
        orig_care_map_pb = zf.read("care_map.pb")

    blobs_tmp = output_zip.with_suffix(".blobs.tmp")
    new_top_fields: list[tuple[int, int, int | bytes]] = []
    partition_summaries: dict[str, dict] = {}

    try:
        with open(source_zip, "rb") as src_f, open(blobs_tmp, "wb") as blob_out:
            src_f.seek(info.header_offset)
            lhdr = src_f.read(30)
            fn_len, ex_len = struct.unpack("<HH", lhdr[26:30])
            payload_start = info.header_offset + 30 + fn_len + ex_len
            src_f.seek(payload_start)
            hdr = src_f.read(24)
            magic, ver, manifest_size, sig_size = struct.unpack(">4sQQI", hdr)
            manifest = src_f.read(manifest_size)
            blob_start = payload_start + 24 + manifest_size + sig_size

            current_offset = 0
            orig_fields = parse_proto_fields(manifest)

            for fn, wt, val in orig_fields:
                if fn in (4, 5):
                    # signatures_offset (4) and signatures_size (5) will be set after all blobs
                    continue
                if fn != 13 or not isinstance(val, bytes):
                    new_top_fields.append((fn, wt, val))
                    continue

                pu = parse_partition_update(val)
                pname = pu["name"]
                has_cow = any(f == 19 for f, _, _ in parse_proto_fields(val))

                if pname in replacement_images:
                    img_path = replacement_images[pname]
                    new_sz, new_hash, new_ops, current_offset, cow_est = encode_partition_image(
                        img_path, blob_out, current_offset, max_workers=max_workers
                    )
                    pu_proto = build_partition_update_proto(
                        pname,
                        pu["other_fields"],
                        new_sz,
                        new_hash,
                        new_ops,
                        cow_est if has_cow else None,
                    )
                    new_top_fields.append((13, 2, pu_proto))
                    partition_summaries[pname] = {
                        "modified": True,
                        "size": new_sz,
                        "sha256": new_hash.hex(),
                        "operations": len(new_ops),
                    }
                else:
                    # Copy compressed operation blobs verbatim from source payload.bin
                    copied_ops: list[bytes] = []
                    cow_est = 0
                    for op_bytes in pu["ops"]:
                        op_fields = parse_proto_fields(op_bytes)
                        op_dict = dict((k, v) for k, _, v in op_fields)
                        op_type = int(op_dict.get(1, 0))  # type: ignore[arg-type]
                        if op_type == OP_ZERO or 2 not in op_dict:
                            copied_ops.append(op_bytes)
                            continue
                        old_off = int(op_dict[2])  # type: ignore[arg-type]
                        dlen = int(op_dict[3])  # type: ignore[arg-type]
                        src_f.seek(blob_start + old_off)
                        raw_blob = src_f.read(dlen)
                        blob_out.write(raw_blob)
                        rebuilt_op = [
                            (k, w, current_offset if k == 2 else v)
                            for k, w, v in op_fields
                        ]
                        copied_ops.append(serialize_proto_fields(rebuilt_op))
                        current_offset += dlen
                        cow_est += dlen

                    orig_cow = next(
                        (int(v) for f, _, v in parse_proto_fields(val) if f == 19),
                        None,
                    )
                    pu_proto = build_partition_update_proto(
                        pname,
                        pu["other_fields"],
                        pu["size"],
                        pu["hash"],
                        copied_ops,
                        orig_cow if has_cow else None,
                    )
                    new_top_fields.append((13, 2, pu_proto))
                    partition_summaries[pname] = {
                        "modified": False,
                        "size": pu["size"],
                        "sha256": pu["hash"].hex(),
                        "operations": len(copied_ops),
                    }

            # Insert signatures_offset (4) and signatures_size (5) right after block_size (3)
            final_fields: list[tuple[int, int, int | bytes]] = []
            inserted_sig_fields = False
            for fn, wt, val in new_top_fields:
                final_fields.append((fn, wt, val))
                if fn == 3 and not inserted_sig_fields:
                    final_fields.append((4, 0, current_offset))
                    final_fields.append((5, 0, 267))
                    inserted_sig_fields = True
            if not inserted_sig_fields:
                final_fields.insert(0, (4, 0, current_offset))
                final_fields.insert(1, (5, 0, 267))

            new_manifest = serialize_proto_fields(final_fields)
            new_hdr = struct.pack(">4sQQI", CRAU_MAGIC, CRAU_VERSION, len(new_manifest), 267)
            meta_bytes = new_hdr + new_manifest
            meta_hash = hashlib.sha256(meta_bytes).digest()
            meta_sig = sign_crau_digest(meta_hash, rsa_n, rsa_d)

        # Compute payload signature over (new_hdr + new_manifest + data_blobs)
        pay_hasher = hashlib.sha256(meta_bytes)
        with open(blobs_tmp, "rb") as bf:
            while chunk := bf.read(8 * 1024 * 1024):
                pay_hasher.update(chunk)
        pay_sig = sign_crau_digest(pay_hasher.digest(), rsa_n, rsa_d)

        # Compute full payload.bin hash and size
        full_hasher = hashlib.sha256(meta_bytes)
        full_hasher.update(meta_sig)
        with open(blobs_tmp, "rb") as bf:
            while chunk := bf.read(8 * 1024 * 1024):
                full_hasher.update(chunk)
        full_hasher.update(pay_sig)

        payload_size = len(meta_bytes) + len(meta_sig) + current_offset + len(pay_sig)
        metadata_size = len(meta_bytes)
        metadata_plus_sig_size = len(meta_bytes) + len(meta_sig)
        file_hash_b64 = base64.b64encode(full_hasher.digest()).decode("ascii")
        meta_hash_b64 = base64.b64encode(meta_hash).decode("ascii")

        payload_props = (
            f"FILE_HASH={file_hash_b64}\n"
            f"FILE_SIZE={payload_size}\n"
            f"METADATA_HASH={meta_hash_b64}\n"
            f"METADATA_SIZE={metadata_size}\n"
        ).encode("utf-8")

        # Update care_map.pb block counts for any modified partitions
        care_map_fields: list[tuple[int, int, int | bytes]] = []
        for cfn, cwt, cval in parse_proto_fields(orig_care_map_pb):
            if cfn == 1 and isinstance(cval, bytes):
                sub_fields = parse_proto_fields(cval)
                pname_bytes = next((v for k, _, v in sub_fields if k == 1 and isinstance(v, bytes)), b"")
                pname_str = pname_bytes.decode("utf-8", errors="replace")
                if pname_str in partition_summaries and partition_summaries[pname_str]["modified"]:
                    nblks = partition_summaries[pname_str]["size"] // BLOCK_SIZE
                    new_sub = [
                        (k, w, f"2,0,{nblks}".encode("utf-8") if k == 2 else v)
                        for k, w, v in sub_fields
                    ]
                    care_map_fields.append((cfn, cwt, serialize_proto_fields(new_sub)))
                    continue
            care_map_fields.append((cfn, cwt, cval))
        updated_care_map_pb = serialize_proto_fields(care_map_fields)

        # Write the output zip with STORED entries and exact offset calculation
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_STORED) as out_zf:
            # Reserve placeholder metadata with fixed length equal to original
            out_zf.writestr("META-INF/com/android/metadata", " " * len(orig_metadata_txt))
            out_zf.writestr("META-INF/com/android/metadata.pb", orig_metadata_pb)
            out_zf.writestr("apex_info.pb", orig_apex_info_pb)
            out_zf.writestr("care_map.pb", updated_care_map_pb)

            with out_zf.open("payload.bin", "w", force_zip64=True) as zpf:
                zpf.write(meta_bytes)
                zpf.write(meta_sig)
                with open(blobs_tmp, "rb") as bf:
                    while chunk := bf.read(8 * 1024 * 1024):
                        zpf.write(chunk)
                zpf.write(pay_sig)

            out_zf.writestr("payload_properties.txt", payload_props)
            out_zf.writestr("META-INF/com/android/otacert", otacert_pem)

        # Compute exact data offsets inside output_zip and patch metadata + metadata.pb in-place
        entry_ranges: dict[str, tuple[int, int, int]] = {}
        with zipfile.ZipFile(output_zip, "r") as check_zf, open(output_zip, "rb") as raw_zf:
            for zinfo in check_zf.infolist():
                raw_zf.seek(zinfo.header_offset)
                lh = raw_zf.read(30)
                flen, elen = struct.unpack("<HH", lh[26:30])
                dstart = zinfo.header_offset + 30 + flen + elen
                entry_ranges[zinfo.filename] = (zinfo.header_offset, dstart, zinfo.file_size)

        _, p_off, p_sz = entry_ranges["payload.bin"]
        _, pp_off, pp_sz = entry_ranges["payload_properties.txt"]
        _, ap_off, ap_sz = entry_ranges["apex_info.pb"]
        _, cm_off, cm_sz = entry_ranges["care_map.pb"]
        md_hdr_off, md_off, md_sz = entry_ranges["META-INF/com/android/metadata"]
        mpb_hdr_off, mpb_off, mpb_sz = entry_ranges["META-INF/com/android/metadata.pb"]

        ota_prop_files = (
            f"payload_metadata.bin:{p_off}:{metadata_plus_sig_size},"
            f"payload.bin:{p_off}:{p_sz},"
            f"payload_properties.txt:{pp_off}:{pp_sz},"
            f"apex_info.pb:{ap_off}:{ap_sz},"
            f"care_map.pb:{cm_off}:{cm_sz},"
            f"metadata:{md_off}:{md_sz},"
            f"metadata.pb:{mpb_off}:{mpb_sz}"
        )
        ota_stream_files = (
            f"payload.bin:{p_off}:{p_sz},"
            f"payload_properties.txt:{pp_off}:{pp_sz},"
            f"apex_info.pb:{ap_off}:{ap_sz},"
            f"care_map.pb:{cm_off}:{cm_sz},"
            f"metadata:{md_off}:{md_sz},"
            f"metadata.pb:{mpb_off}:{mpb_sz}"
        )

        updated_lines: list[str] = []
        for line in orig_metadata_txt.splitlines():
            if line.startswith("ota-property-files="):
                val = f"ota-property-files={ota_prop_files}"
                updated_lines.append(val.ljust(len(line)))
            elif line.startswith("ota-streaming-property-files="):
                val = f"ota-streaming-property-files={ota_stream_files}"
                updated_lines.append(val.ljust(len(line)))
            else:
                updated_lines.append(line)
        new_meta_txt = "\n".join(updated_lines) + "\n"
        if len(new_meta_txt) < md_sz:
            new_meta_txt = new_meta_txt[:-1] + (" " * (md_sz - len(new_meta_txt))) + "\n"

        # Update ota-property-files and ota-streaming-property-files inside metadata.pb
        mpb_fields: list[tuple[int, int, int | bytes]] = []
        for mfn, mwt, mval in parse_proto_fields(orig_metadata_pb):
            if mfn == 4 and isinstance(mval, bytes):
                kv = parse_proto_fields(mval)
                k_bytes = next((v for k, _, v in kv if k == 1 and isinstance(v, bytes)), b"")
                v_bytes = next((v for k, _, v in kv if k == 2 and isinstance(v, bytes)), b"")
                if k_bytes == b"ota-property-files":
                    padded = ota_prop_files.encode("utf-8").ljust(len(v_bytes), b" ")
                    mpb_fields.append((4, 2, serialize_proto_fields([(1, 2, k_bytes), (2, 2, padded)])))
                    continue
                if k_bytes == b"ota-streaming-property-files":
                    padded = ota_stream_files.encode("utf-8").ljust(len(v_bytes), b" ")
                    mpb_fields.append((4, 2, serialize_proto_fields([(1, 2, k_bytes), (2, 2, padded)])))
                    continue
            mpb_fields.append((mfn, mwt, mval))
        new_mpb_bytes = serialize_proto_fields(mpb_fields)

        def patch_zip_entry_in_place(
            rwf: io.BufferedRandom,
            entry_name: bytes,
            hdr_offset: int,
            data_offset: int,
            expected_size: int,
            new_bytes: bytes,
        ) -> None:
            if len(new_bytes) != expected_size:
                return
            crc = zipfile.crc32(new_bytes) & 0xFFFFFFFF
            rwf.seek(data_offset)
            rwf.write(new_bytes)
            rwf.seek(hdr_offset + 14)
            rwf.write(struct.pack("<I", crc))
            tail_base = max(0, output_zip.stat().st_size - 65536)
            rwf.seek(tail_base)
            tail = rwf.read()
            cd_pos = tail.find(b"PK\x01\x02")
            while cd_pos != -1:
                fn_l = struct.unpack_from("<H", tail, cd_pos + 28)[0]
                name_bytes = tail[cd_pos + 46 : cd_pos + 46 + fn_l]
                if name_bytes == entry_name:
                    rwf.seek(tail_base + cd_pos + 16)
                    rwf.write(struct.pack("<I", crc))
                    break
                cd_pos = tail.find(b"PK\x01\x02", cd_pos + 1)

        meta_bytes_final = new_meta_txt.encode("utf-8")
        with open(output_zip, "r+b") as rwf:
            patch_zip_entry_in_place(
                rwf, b"META-INF/com/android/metadata", md_hdr_off, md_off, md_sz, meta_bytes_final
            )
            patch_zip_entry_in_place(
                rwf, b"META-INF/com/android/metadata.pb", mpb_hdr_off, mpb_off, mpb_sz, new_mpb_bytes
            )

        return {
            "output_zip": str(output_zip),
            "output_zip_size": output_zip.stat().st_size,
            "payload_size": payload_size,
            "payload_sha256": full_hasher.hexdigest(),
            "metadata_size": metadata_size,
            "metadata_sha256": meta_hash.hex(),
            "partitions": partition_summaries,
        }
    finally:
        if blobs_tmp.exists():
            blobs_tmp.unlink()


def load_rsa_private_key_numbers(key_json_path: Path) -> tuple[int, int, bytes]:
    data = json.loads(key_json_path.read_text(encoding="utf-8"))
    return int(data["n"]), int(data["d"]), data["cert_pem"].encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    ext_p = sub.add_parser("extract", help="Extract specified partitions from OTA zip")
    ext_p.add_argument("--zip", required=True, type=Path)
    ext_p.add_argument("--partitions", required=True, nargs="+")
    ext_p.add_argument("--output-dir", required=True, type=Path)
    ext_p.add_argument("--report", required=True, type=Path)

    rep_p = sub.add_parser("repack", help="Repack OTA zip with modified partition images")
    rep_p.add_argument("--source-zip", required=True, type=Path)
    rep_p.add_argument("--images-dir", required=True, type=Path)
    rep_p.add_argument("--partitions", required=True, nargs="+")
    rep_p.add_argument("--key-json", required=True, type=Path)
    rep_p.add_argument("--output-zip", required=True, type=Path)
    rep_p.add_argument("--report", required=True, type=Path)
    rep_p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)

    args = parser.parse_args()
    if args.cmd == "extract":
        res = extract_partitions_from_zip(args.zip, set(args.partitions), args.output_dir)
        args.report.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    elif args.cmd == "repack":
        replacements = {p: args.images_dir / f"{p}.img" for p in args.partitions}
        n, d, cert_pem = load_rsa_private_key_numbers(args.key_json)
        res = repack_ota_zip(
            args.source_zip,
            replacements,
            args.output_zip,
            n,
            d,
            cert_pem,
            max_workers=args.jobs,
        )
        args.report.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
