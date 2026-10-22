#!/usr/bin/env bash
# Build a single pinned Meteoric/KernelSU Next candidate on a cloud runner.
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

COMMON_COMMIT=744d5c916fbbd6fb15f92c69c32de440d7345c6f
NEXT_COMMIT=4c706b5938665866ebe16944a481a7b91e3396af
CONFIG_SHA256=a8b21ecc239eb1cbb9a72ccca8c5fa98c9833c2b003472fc840e099ef26ab778
EXPECTED_RELEASE=5.10.237-Meteoric
WHITELIST=/buildbot/src/android/common-android12-5.10-2023-09/out/android12-5.10/common/abi_symbollist.raw

[ "$(git -C "$SOURCE" rev-parse HEAD)" = "$COMMON_COMMIT" ] || fail 'unexpected ACK revision'
[ -z "$(git -C "$SOURCE" status --porcelain)" ] || fail 'ACK source checkout is not clean'
[ "$(git -C "$ROOT_SOURCE" rev-parse HEAD)" = "$NEXT_COMMIT" ] || fail 'unexpected KernelSU Next revision'
[ -z "$(git -C "$ROOT_SOURCE" status --porcelain)" ] || fail 'KernelSU Next checkout is not clean'
[ "$(git -C "$ROOT_SOURCE" rev-parse v3.4.2^{commit})" = "$NEXT_COMMIT" ] || fail 'Next tag does not match pinned revision'
[ "$(git -C "$ROOT_SOURCE" rev-list --count HEAD)" = 3355 ] || fail 'Next history count would produce the wrong driver version'
[ ! -f "$ROOT_SOURCE/.git/shallow" ] || fail 'Next history is shallow'
printf '%s  %s\n' "$CONFIG_SHA256" "$CONFIG" | sha256sum --check --strict
[ -x "$TOOLCHAIN/bin/clang" ] && [ -x "$TOOLCHAIN/bin/ld.lld" ] || fail 'compiler tools are missing'
"$TOOLCHAIN/bin/clang" --version | head -1
"$TOOLCHAIN/bin/clang" --version | head -1 | grep -Fq 'clang version 12.0.5' || fail 'unexpected compiler version'

mkdir -p "$OUTPUT_DIR"
rm -f "$OUTPUT_DIR/cache-trusted.json"
BUILD_STAGE=configuring
write_build_state() {
  python3 - "$OUTPUT_DIR/build-state.json" "$BUILD_STAGE" "$1" "${2:-0}" <<'PY'
import datetime, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "stage": sys.argv[2], "status": sys.argv[3], "exit_code": int(sys.argv[4]),
    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, indent=2) + "\n")
PY
}
trap 'exit_status=$?; if [ "$exit_status" -ne 0 ]; then write_build_state failed "$exit_status" || true; fi' EXIT
write_build_state running
available_kb="$(df -Pk "$OUTPUT_DIR" | awk 'NR==2 {print $4}')"
[ "$available_kb" -ge $((35 * 1024 * 1024)) ] || fail 'less than 35 GiB free before compilation'

# Start from clean ACK, which has no old KernelSU core to retain.
[ ! -e "$SOURCE/drivers/kernelsu" ] || fail 'a root core is already present'
if grep -Eq 'kernelsu|CONFIG_KSU' "$SOURCE/drivers/Makefile" "$SOURCE/drivers/Kconfig"; then
  fail 'an existing KernelSU integration was found'
fi
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
printf '%s\n' '-Meteoric' > "$SOURCE/.scmversion"

# Reproduce the saved config's path using the actual installed kernel's exports.
# An ACK branch's aggregate whitelist is not proof of this ROM's export set.
python3 - "$ABI_WHITELIST" <<'PY'
import pathlib, re, sys
names = pathlib.Path(sys.argv[1]).read_text().splitlines()
if not names or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in names):
    raise SystemExit("original-kernel export whitelist is empty or malformed")
if len(names) != len(set(names)):
    raise SystemExit("original-kernel export whitelist contains duplicate names")
PY
sudo install -d -o "$(id -u)" -g "$(id -g)" "$(dirname "$WHITELIST")"
install -m 0644 "$ABI_WHITELIST" "$WHITELIST"
cp "$WHITELIST" "$OUTPUT_DIR/abi_symbollist.raw"
sha256sum "$OUTPUT_DIR/abi_symbollist.raw" > "$OUTPUT_DIR/abi_symbollist.raw.sha256"

export PATH="/usr/lib/ccache:$TOOLCHAIN/bin:$PATH"
export ARCH=arm64 LLVM=1 LLVM_IAS=1 CROSS_COMPILE=aarch64-linux-gnu-
export KBUILD_BUILD_USER=build-user KBUILD_BUILD_HOST=build-host
export KBUILD_BUILD_TIMESTAMP='Mon Dec 8 09:19:46 UTC 2025'
export SOURCE_DATE_EPOCH=1765185586
export CCACHE_DIR="${CCACHE_DIR:-$PWD/work/ccache}"
export CCACHE_COMPILERCHECK=content CCACHE_COMPRESS=1
ccache --max-size 1G >/dev/null

install -m 0644 "$CONFIG" "$OUTPUT_DIR/.config"
make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
"$SOURCE/scripts/config" --file "$OUTPUT_DIR/.config" \
  -d LTO_CLANG_FULL -e LTO_CLANG_THIN -e KSU -d KSU_DEBUG \
  -d KSU_DISABLE_MANAGER -d KSU_DISABLE_POLICY
make -C "$SOURCE" O="$OUTPUT_DIR" olddefconfig
diff -u "$CONFIG" "$OUTPUT_DIR/.config" > "$OUTPUT_DIR/config.diff" || [ "$?" = 1 ]

# Admit only individually reviewed olddefconfig transitions; never ignore drift.
BUILD_STAGE=config-validation
write_build_state running
python3 "$SCRIPT_DIR/verify_lunaris_config.py" --original "$CONFIG" \
  --built "$OUTPUT_DIR/.config" --report "$OUTPUT_DIR/config-drift.json"
for expected in CONFIG_KSU=y CONFIG_LTO_CLANG_THIN=y CONFIG_CFI_CLANG=y CONFIG_CFI_CLANG_SHADOW=y \
  CONFIG_SMP=y CONFIG_PREEMPT=y CONFIG_MODULE_UNLOAD=y CONFIG_MODVERSIONS=y CONFIG_TRIM_UNUSED_KSYMS=y; do
  grep -Fqx "$expected" "$OUTPUT_DIR/.config" || fail "required config missing: $expected"
done
if grep -Eq '^CONFIG_(KSU_SUSFS|SUSFS)[^=]*=y$' "$OUTPUT_DIR/.config"; then
  fail 'SUSFS was unexpectedly enabled'
fi
release="$(make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease)"
[ "$release" = "$EXPECTED_RELEASE" ] || fail "unexpected pre-build release: $release"

# Only this marker permits a failed run's content-addressed compiler cache to be
# saved. It is created after pinned inputs and the final config have passed.
python3 - "$OUTPUT_DIR/cache-trusted.json" "$OUTPUT_DIR/.config" "$COMMON_COMMIT" "$NEXT_COMMIT" <<'PY'
import hashlib, json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "config_sha256": hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest(),
    "kernel_commit": sys.argv[3], "next_commit": sys.argv[4],
    "compiler": "Android clang 12.0.5 / r416183b",
}, indent=2) + "\n")
PY

# Compile the new root core before spending time on the rest of the kernel.
# Pinned ACK supports directory targets (Makefile's single-targets %/ rule).
# SELinux's generated headers are not emitted by modules_prepare; use the exact
# genheaders host-tool invocation from security/selinux/Makefile first.
BUILD_STAGE=next-preflight
write_build_state running
preflight_config_sha256="$(sha256sum "$OUTPUT_DIR/.config" | cut -d ' ' -f 1)"
{
  printf '%s\n' 'Starting KernelSU Next object preflight; full kernel compilation is deferred.'
  make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" LTO=thin prepare modules_prepare scripts
  mkdir -p "$OUTPUT_DIR/security/selinux"
  "$OUTPUT_DIR/scripts/selinux/genheaders/genheaders" \
    "$OUTPUT_DIR/security/selinux/flask.h" "$OUTPUT_DIR/security/selinux/av_permissions.h"
  make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" LTO=thin drivers/kernelsu/
  test -s "$OUTPUT_DIR/drivers/kernelsu/core/init.o"
  test -s "$OUTPUT_DIR/drivers/kernelsu/hook/arm64/syscall_hook.o"
  test -s "$OUTPUT_DIR/drivers/kernelsu/built-in.a"
} 2>&1 | tee "$OUTPUT_DIR/preflight.log"
[ "$(sha256sum "$OUTPUT_DIR/.config" | cut -d ' ' -f 1)" = "$preflight_config_sha256" ] \
  || fail 'configuration changed during root-driver preflight'
grep -Fq -- '-- KernelSU-Next version: 33355' "$OUTPUT_DIR/preflight.log" || fail 'preflight Next driver version is wrong'
grep -Fq -- '-- KernelSU-Next tag: v3.4.2' "$OUTPUT_DIR/preflight.log" || fail 'preflight Next version tag is wrong'
printf '%s\n' 'KernelSU Next object preflight passed; full kernel build has not run yet.' \
  | tee "$OUTPUT_DIR/preflight-passed.txt" | tee -a "$OUTPUT_DIR/preflight.log"

# Reuse the successful preflight's .config, generated headers and object files.
BUILD_STAGE=kernel-build
write_build_state running
make -C "$SOURCE" O="$OUTPUT_DIR" -j"${JOBS:-$(nproc)}" LTO=thin Image 2>&1 | tee "$OUTPUT_DIR/build.log"
BUILD_STAGE=post-build-validation
write_build_state running
release="$(make -s -C "$SOURCE" O="$OUTPUT_DIR" kernelrelease)"
[ "$release" = "$EXPECTED_RELEASE" ] || fail "unexpected built release: $release"
grep -Eq '[[:space:]]ksu_' "$OUTPUT_DIR/System.map" || fail 'built-in KernelSU Next symbols are absent'
grep -Fq -- '-- KernelSU-Next version: 33355' "$OUTPUT_DIR/build.log" || fail 'wrong or missing Next driver version'
grep -Fq -- '-- KernelSU-Next tag: v3.4.2' "$OUTPUT_DIR/build.log" || fail 'wrong or missing Next version tag'
cp "$OUTPUT_DIR/arch/arm64/boot/Image" "$OUTPUT_DIR/Image"
cp "$OUTPUT_DIR/.config" "$OUTPUT_DIR/used.config"
# Image-only builds refresh vmlinux.symvers. Never reuse the copied symvers
# from a previous persistent build when checking the current Image's ABI.
test -s "$OUTPUT_DIR/vmlinux.symvers" || fail 'current vmlinux.symvers is missing'
cp "$OUTPUT_DIR/vmlinux.symvers" "$OUTPUT_DIR/Module.symvers"
printf '%s\n' "$release" > "$OUTPUT_DIR/kernelrelease.txt"
sha256sum "$OUTPUT_DIR/Image" "$OUTPUT_DIR/used.config"
ccache --show-stats || true
BUILD_STAGE=kernel-build
write_build_state passed
