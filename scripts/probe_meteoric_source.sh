#!/usr/bin/env bash
# Diagnostic only: compile three ABI fingerprints, never an Image or boot image.
set -euo pipefail
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SOURCE= TOOLCHAIN= CONFIG= ABI_WHITELIST= ORIGINAL_IMAGE= ABI_PYTHON= OUTPUT_DIR=
CONFIG_MODE=saved
while [ "$#" -gt 0 ]; do
  [ "$#" -ge 2 ] || fail "missing value for $1"
  case "$1" in
    --source) SOURCE="$2" ;;
    --toolchain) TOOLCHAIN="$2" ;;
    --config) CONFIG="$2" ;;
    --abi-whitelist) ABI_WHITELIST="$2" ;;
    --original-image) ORIGINAL_IMAGE="$2" ;;
    --abi-python) ABI_PYTHON="$2" ;;
    --output-dir) OUTPUT_DIR="$2" ;;
    --config-mode) CONFIG_MODE="$2" ;;
    *) fail "unknown option: $1" ;;
  esac
  shift 2
done
case "$CONFIG_MODE" in saved|rom) ;; *) fail 'config mode must be saved or rom' ;; esac
for value in "$SOURCE" "$TOOLCHAIN" "$CONFIG" "$ABI_WHITELIST" "$ORIGINAL_IMAGE" "$ABI_PYTHON" "$OUTPUT_DIR"; do
  [ -n "$value" ] || fail 'all seven path arguments are required'
done
: "${SOURCE_REPO:?SOURCE_REPO is required}"
: "${SOURCE_COMMIT:?SOURCE_COMMIT is required}"
case "$SOURCE_REPO@$SOURCE_COMMIT" in
  Pong-Development/kernel_nothing_sm8475_old@7cccdf9b9c80932d8402e6fe1d51e56fff7fbdb2) ;;
  MiguVT/kernel_nothing_sm8475_cleaned@87a807cf8b730f6e5c25c7cfc1a6262379270d16) ;;
  *) fail 'unreviewed diagnostic source revision' ;;
esac
SOURCE="$(realpath "$SOURCE")"
TOOLCHAIN="$(realpath "$TOOLCHAIN")"
CONFIG="$(realpath "$CONFIG")"
ABI_WHITELIST="$(realpath "$ABI_WHITELIST")"
ORIGINAL_IMAGE="$(realpath "$ORIGINAL_IMAGE")"
# Preserve the venv interpreter symlink; resolving it would select system Python.
ABI_PYTHON="$(realpath -s "$ABI_PYTHON")"
OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$OUTPUT_DIR"
PHASE=input-validation
record_state() {
  python3 - "$OUTPUT_DIR/probe-state.json" "$PHASE" "$1" "${2:-0}" "$CONFIG_MODE" <<'PY'
import datetime, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "mode": "diagnostic_source_probe", "full_kernel_build": False,
    "phase": sys.argv[2], "status": sys.argv[3], "exit_code": int(sys.argv[4]),
    "config_mode": sys.argv[5],
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2) + "\n")
PY
}
trap 'exit_status=$?; if [ "$exit_status" -ne 0 ]; then record_state failed "$exit_status" || true; fi' EXIT
record_state running

[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$SOURCE_COMMIT" ] || fail 'unexpected source revision'
[ -z "$(git -C "$SOURCE" status --porcelain)" ] || fail 'source checkout is not clean'
[ "$(git -C "$SOURCE" remote get-url origin)" = "https://github.com/$SOURCE_REPO.git" ] || fail 'unexpected source origin'
printf '%s  %s\n' a8b21ecc239eb1cbb9a72ccca8c5fa98c9833c2b003472fc840e099ef26ab778 "$CONFIG" | sha256sum --check --strict
printf '%s  %s\n' 56cfce8294ee8bff7150f1bb666ce84986d8e51931df5889a29ba89cf3a0db0f "$TOOLCHAIN/BUILD_INFO" | sha256sum --check --strict
compiler_version="$("$TOOLCHAIN/bin/clang" --version)"
printf '%s\n' "$compiler_version" | grep -Fq 'Android (13290119, +pgo, +bolt, +lto, +mlgo, based on r547379) clang version 20.0.0' || fail 'unexpected compiler build'
printf '%s\n' "$compiler_version" | grep -Fq 'b718bcaf8c198c82f3021447d943401e3ab5bd54' || fail 'unexpected LLVM source revision'
[ ! -e "$SOURCE/drivers/kernelsu" ] || fail 'source already contains a root core'
if grep -Eq 'kernelsu|CONFIG_KSU' "$SOURCE/drivers/Makefile" "$SOURCE/drivers/Kconfig"; then
  fail 'source already integrates a root core'
fi
grep -Fq -- '-D__ANDROID_COMMON_KERNEL__' "$SOURCE/build.config.common" || fail 'candidate recipe lacks expected Android common define'

WHITELIST=/buildbot/src/android/common-android12-5.10-2023-09/out/android12-5.10/common/abi_symbollist.raw
python3 - "$ABI_WHITELIST" <<'PY'
import pathlib, re, sys
names = pathlib.Path(sys.argv[1]).read_text().splitlines()
if not names or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
    raise SystemExit("original export whitelist is empty or malformed")
if len(names) != len(set(names)):
    raise SystemExit("original export whitelist contains duplicates")
PY
sudo install -d -o "$(id -u)" -g "$(id -g)" "$(dirname "$WHITELIST")"
install -m 0644 "$ABI_WHITELIST" "$WHITELIST"
printf '%s\n' '-Meteoric' > "$SOURCE/.scmversion"
export PATH="$TOOLCHAIN/bin:$PATH"
export ARCH=arm64 LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu-
export KCFLAGS=-D__ANDROID_COMMON_KERNEL__
export KBUILD_BUILD_USER=build-user KBUILD_BUILD_HOST=build-host
export KBUILD_BUILD_TIMESTAMP='Mon Dec 8 09:19:46 UTC 2025'
export SOURCE_DATE_EPOCH=1765185586
export CONFIG_MODE

python3 - "$OUTPUT_DIR/source-provenance.json" "$SOURCE" "$CONFIG" "$ABI_WHITELIST" "$ORIGINAL_IMAGE" "$TOOLCHAIN" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
def sha(path): return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
record = {
    "mode": "diagnostic_source_probe", "full_kernel_build": False, "root_integration": None,
    "config_mode": os.environ["CONFIG_MODE"],
    "source_repository": os.environ["SOURCE_REPO"],
    "source_commit": subprocess.check_output(["git", "-C", sys.argv[2], "rev-parse", "HEAD"], text=True).strip(),
    "original_config_sha256": sha(sys.argv[3]), "original_whitelist_sha256": sha(sys.argv[4]),
    "original_image_sha256": sha(sys.argv[5]), "compiler_build_info_sha256": sha(pathlib.Path(sys.argv[6]) / "BUILD_INFO"),
    "compiler_source_commit": "2d4dcbff7cb42f360f368bacf51cfe711fd1cb0f",
    "compiler_version": subprocess.check_output([str(pathlib.Path(sys.argv[6]) / "bin/clang"), "--version"], text=True),
    "kcflags": os.environ["KCFLAGS"], "requested_targets": ["kernel/module.o", "fs/block_dev.o", "net/core/skbuff.o"],
}
if os.environ["CONFIG_MODE"] == "rom":
    record["configuration_target"] = "vendor/meteoric_defconfig"
    record["configuration_target_sha256"] = sha(pathlib.Path(sys.argv[2]) / "arch/arm64/configs/vendor/meteoric_defconfig")
    record["historical_device_recipe_commit"] = "7a4ab944183da07a5547f4f8a3041f7ee418a188"
pathlib.Path(sys.argv[1]).write_text(json.dumps(record, indent=2) + "\n")
PY

PHASE=configuration
record_state running
{
  if [ "$CONFIG_MODE" = saved ]; then
    install -m 0644 "$CONFIG" "$OUTPUT_DIR/.config"
  else
    # The December 2025 luna device recipe uses this sole defconfig. Current
    # device-tree fragments belong to a later kernel migration.
    make -C "$SOURCE" O="$OUTPUT_DIR" vendor/meteoric_defconfig
  fi
  make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
  "$SOURCE/scripts/config" --file "$OUTPUT_DIR/.config" -d LTO_CLANG_FULL -e LTO_CLANG_THIN \
    --set-str LOCALVERSION '' --set-str UNUSED_KSYMS_WHITELIST "$WHITELIST"
  make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
} 2>&1 | tee "$OUTPUT_DIR/configure.log"
diff -u "$CONFIG" "$OUTPUT_DIR/.config" > "$OUTPUT_DIR/config.diff" || [ "$?" = 1 ]
cp "$OUTPUT_DIR/.config" "$OUTPUT_DIR/used.config"

# This diagnostic records all drift; it cannot authorize a final rooted build.
python3 - "$CONFIG" "$OUTPUT_DIR/.config" "$OUTPUT_DIR/config-drift.json" <<'PY'
import hashlib, json, os, pathlib, re, sys
def parse(path):
    result = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1); result[key] = value
        else:
            match = re.fullmatch(r"# (CONFIG_\w+) is not set", line)
            if match: result[match[1]] = "n"
    return result
old, new = map(parse, sys.argv[1:3])
required = ["CONFIG_SMP", "CONFIG_PREEMPT", "CONFIG_MODVERSIONS", "CONFIG_LTO_CLANG_THIN"]
if os.environ["CONFIG_MODE"] == "saved":
    required.extend(["CONFIG_CFI_CLANG", "CONFIG_CFI_CLANG_SHADOW", "CONFIG_TRIM_UNUSED_KSYMS"])
observed_keys = required + ["CONFIG_CFI_CLANG", "CONFIG_CFI_CLANG_SHADOW", "CONFIG_TRIM_UNUSED_KSYMS",
                            "CONFIG_UNUSED_KSYMS_WHITELIST", "CONFIG_LOCALVERSION", "CONFIG_LOCALVERSION_AUTO",
                            "CONFIG_KASAN", "CONFIG_KASAN_HW_TAGS", "CONFIG_SHADOW_CALL_STACK", "CONFIG_KSU"]
failures = [key for key in required if new.get(key) != "y"]
if new.get("CONFIG_KSU") in ("y", "m"): failures.append("CONFIG_KSU must not be enabled")
report = {
    "mode": "diagnostic_config_capture", "authorizes_full_kernel_build": False,
    "config_mode": os.environ["CONFIG_MODE"],
    "original_sha256": hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(),
    "built_sha256": hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest(),
    "changes": {key: {"original": old.get(key), "built": new.get(key)} for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)},
    "observed_fundamentals": {key: new.get(key) for key in sorted(set(observed_keys))},
    "original_whitelist_active": new.get("CONFIG_TRIM_UNUSED_KSYMS") == "y",
    "required_failures": failures,
}
pathlib.Path(sys.argv[3]).write_text(json.dumps(report, indent=2) + "\n")
pathlib.Path(sys.argv[3]).with_name("fundamental-config.txt").write_text("\n".join(
    f"{key}={new.get(key, '<not present>')}" for key in sorted(set(observed_keys))) + "\n")
if failures: raise SystemExit("fundamental probe configuration failed: " + ", ".join(failures))
PY
make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease | tee "$OUTPUT_DIR/kernelrelease.txt"

PHASE=preparing-headers
record_state running
make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" LTO=thin prepare modules_prepare scripts \
  2>&1 | tee "$OUTPUT_DIR/prepare.log"

PHASE=compiling-three-fingerprints
record_state running
printf '%s\n' 'Compiling three ABI probe objects only; Image/vmlinux are not requested.'
make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" LTO=thin KBUILD_SYMTYPES=1 \
  kernel/module.o fs/block_dev.o net/core/skbuff.o 2>&1 | tee "$OUTPUT_DIR/probe.log"
for object in kernel/module.o fs/block_dev.o net/core/skbuff.o; do
  test -s "$OUTPUT_DIR/$object.symversions" || fail "CRC fragment missing: $object.symversions"
done
PHASE=partial-abi-comparison
record_state running
"$ABI_PYTHON" "$SCRIPT_DIR/probe_kernel_abi.py" --original-image "$ORIGINAL_IMAGE" \
  --symversions "$OUTPUT_DIR/kernel/module.o.symversions" \
  --symversions "$OUTPUT_DIR/fs/block_dev.o.symversions" \
  --symversions "$OUTPUT_DIR/net/core/skbuff.o.symversions" --report "$OUTPUT_DIR/probe-report.json"
record_state passed
printf '%s\n' 'Partial ABI fingerprints match. A full kernel build and full ABI verification are still required.'
