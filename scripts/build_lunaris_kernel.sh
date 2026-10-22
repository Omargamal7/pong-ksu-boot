#!/usr/bin/env bash
# Rebuild the pinned original Meteoric profile with exactly one Next core.
set -euo pipefail
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

SOURCE= ROOT_SOURCE= TOOLCHAIN= CONFIG= OUTPUT_DIR= ABI_WHITELIST=
while [ "$#" -gt 0 ]; do
  [ "$#" -ge 2 ] || fail "missing value for $1"
  case "$1" in
    --source) SOURCE="$2" ;;
    --root-source) ROOT_SOURCE="$2" ;;
    --toolchain) TOOLCHAIN="$2" ;;
    --config) CONFIG="$2" ;;
    --output-dir) OUTPUT_DIR="$2" ;;
    --abi-whitelist) ABI_WHITELIST="$2" ;;
    *) fail "unknown option: $1" ;;
  esac
  shift 2
done
for value in "$SOURCE" "$ROOT_SOURCE" "$TOOLCHAIN" "$CONFIG" "$OUTPUT_DIR" "$ABI_WHITELIST"; do
  [ -n "$value" ] || fail 'all six path arguments are required'
done
SOURCE="$(realpath "$SOURCE")"
ROOT_SOURCE="$(realpath "$ROOT_SOURCE")"
TOOLCHAIN="$(realpath "$TOOLCHAIN")"
CONFIG="$(realpath "$CONFIG")"
OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
ABI_WHITELIST="$(realpath "$ABI_WHITELIST")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SOURCE_COMMIT=7cccdf9b9c80932d8402e6fe1d51e56fff7fbdb2
SOURCE_REPO=https://github.com/Pong-Development/kernel_nothing_sm8475_old.git
NEXT_COMMIT=4c706b5938665866ebe16944a481a7b91e3396af
TOOLCHAIN_COMMIT=2d4dcbff7cb42f360f368bacf51cfe711fd1cb0f
BUILD_INFO_SHA256=56cfce8294ee8bff7150f1bb666ce84986d8e51931df5889a29ba89cf3a0db0f
CONFIG_SHA256=a8b21ecc239eb1cbb9a72ccca8c5fa98c9833c2b003472fc840e099ef26ab778
DEFCONFIG="$SOURCE/arch/arm64/configs/vendor/meteoric_defconfig"
DEFCONFIG_SHA256=711fdbc9fd53c3006bb366104f3db97e85b61486556a99495947b512e97e7e3f
EXPECTED_RELEASE=5.10.237-Meteoric

mkdir -p "$OUTPUT_DIR"
rm -f "$OUTPUT_DIR/cache-trusted.json"
BUILD_STAGE=input-validation
write_build_state() {
  python3 - "$OUTPUT_DIR/build-state.json" "$BUILD_STAGE" "$1" "${2:-0}" <<'PY'
import datetime, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "profile": "lunaris-pong-meteoric-7ccc-clang20", "stage": sys.argv[2],
    "status": sys.argv[3], "exit_code": int(sys.argv[4]),
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2) + "\n")
PY
}
trap 'exit_status=$?; if [ "$exit_status" -ne 0 ]; then write_build_state failed "$exit_status" || true; fi' EXIT
write_build_state running
[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$SOURCE_COMMIT" ] || fail 'unexpected Meteoric source revision'
[ "$(git -C "$SOURCE" remote get-url origin)" = "$SOURCE_REPO" ] || fail 'unexpected Meteoric source origin'
[ -z "$(git -C "$SOURCE" status --porcelain)" ] || fail 'Meteoric source checkout is not clean'
[ "$(git -C "$ROOT_SOURCE" rev-parse HEAD)" = "$NEXT_COMMIT" ] || fail 'unexpected KernelSU Next revision'
[ -z "$(git -C "$ROOT_SOURCE" status --porcelain)" ] || fail 'KernelSU Next checkout is not clean'
[ "$(git -C "$ROOT_SOURCE" rev-parse v3.4.2^{commit})" = "$NEXT_COMMIT" ] || fail 'Next tag does not match pinned revision'
[ "$(git -C "$ROOT_SOURCE" rev-list --count HEAD)" = 3355 ] || fail 'Next history count would produce the wrong driver version'
[ ! -f "$ROOT_SOURCE/.git/shallow" ] || fail 'Next history is shallow'
printf '%s  %s\n' "$CONFIG_SHA256" "$CONFIG" | sha256sum --check --strict
printf '%s  %s\n' "$DEFCONFIG_SHA256" "$DEFCONFIG" | sha256sum --check --strict
printf '%s  %s\n' "$BUILD_INFO_SHA256" "$TOOLCHAIN/BUILD_INFO" | sha256sum --check --strict
[ -x "$TOOLCHAIN/bin/clang" ] && [ -x "$TOOLCHAIN/bin/ld.lld" ] || fail 'compiler tools are missing'
compiler_version="$("$TOOLCHAIN/bin/clang" --version)"
printf '%s\n' "$compiler_version" > "$OUTPUT_DIR/compiler-version.txt"
printf '%s\n' "$compiler_version" | grep -Fq 'Android (13290119, +pgo, +bolt, +lto, +mlgo, based on r547379) clang version 20.0.0' || fail 'unexpected original compiler build'
printf '%s\n' "$compiler_version" | grep -Fq 'b718bcaf8c198c82f3021447d943401e3ab5bd54' || fail 'unexpected original LLVM revision'
available_kb="$(df -Pk "$OUTPUT_DIR" | awk 'NR==2 {print $4}')"
[ "$available_kb" -ge $((35 * 1024 * 1024)) ] || fail 'less than 35 GiB free before compilation'

# The source tree used for the baseline must not already contain a root driver.
[ ! -e "$SOURCE/drivers/kernelsu" ] && [ ! -L "$SOURCE/drivers/kernelsu" ] || fail 'a root core is already present'
if grep -Eq 'kernelsu|CONFIG_KSU' "$SOURCE/drivers/Makefile" "$SOURCE/drivers/Kconfig"; then
  fail 'an existing KernelSU integration was found'
fi
python3 - "$ABI_WHITELIST" <<'PY'
import pathlib, re, sys
names = pathlib.Path(sys.argv[1]).read_text().splitlines()
if len(names) != 14523 or len(names) != len(set(names)) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
    raise SystemExit("expected all 14,523 unique original export names")
if not {"module_layout", "I_BDEV", "__alloc_skb"}.issubset(names):
    raise SystemExit("original export evidence lacks required anchors")
PY
cp "$ABI_WHITELIST" "$OUTPUT_DIR/abi_symbollist.raw"
sha256sum "$OUTPUT_DIR/abi_symbollist.raw" > "$OUTPUT_DIR/abi_symbollist.raw.sha256"
# Kept for provenance only: this exact uploaded IKCONFIG is unrelated stock_defconfig.
install -m 0644 "$CONFIG" "$OUTPUT_DIR/embedded-stock.config"

export PATH="/usr/lib/ccache:$TOOLCHAIN/bin:$PATH"
export ARCH=arm64 LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu-
# Lunaris's make-kernel-config/build recipe does not source build.config.common.
# Do not inject its unrelated __ANDROID_COMMON_KERNEL__ define.
export KCFLAGS=''
export KBUILD_BUILD_USER=build-user KBUILD_BUILD_HOST=build-host KBUILD_BUILD_VERSION=1
export KBUILD_BUILD_TIMESTAMP='Mon Dec 8 09:19:46 UTC 2025'
export SOURCE_DATE_EPOCH=1765185586
export CCACHE_DIR="${CCACHE_DIR:-$PWD/work/ccache}"
export CCACHE_COMPILERCHECK=content CCACHE_COMPRESS=1
ccache --max-size 1G >/dev/null
# Actual defconfig already supplies -Meteoric with LOCALVERSION_AUTO disabled.
# An empty .scmversion suppresses git/dirty suffixes without adding it twice.
: > "$SOURCE/.scmversion"

BUILD_STAGE=effective-baseline
write_build_state running
{
  # Match Lunaris build/tasks/kernel.mk: copy the sole defconfig, then olddefconfig.
  install -m 0644 "$DEFCONFIG" "$OUTPUT_DIR/.config"
  make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
} 2>&1 | tee "$OUTPUT_DIR/baseline-configure.log"
cp "$OUTPUT_DIR/.config" "$OUTPUT_DIR/baseline.config"
make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease > "$OUTPUT_DIR/baseline-kernelrelease.txt"
[ "$(cat "$OUTPUT_DIR/baseline-kernelrelease.txt")" = "$EXPECTED_RELEASE" ] || fail 'effective baseline has an unexpected kernel release'
python3 - "$OUTPUT_DIR" "$SOURCE_COMMIT" "$NEXT_COMMIT" "$TOOLCHAIN_COMMIT" "$DEFCONFIG" <<'PY'
import hashlib, json, pathlib, sys
out = pathlib.Path(sys.argv[1])
def sha(path): return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
record = {
    "profile": "lunaris-pong-meteoric-7ccc-clang20",
    "source_repository": "Pong-Development/kernel_nothing_sm8475_old", "source_commit": sys.argv[2],
    "root_commit": sys.argv[3], "toolchain_source_commit": sys.argv[4],
    "defconfig_target": "vendor/meteoric_defconfig", "defconfig_sha256": sha(sys.argv[5]),
    "historical_device_recipe_commit": "7a4ab944183da07a5547f4f8a3041f7ee418a188",
    "baseline_config_sha256": sha(out / "baseline.config"),
    "embedded_stock_config_sha256": sha(out / "embedded-stock.config"),
    "embedded_config_used_as_baseline": False,
    "embedded_config_note": "Original source deliberately embeds unrelated stock_defconfig; retained only as provenance.",
    "compiler": (out / "compiler-version.txt").read_text(),
    "compiler_build_info_sha256": "56cfce8294ee8bff7150f1bb666ce84986d8e51931df5889a29ba89cf3a0db0f",
    "kcflags": "", "lto": "none", "scmversion": "",
    "original_export_whitelist_sha256": sha(out / "abi_symbollist.raw"),
    "original_export_whitelist_used_to_change_config": False,
    "full_abi_verified": False,
}
(out / "baseline-provenance.json").write_text(json.dumps(record, indent=2) + "\n")
PY

# Integrate Next only after capturing the effective original baseline.
BUILD_STAGE=root-integration
write_build_state running
ln -s "$ROOT_SOURCE/kernel" "$SOURCE/drivers/kernelsu"
printf '\nobj-$(CONFIG_KSU) += kernelsu/\n' >> "$SOURCE/drivers/Makefile"
python3 - "$SOURCE/drivers/Kconfig" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
before, token, after = text.rpartition("endmenu")
if not token:
    raise SystemExit("drivers/Kconfig has no enclosing menu")
path.write_text(before + 'source "drivers/kernelsu/Kconfig"\n\n' + token + after)
PY
"$SOURCE/scripts/config" --file "$OUTPUT_DIR/.config" -e KSU -d KSU_DEBUG \
  -d KSU_DISABLE_MANAGER -d KSU_DISABLE_POLICY
make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
diff -u "$OUTPUT_DIR/baseline.config" "$OUTPUT_DIR/.config" > "$OUTPUT_DIR/config.diff" || [ "$?" = 1 ]
BUILD_STAGE=config-validation
write_build_state running
python3 "$SCRIPT_DIR/verify_lunaris_config.py" --original "$OUTPUT_DIR/baseline.config" \
  --built "$OUTPUT_DIR/.config" --report "$OUTPUT_DIR/config-drift.json"
release="$(make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease)"
[ "$release" = "$EXPECTED_RELEASE" ] || fail "unexpected pre-build release: $release"
python3 - "$OUTPUT_DIR/cache-trusted.json" "$OUTPUT_DIR/.config" "$OUTPUT_DIR/baseline.config" "$SOURCE_COMMIT" "$NEXT_COMMIT" <<'PY'
import hashlib, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "config_sha256": hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest(),
    "baseline_config_sha256": hashlib.sha256(pathlib.Path(sys.argv[3]).read_bytes()).hexdigest(),
    "kernel_commit": sys.argv[4], "next_commit": sys.argv[5],
    "compiler": "Android clang 20.0.0 / r547379 / 13290119", "lto": "none",
}, indent=2) + "\n")
PY

# Compile Next first. Generated SELinux headers are needed by its source files.
BUILD_STAGE=next-preflight
write_build_state running
preflight_config_sha256="$(sha256sum "$OUTPUT_DIR/.config" | cut -d ' ' -f 1)"
{
  printf '%s\n' 'Starting KernelSU Next object preflight; full kernel compilation is deferred.'
  make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" prepare modules_prepare scripts
  mkdir -p "$OUTPUT_DIR/security/selinux"
  "$OUTPUT_DIR/scripts/selinux/genheaders/genheaders" \
    "$OUTPUT_DIR/security/selinux/flask.h" "$OUTPUT_DIR/security/selinux/av_permissions.h"
  make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" drivers/kernelsu/
  test -s "$OUTPUT_DIR/drivers/kernelsu/core/init.o"
  test -s "$OUTPUT_DIR/drivers/kernelsu/hook/arm64/syscall_hook.o"
  test -s "$OUTPUT_DIR/drivers/kernelsu/built-in.a"
} 2>&1 | tee "$OUTPUT_DIR/preflight.log"
[ "$(sha256sum "$OUTPUT_DIR/.config" | cut -d ' ' -f 1)" = "$preflight_config_sha256" ] || fail 'configuration changed during root-driver preflight'
grep -Fq -- '-- KernelSU-Next version: 33355' "$OUTPUT_DIR/preflight.log" || fail 'preflight Next driver version is wrong'
grep -Fq -- '-- KernelSU-Next tag: v3.4.2' "$OUTPUT_DIR/preflight.log" || fail 'preflight Next version tag is wrong'
printf '%s\n' 'KernelSU Next object preflight passed; full kernel build has not run yet.' \
  | tee "$OUTPUT_DIR/preflight-passed.txt" | tee -a "$OUTPUT_DIR/preflight.log"

# Never reuse symvers copied from an earlier build when validating this Image.
BUILD_STAGE=kernel-build
write_build_state running
rm -f "$OUTPUT_DIR/Module.symvers" "$OUTPUT_DIR/vmlinux.symvers"
make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" Image 2>&1 | tee "$OUTPUT_DIR/build.log"
BUILD_STAGE=post-build-validation
write_build_state running
[ "$(sha256sum "$OUTPUT_DIR/.config" | cut -d ' ' -f 1)" = "$preflight_config_sha256" ] || fail 'configuration changed during full compilation'
release="$(make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease)"
[ "$release" = "$EXPECTED_RELEASE" ] || fail "unexpected built release: $release"
grep -Eq '[[:space:]]ksu_' "$OUTPUT_DIR/System.map" || fail 'built-in KernelSU Next symbols are absent'
grep -Fq -- '-- KernelSU-Next version: 33355' "$OUTPUT_DIR/build.log" || fail 'wrong or missing Next driver version'
grep -Fq -- '-- KernelSU-Next tag: v3.4.2' "$OUTPUT_DIR/build.log" || fail 'wrong or missing Next version tag'
cp "$OUTPUT_DIR/arch/arm64/boot/Image" "$OUTPUT_DIR/Image"
cp "$OUTPUT_DIR/.config" "$OUTPUT_DIR/used.config"
test -s "$OUTPUT_DIR/vmlinux.symvers" || fail 'current vmlinux.symvers is missing'
cp "$OUTPUT_DIR/vmlinux.symvers" "$OUTPUT_DIR/Module.symvers"
printf '%s\n' "$release" > "$OUTPUT_DIR/kernelrelease.txt"
sha256sum "$OUTPUT_DIR/Image" "$OUTPUT_DIR/used.config"
ccache --show-stats || true
BUILD_STAGE=kernel-build
write_build_state passed
