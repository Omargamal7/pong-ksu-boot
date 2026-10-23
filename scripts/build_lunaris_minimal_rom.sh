#!/usr/bin/env bash
# Repack official Lunaris-AOSP 3.6 (Pong) into a minimal Pico-GApps custom ROM zip.
# Runs on an x86_64 Linux cloud runner (GitHub Actions / Codespaces), never on the phone.
set -euo pipefail

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

OFFICIAL_ROM_FILENAME="Lunaris-AOSP-Pong-OFFICIAL-3.6-Gapps-2025120905.zip"
OFFICIAL_ROM_SHA256="1462e100777330b7fd496fafde705dae6be582dc57c8bb2fea66474c487722ba"
OFFICIAL_ROM_URL="https://sourceforge.net/projects/ghosuto/files/Pong/${OFFICIAL_ROM_FILENAME}/download"
OUTPUT_ROM_FILENAME="Lunaris-AOSP-Pong-Minimal-3.6-Pico.zip"

SOURCE_ZIP=""
BOOT_IMAGE=""
WORK_DIR="$REPO_DIR/work/minimal-rom"
OUTPUT_DIR="$REPO_DIR/dist/minimal-rom"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-zip)
      [ "$#" -ge 2 ] || fail "--source-zip requires a path argument"
      SOURCE_ZIP="$2"
      shift 2
      ;;
    --boot-image)
      [ "$#" -ge 2 ] || fail "--boot-image requires a path argument"
      BOOT_IMAGE="$2"
      shift 2
      ;;
    --work-dir)
      [ "$#" -ge 2 ] || fail "--work-dir requires a path argument"
      WORK_DIR="$2"
      shift 2
      ;;
    --output-dir)
      [ "$#" -ge 2 ] || fail "--output-dir requires a path argument"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --jobs)
      [ "$#" -ge 2 ] || fail "--jobs requires a count argument"
      JOBS="$2"
      shift 2
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[ "$(uname -s)" = "Linux" ] || fail "this builder requires a Linux host"
[ "$(uname -m)" = "x86_64" ] || fail "this cloud build requires Linux x86_64 (never build on the phone)"

SUDO=()
if [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null 2>&1 || fail "root or sudo is required to mount partition images"
  SUDO=(sudo)
fi

for cmd in python3 openssl e2fsck resize2fs tune2fs mkfs.erofs mount umount sha256sum curl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "missing required command: $cmd"
done

mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
WORK_DIR="$(realpath "$WORK_DIR")"
OUTPUT_DIR="$(realpath "$OUTPUT_DIR")"

IMAGES_DIR="$WORK_DIR/images"
MNT_DIR="$WORK_DIR/mnt"
VENDOR_STAGE="$WORK_DIR/vendor_stage"
mkdir -p "$IMAGES_DIR" "$MNT_DIR"

cleanup_mounts() {
  local part mnt
  for part in system system_ext product vendor; do
    mnt="$MNT_DIR/$part"
    if mountpoint -q "$mnt" 2>/dev/null; then
      "${SUDO[@]}" umount -lf "$mnt" 2>/dev/null || true
    fi
  done
}
trap cleanup_mounts EXIT

if [ -z "$SOURCE_ZIP" ]; then
  SOURCE_ZIP="$WORK_DIR/$OFFICIAL_ROM_FILENAME"
fi

if [ ! -f "$SOURCE_ZIP" ]; then
  printf 'Downloading official Lunaris 3.6 ROM zip from %s ...\n' "$OFFICIAL_ROM_URL"
  curl --fail --location --retry 3 --connect-timeout 30 \
    "$OFFICIAL_ROM_URL" --output "$SOURCE_ZIP.partial"
  mv "$SOURCE_ZIP.partial" "$SOURCE_ZIP"
fi

printf 'Verifying SHA-256 of source ROM zip (%s)...\n' "$SOURCE_ZIP"
printf '%s  %s\n' "$OFFICIAL_ROM_SHA256" "$SOURCE_ZIP" | sha256sum --check --strict

EXTRACT_PARTITIONS=(system system_ext product vendor vbmeta_system vbmeta_vendor)
MODIFIED_PARTITIONS=(system system_ext product vendor vbmeta_system vbmeta_vendor)

printf 'Extracting partitions from payload.bin: %s\n' "${EXTRACT_PARTITIONS[*]}"
python3 "$SCRIPT_DIR/lunaris_payload.py" extract \
  --zip "$SOURCE_ZIP" \
  --partitions "${EXTRACT_PARTITIONS[@]}" \
  --output-dir "$IMAGES_DIR" \
  --report "$WORK_DIR/extract-report.json"

if [ -n "$BOOT_IMAGE" ]; then
  [ -f "$BOOT_IMAGE" ] || fail "specified --boot-image does not exist: $BOOT_IMAGE"
  boot_size="$(stat -c '%s' "$BOOT_IMAGE")"
  [ "$boot_size" -eq 100663296 ] || fail "expected 100663296-byte Pong boot.img, got $boot_size bytes"
  cp -f "$BOOT_IMAGE" "$IMAGES_DIR/boot.img"
  MODIFIED_PARTITIONS+=(boot)
  printf 'Included custom boot image: %s\n' "$BOOT_IMAGE"
fi

# Generate ephemeral RSA-2048 OTA signing key and X.509 certificate
OTA_KEY="$WORK_DIR/ota.key"
OTA_CRT="$WORK_DIR/ota.crt"
KEY_JSON="$WORK_DIR/ota_key.json"
openssl req -x509 -newkey rsa:2048 -keyout "$OTA_KEY" -out "$OTA_CRT" \
  -days 3650 -nodes -subj "/CN=Lunaris-Pong-Minimal-OTA" >/dev/null 2>&1

python3 - "$OTA_KEY" "$OTA_CRT" "$KEY_JSON" <<'PY'
import json, pathlib, re, subprocess, sys
key_path, crt_path, out_path = map(pathlib.Path, sys.argv[1:4])
text = subprocess.check_output(
    ["openssl", "rsa", "-in", str(key_path), "-text", "-noout"],
    text=True,
)
def parse_hex_block(label: str, src: str) -> int:
    m = re.search(rf"{re.escape(label)}:\s*\n((?:[ \t]+[0-9a-f:]+\n)+)", src)
    if not m:
        raise SystemExit(f"failed to parse {label} from openssl rsa output")
    hex_str = re.sub(r"[^0-9a-f]", "", m.group(1))
    return int(hex_str, 16)

n = parse_hex_block("modulus", text)
d = parse_hex_block("privateExponent", text)
if pow(pow(42, 65537, n), d, n) != 42:
    raise SystemExit("RSA key pair consistency check failed")
payload = {
    "n": str(n),
    "d": str(d),
    "cert_pem": crt_path.read_text(encoding="utf-8"),
}
out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

# Patch vbmeta_system and vbmeta_vendor flags to 3 (disable verity / hashtree checks)
for vb_part in vbmeta_system vbmeta_vendor; do
  printf 'Patching AVB verification flags on %s.img...\n' "$vb_part"
  python3 "$SCRIPT_DIR/lunaris_rom_profile.py" patch-vbmeta --image "$IMAGES_DIR/$vb_part.img"
done

# Strip and tune ext4 partitions (system, system_ext, product)
for part in system system_ext product; do
  img="$IMAGES_DIR/$part.img"
  mnt="$MNT_DIR/$part"
  mkdir -p "$mnt"

  printf 'Preparing ext4 image for %s...\n' "$part"
  # Strip trailing AVB hashtree footer bytes beyond the ext4 filesystem block count
  block_count="$(tune2fs -l "$img" | awk -F: '/^Block count:/ {gsub(/ /, "", $2); print $2}')"
  block_size="$(tune2fs -l "$img" | awk -F: '/^Block size:/ {gsub(/ /, "", $2); print $2}')"
  [ -n "$block_count" ] && [ -n "$block_size" ] || fail "failed to read ext4 geometry for $part"
  fs_bytes=$((block_count * block_size))
  truncate -s "$fs_bytes" "$img"

  e2fsck -fy "$img" >/dev/null 2>&1 || [ "$?" -le 1 ] || fail "e2fsck pre-check failed on $part.img"
  # Add 16 MiB temporary headroom so mounting rw and writing init/prop updates never hits ENOSPC
  truncate -s $((fs_bytes + 16 * 1024 * 1024)) "$img"
  resize2fs "$img" >/dev/null 2>&1

  "${SUDO[@]}" mount -t ext4 -o loop,rw "$img" "$mnt"
  printf 'Applying minimal ROM profile to %s...\n' "$part"
  "${SUDO[@]}" python3 "$SCRIPT_DIR/lunaris_rom_profile.py" apply \
    --partition "$part" \
    --mount-dir "$mnt" \
    --report "$WORK_DIR/report-$part.json"

  if [ "$part" = "system" ]; then
    # Update /system/etc/security/otacerts.zip in-place so the system trusts our OTA certificate
    otacerts_path="$mnt/system/etc/security/otacerts.zip"
    if [ -f "$otacerts_path" ]; then
      "${SUDO[@]}" python3 - "$otacerts_path" "$OTA_CRT" <<'PY'
import io, pathlib, sys, zipfile
target, cert = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("releasekey.x509.pem", cert.read_bytes())
with open(target, "r+b") as f:
    f.seek(0)
    f.truncate(0)
    f.write(buf.getvalue())
PY
    fi
  fi

  # Zero out free space so freed blocks encode as compact OP_ZERO chunks in payload.bin
  "${SUDO[@]}" dd if=/dev/zero of="$mnt/.zero_fill" bs=4M status=none 2>/dev/null || true
  sync
  "${SUDO[@]}" rm -f "$mnt/.zero_fill"
  sync
  "${SUDO[@]}" umount "$mnt"

  e2fsck -fy "$img" >/dev/null 2>&1 || [ "$?" -le 1 ] || fail "e2fsck post-umount failed on $part.img"
  resize2fs -M "$img" >/dev/null 2>&1
  e2fsck -fy "$img" >/dev/null 2>&1 || [ "$?" -le 1 ] || fail "e2fsck post-shrink failed on $part.img"

  new_blocks="$(tune2fs -l "$img" | awk -F: '/^Block count:/ {gsub(/ /, "", $2); print $2}')"
  truncate -s $((new_blocks * 4096)) "$img"
  printf 'Finished %s.img (shrunk to %d bytes)\n' "$part" "$((new_blocks * 4096))"
done

# Strip and tune erofs partition (vendor)
printf 'Mounting and staging vendor.img (erofs)...\n'
VENDOR_MNT="$MNT_DIR/vendor"
mkdir -p "$VENDOR_MNT"
"${SUDO[@]}" rm -rf "$VENDOR_STAGE"
mkdir -p "$VENDOR_STAGE"

"${SUDO[@]}" mount -t erofs -o loop,ro "$IMAGES_DIR/vendor.img" "$VENDOR_MNT"
"${SUDO[@]}" cp -a "$VENDOR_MNT/." "$VENDOR_STAGE/"
"${SUDO[@]}" umount "$VENDOR_MNT"

printf 'Applying minimal ROM profile to vendor...\n'
"${SUDO[@]}" python3 "$SCRIPT_DIR/lunaris_rom_profile.py" apply \
  --partition vendor \
  --mount-dir "$VENDOR_STAGE" \
  --report "$WORK_DIR/report-vendor.json"

printf 'Rebuilding vendor.img with mkfs.erofs (lz4hc)...\n'
rm -f "$IMAGES_DIR/vendor.new.img"
"${SUDO[@]}" mkfs.erofs -zlz4hc --mount-point=/vendor "$IMAGES_DIR/vendor.new.img" "$VENDOR_STAGE" >/dev/null
"${SUDO[@]}" chown "$(id -u):$(id -g)" "$IMAGES_DIR/vendor.new.img"
vendor_raw_size="$(stat -c '%s' "$IMAGES_DIR/vendor.new.img")"
vendor_aligned_size=$(((vendor_raw_size + 4095) / 4096 * 4096))
truncate -s "$vendor_aligned_size" "$IMAGES_DIR/vendor.new.img"
mv -f "$IMAGES_DIR/vendor.new.img" "$IMAGES_DIR/vendor.img"
"${SUDO[@]}" rm -rf "$VENDOR_STAGE"
printf 'Finished vendor.img (%d bytes)\n' "$vendor_aligned_size"

# Repack OTA zip
OUTPUT_ZIP="$OUTPUT_DIR/$OUTPUT_ROM_FILENAME"
rm -f "$OUTPUT_ZIP" "$OUTPUT_DIR/SHA256SUMS"
printf 'Repacking signed OTA zip: %s (using %s parallel workers)...\n' "$OUTPUT_ZIP" "$JOBS"
python3 "$SCRIPT_DIR/lunaris_payload.py" repack \
  --source-zip "$SOURCE_ZIP" \
  --images-dir "$IMAGES_DIR" \
  --partitions "${MODIFIED_PARTITIONS[@]}" \
  --key-json "$KEY_JSON" \
  --output-zip "$OUTPUT_ZIP" \
  --report "$WORK_DIR/repack-report.json" \
  --jobs "$JOBS"

# Write consolidated debloat & provenance reports
python3 - "$WORK_DIR" "$OUTPUT_DIR" "$SOURCE_ZIP" "$OUTPUT_ZIP" "$BOOT_IMAGE" <<'PY'
import hashlib, json, pathlib, sys
work_dir, out_dir, src_zip, out_zip = map(pathlib.Path, sys.argv[1:5])
boot_img = sys.argv[5]

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()

part_reports = {}
total_removed = 0
total_bytes_freed = 0
for part in ("system", "system_ext", "product", "vendor"):
    rpath = work_dir / f"report-{part}.json"
    if rpath.is_file():
        data = json.loads(rpath.read_text(encoding="utf-8"))
        part_reports[part] = data
        total_removed += data.get("removed_count", 0)
        total_bytes_freed += data.get("bytes_freed", 0)

repack_report = json.loads((work_dir / "repack-report.json").read_text(encoding="utf-8"))
out_sha256 = sha256_file(out_zip)

summary = {
    "device": "Nothing Phone (2) / Pong",
    "base_rom": src_zip.name,
    "base_rom_sha256": sha256_file(src_zip),
    "output_rom": out_zip.name,
    "output_rom_sha256": out_sha256,
    "output_rom_size_bytes": out_zip.stat().st_size,
    "custom_boot_image": boot_img if boot_img else None,
    "profile": "minimal-pico-gapps-low-ram-v1",
    "total_entries_removed": total_removed,
    "total_uncompressed_bytes_freed": total_bytes_freed,
    "partition_debloat_reports": part_reports,
    "payload_repack": repack_report,
}
(out_dir / "debloat-report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

(out_dir / "FLASH-INSTRUCTIONS.txt").write_text(
    "Lunaris-AOSP 3.6 Minimal (Pico GApps / Ultra-Low Idle RAM) for Nothing Phone (2) [Pong]\n"
    "=======================================================================================\n\n"
    "1. Reboot to recovery (Lineage/Lunaris Recovery or TWRP/OrangeFox)\n"
    "   or use fastboot/sideload:\n"
    "     adb sideload Lunaris-AOSP-Pong-Minimal-3.6-Pico.zip\n"
    "   (Accept the custom signature prompt in recovery if shown.)\n\n"
    "2. Alternatively, extract payload.bin partitions and flash via fastbootd.\n\n"
    "3. Keep a backup of your data and original boot/ROM zip before flashing.\n",
    encoding="utf-8",
)
PY

python3 "$SCRIPT_DIR/write_artifact_checksums.py" --directory "$OUTPUT_DIR"
printf 'Minimal ROM build complete: %s\n' "$OUTPUT_ZIP"
