#!/usr/bin/env bash
# Persistent cloud/Codespaces counterpart to the GitHub Actions build.
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"
mkdir -p work/cloud
BACKGROUND=false
if [ "${1:-}" = --background ]; then
  BACKGROUND=true
  shift
fi
INHERITED_LOCK=false
if [ "${1:-}" = --lock-held ]; then INHERITED_LOCK=true; shift; fi
PUBLISH=true
if [ "${1:-}" = --no-publish ]; then PUBLISH=false; shift; fi
[ "$#" = 0 ] || fail 'usage: build_lunaris_cloud.sh [--background] [--no-publish]'
command -v flock >/dev/null || fail 'flock is required'
if [ "$INHERITED_LOCK" = true ]; then
  [ "$(readlink "/proc/$$/fd/9")" = "$(realpath work/cloud/build.lock)" ] || \
    fail 'the inherited build lock is missing'
else
  exec 9>work/cloud/build.lock
fi
flock -n 9 || fail 'another cloud build already holds the build lock'
if [ "$BACKGROUND" = true ]; then
  arguments=(--lock-held)
  if [ "$PUBLISH" = false ]; then arguments+=(--no-publish); fi
  # Descriptor 9 remains locked across nohup/exec. Obtain it before touching
  # the active build's log or PID, including on a second post-create launch.
  nohup bash "$SCRIPT_DIR/build_lunaris_cloud.sh" "${arguments[@]}" \
    > work/cloud/session.log 2>&1 < /dev/null &
  printf '%s\n' "$!" > work/cloud/build.pid
  printf 'Cloud build started. Follow: tail -f work/cloud/session.log\n'
  exit 0
fi
[ "$(uname -m)" = x86_64 ] || fail 'this cloud build requires Linux x86_64'

REPOSITORY=Omargamal7/pong-ksu-boot
SOURCE_REPO=Pong-Development/kernel_nothing_sm8475_old
COMMON_COMMIT=7cccdf9b9c80932d8402e6fe1d51e56fff7fbdb2
NEXT_COMMIT=4c706b5938665866ebe16944a481a7b91e3396af
TOOLCHAIN_COMMIT=2d4dcbff7cb42f360f368bacf51cfe711fd1cb0f
BUILD_INFO_SHA256=56cfce8294ee8bff7150f1bb666ce84986d8e51931df5889a29ba89cf3a0db0f
BUILD_PROFILE=meteoric-r547379-lto-none-v1
BOOT_TOOLS_COMMIT=d2bb0af5ba6d3198a3e99529c97eda1be0b5a093
ABI_EXTRACTOR_COMMIT=2d094bc9c220b6b50f8215cbc3c9e581210ce1a6
BOOT_SHA256=f86ca8c2a1f731a921813cecf0de1d693ed795f684cfb10cf1140bee4781cc7d
MANAGER_SHA256=774a5511f7034a4930e881114e202319cbdc34c5aaaa5c7c6b6d2902364e4c58
export SOURCE_REPO COMMON_COMMIT NEXT_COMMIT TOOLCHAIN_COMMIT BOOT_TOOLS_COMMIT ABI_EXTRACTOR_COMMIT BUILD_PROFILE BUILD_INFO_SHA256
# Keep incompatible earlier ACK/Clang12 sources, compiler and objects separate.
# The top-level work/cloud lock, PID and session log remain stable for callers.
WORK="$REPO_DIR/work/cloud/$BUILD_PROFILE"
mkdir -p "$WORK"
DIST="$WORK/dist"

check_disk() {
  local available
  available="$(df -Pk "$WORK" | awk 'NR==2 {print $4}')"
  df -h "$WORK"
  [ "$available" -ge $(($1 * 1024 * 1024)) ] || \
    fail "at least $1 GiB free disk is required; use a Codespace with more storage"
}
if [ -d "$WORK/source/.git" ]; then check_disk 35; else check_disk 45; fi
SUDO=()
if [ "$(id -u)" != 0 ]; then SUDO=(sudo); fi
bash scripts/configure_cloud_apt.sh
timeout 10m "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get \
  -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
timeout 10m "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get \
  -o DPkg::Lock::Timeout=120 -o Acquire::Retries=1 \
  -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
  bc binutils-aarch64-linux-gnu bison build-essential ccache cpio curl \
  dwarves flex gcc-aarch64-linux-gnu gh git libelf-dev libssl-dev lz4 \
  python3 python3-pip python3-venv rsync xz-utils

gh auth status
if [ ! -x "$WORK/abi-venv/bin/python" ]; then python3 -m venv "$WORK/abi-venv"; fi
CC=gcc "$WORK/abi-venv/bin/pip" install --no-cache-dir \
  'pyelftools==0.32' \
  "git+https://github.com/marin-m/vmlinux-to-elf.git@$ABI_EXTRACTOR_COMMIT"
"$WORK/abi-venv/bin/python" -m unittest discover -s tests -v

BOOT="$WORK/boot-current.img"
if [ ! -f "$BOOT" ]; then
  rm -f "$BOOT.partial"
  gh release download lunaris-boot-input --repo "$REPOSITORY" \
    --pattern boot-current.img --output "$BOOT.partial"
  mv "$BOOT.partial" "$BOOT"
fi
printf '%s  %s\n' "$BOOT_SHA256" "$BOOT" | sha256sum --check --strict
python3 scripts/prepare_lunaris_boot.py --boot "$BOOT" --output-dir "$WORK/original"
"$WORK/abi-venv/bin/python" scripts/verify_kernel_abi.py \
  --original-image "$WORK/original/kernel" --report "$WORK/original/abi-baseline.json" \
  --export-whitelist "$WORK/original/abi_symbollist.raw"

fetch_pinned() {
  local directory="$1" url="$2" revision="$3"
  if [ ! -d "$directory/.git" ]; then
    mkdir -p "$directory"
    git -C "$directory" init -q
    git -C "$directory" remote add origin "$url"
  fi
  [ "$(git -C "$directory" remote get-url origin)" = "$url" ] || fail "unexpected remote: $directory"
  if ! git -C "$directory" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$directory" fetch --depth=1 --no-tags origin "$revision"
  fi
  if ! git -C "$directory" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$directory" checkout -q --detach "$revision"
  fi
  [ "$(git -C "$directory" rev-parse HEAD)" = "$revision" ] || fail "unexpected revision: $directory"
}
fetch_pinned "$WORK/source" "https://github.com/$SOURCE_REPO.git" "$COMMON_COMMIT"
fetch_pinned "$WORK/mkbootimg" https://android.googlesource.com/platform/system/tools/mkbootimg "$BOOT_TOOLS_COMMIT"
if [ ! -d "$WORK/KernelSU-Next/.git" ]; then
  git clone --filter=blob:none --single-branch --branch v3.4.2 \
    https://github.com/KernelSU-Next/KernelSU-Next.git "$WORK/KernelSU-Next"
fi
[ "$(git -C "$WORK/KernelSU-Next" rev-parse HEAD)" = "$NEXT_COMMIT" ] || fail 'unexpected Next revision'

# Resume only our own source integration; preserve all compiled objects in build/.
# Refuse unrelated edits rather than resetting a user's changes.
python3 - "$WORK/source" "$WORK/KernelSU-Next/kernel" <<'PY'
import pathlib, subprocess, sys
source, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
changes = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '-z']).split(b'\0')
allowed = {'drivers/Makefile', 'drivers/Kconfig', 'drivers/kernelsu', '.scmversion'}
for entry in filter(None, changes):
    name = entry[3:].decode()
    if name not in allowed:
        raise SystemExit('refusing to reset unrelated source edit: ' + name)
link = source / 'drivers/kernelsu'
if link.is_symlink():
    if link.resolve() != root.resolve():
        raise SystemExit('refusing to remove an unexpected KernelSU source link')
    link.unlink()
elif link.exists():
    raise SystemExit('expected only the wrapper-created KernelSU symlink')
subprocess.run(['git', '-C', str(source), 'restore', '--source=HEAD', '--', 'drivers/Makefile', 'drivers/Kconfig'], check=True)
scmversion = source / '.scmversion'
if scmversion.exists():
    if scmversion.read_text() not in ('', '-Meteoric\n'):
        raise SystemExit('refusing to remove an unexpected .scmversion')
    scmversion.unlink()
PY

verify_compiler() {
  local directory="$1" version linker_version tool
  [ -f "$directory/BUILD_INFO" ] || return 1
  printf '%s  %s\n' "$BUILD_INFO_SHA256" "$directory/BUILD_INFO" | sha256sum --check --status || return 1
  for tool in clang ld.lld llvm-nm llvm-ar llvm-objcopy llvm-objdump llvm-readelf llvm-strip; do
    [ -x "$directory/bin/$tool" ] || return 1
  done
  version="$("$directory/bin/clang" --version)" || return 1
  [[ "$version" == *'Android (13290119, +pgo, +bolt, +lto, +mlgo, based on r547379) clang version 20.0.0'* ]] || return 1
  [[ "$version" == *'b718bcaf8c198c82f3021447d943401e3ab5bd54'* ]] || return 1
  linker_version="$("$directory/bin/ld.lld" --version)" || return 1
  [[ "$linker_version" == *'20.0.0'* ]] || return 1
  "$directory/bin/llvm-nm" --version >/dev/null || return 1
}
prune_compiler_variants() {
  local directory="$1" tool
  {
    printf '%s\n' 'Before removing unused top-level compiler variants:'
    du -sh "$directory"
    df -h "$WORK"
    # Official remote_toolchain_inputs uses the retained bin/lib/include/share
    # closure. These four unrelated distribution variants are profile-owned.
    rm -rf "$directory/musl" "$directory/runtimes_ndk_cxx" \
      "$directory/android_libc++" "$directory/python3"
    for tool in clang ld.lld llvm-nm llvm-ar llvm-objcopy llvm-objdump llvm-readelf llvm-strip; do
      printf 'Checking %s after variant removal:\n' "$tool"
      "$directory/bin/$tool" --version
    done
    printf '%s\n' 'After removing unused top-level compiler variants:'
    du -sh "$directory"
    df -h "$WORK"
  } 2>&1 | tee -a "$WORK/toolchain-storage.log"
}
if ! verify_compiler "$WORK/toolchain"; then
  printf '%s\n' 'No complete verified compiler is cached; downloading into an isolated staging directory.'
  STAGED_TOOLCHAIN="$WORK/toolchain.staging"
  # These paths are owned by this build profile, never a shared SDK directory.
  rm -rf "$STAGED_TOOLCHAIN"
  mkdir -p "$STAGED_TOOLCHAIN"
  archive="https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86/+archive/$TOOLCHAIN_COMMIT/clang-r547379.tar.gz"
  if curl --fail --location --retry 0 --connect-timeout 20 --max-time 420 --speed-time 30 --speed-limit 1024 \
    "$archive" --output "$WORK/clang.tar.gz" && tar -xzf "$WORK/clang.tar.gz" -C "$STAGED_TOOLCHAIN"; then
    echo 'official scoped archive' > "$WORK/toolchain-download-method.txt"
  else
    printf '%s\n' 'Scoped archive failed; fetching the same pinned official prebuilt through sparse Git.'
    rm -rf "$STAGED_TOOLCHAIN"
    mkdir -p "$STAGED_TOOLCHAIN" "$WORK/clang-prebuilts"
    if [ ! -d "$WORK/clang-prebuilts/.git" ]; then
      git -C "$WORK/clang-prebuilts" init -q
      git -C "$WORK/clang-prebuilts" remote add origin https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86
    fi
    [ "$(git -C "$WORK/clang-prebuilts" remote get-url origin)" = https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86 ] || fail 'unexpected compiler prebuilt origin'
    git -C "$WORK/clang-prebuilts" config remote.origin.promisor true
    git -C "$WORK/clang-prebuilts" config remote.origin.partialclonefilter blob:none
    git -c protocol.version=2 -C "$WORK/clang-prebuilts" fetch --depth=1 --filter=blob:none --no-tags origin "$TOOLCHAIN_COMMIT"
    git -C "$WORK/clang-prebuilts" sparse-checkout init --cone
    git -C "$WORK/clang-prebuilts" sparse-checkout set clang-r547379
    git -c protocol.version=2 -C "$WORK/clang-prebuilts" checkout -q --detach FETCH_HEAD
    [ "$(git -C "$WORK/clang-prebuilts" rev-parse HEAD)" = "$TOOLCHAIN_COMMIT" ] || fail 'unexpected compiler prebuilt revision'
    cp -a "$WORK/clang-prebuilts/clang-r547379/." "$STAGED_TOOLCHAIN/"
    rm -rf "$WORK/clang-prebuilts"
    echo 'official Git protocol v2, shallow filtered sparse checkout' > "$WORK/toolchain-download-method.txt"
  fi
  verify_compiler "$STAGED_TOOLCHAIN" || fail 'downloaded compiler failed completeness or pinned identity checks'
  prune_compiler_variants "$STAGED_TOOLCHAIN"
  verify_compiler "$STAGED_TOOLCHAIN" || fail 'staged compiler failed verification after removing unused variants'
  rm -rf "$WORK/toolchain.incomplete"
  if [ -e "$WORK/toolchain" ]; then mv "$WORK/toolchain" "$WORK/toolchain.incomplete"; fi
  mv "$STAGED_TOOLCHAIN" "$WORK/toolchain"
  rm -rf "$WORK/toolchain.incomplete"
  rm -f "$WORK/clang.tar.gz"
else
  prune_compiler_variants "$WORK/toolchain"
  verify_compiler "$WORK/toolchain" || fail 'cached compiler failed verification after removing unused variants'
fi
printf '%s  %s\n' "$BUILD_INFO_SHA256" "$WORK/toolchain/BUILD_INFO" | sha256sum --check --strict
test -x "$WORK/toolchain/bin/clang"
test -x "$WORK/toolchain/bin/ld.lld"
"$WORK/toolchain/bin/clang" --version | tee "$WORK/toolchain-version.txt"
grep -Fq 'Android (13290119, +pgo, +bolt, +lto, +mlgo, based on r547379) clang version 20.0.0' "$WORK/toolchain-version.txt" || fail 'unexpected compiler build'
grep -Fq 'b718bcaf8c198c82f3021447d943401e3ab5bd54' "$WORK/toolchain-version.txt" || fail 'unexpected LLVM source revision'
# A Codespace is persistent: do not delete its SDKs, caches or unrelated files.
export CCACHE_DIR="$WORK/ccache-$COMMON_COMMIT-$NEXT_COMMIT-$TOOLCHAIN_COMMIT"
export JOBS="${JOBS:-$(nproc)}"
bash scripts/build_lunaris_kernel.sh \
  --source "$WORK/source" --root-source "$WORK/KernelSU-Next" \
  --toolchain "$WORK/toolchain" --config inputs/meteoric-5.10.237.config \
  --abi-whitelist "$WORK/original/abi_symbollist.raw" --output-dir "$WORK/build"
"$WORK/abi-venv/bin/python" scripts/verify_kernel_abi.py \
  --original-image "$WORK/original/kernel" --symvers "$WORK/build/Module.symvers" \
  --report "$WORK/abi-report.json"

# Start a fresh candidate directory only after compilation and ABI checks pass.
# Do not reuse a stale image after a failed rerun.
mkdir -p "$DIST"
rm -f "$DIST/boot-lunaris-ksunext-v3.4.2.img" "$DIST/SHA256SUMS"
python3 scripts/repack_lunaris_boot.py --base "$BOOT" \
  --kernel "$WORK/build/Image" --tools "$WORK/mkbootimg" \
  --output "$DIST/boot-lunaris-ksunext-v3.4.2.img" --partition-size 100663296
MANAGER="$DIST/KernelSU_Next_v3.4.2_33355-release.apk"
if [ ! -f "$MANAGER" ]; then
  gh release download v3.4.2 --repo KernelSU-Next/KernelSU-Next \
    --pattern KernelSU_Next_v3.4.2_33355-release.apk --dir "$DIST"
fi
printf '%s  %s\n' "$MANAGER_SHA256" "$MANAGER" | sha256sum --check --strict
cp "$WORK/build/Image" "$WORK/build/used.config" "$WORK/build/baseline.config" "$WORK/build/baseline-provenance.json" \
  "$WORK/build/embedded-stock.config" "$WORK/build/config.diff" "$WORK/build/config-drift.json" \
  "$WORK/build/kernelrelease.txt" "$WORK/build/Module.symvers" "$WORK/build/System.map" \
  "$WORK/build/abi_symbollist.raw" "$WORK/build/build.log" "$WORK/build/preflight.log" \
  "$WORK/build/preflight-passed.txt" "$WORK/build/build-state.json" "$WORK/abi-report.json" \
  "$WORK/toolchain-storage.log" "$DIST/"
cp "$WORK/original/abi-baseline.json" "$DIST/original-abi-baseline.json"
python3 - "$WORK" "$DIST" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
work, dist = map(pathlib.Path, sys.argv[1:])
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def revision(path): return subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
manifest = {
    'device': 'Nothing Phone (2) / Pong', 'rom': 'installed LunarisOS',
    'status': 'test candidate; device boot not verified', 'build_host': 'persistent cloud / Codespaces',
    'kernel_release': (work / 'build/kernelrelease.txt').read_text().strip(),
    'base_boot_sha256': digest(work / 'boot-current.img'),
    'kernel_source': revision(work / 'source'), 'root_source': revision(work / 'KernelSU-Next'),
    'kernel_repository': os.environ['SOURCE_REPO'], 'build_profile': os.environ['BUILD_PROFILE'],
    'configuration_target': 'vendor/meteoric_defconfig', 'lto': 'none',
    'effective_baseline_config_sha256': digest(work / 'build/baseline.config'),
    'embedded_stock_config_sha256': digest(work / 'build/embedded-stock.config'),
    'root': 'KernelSU Next v3.4.2 / 33355, built in', 'susfs': False,
    'toolchain_source': os.environ['TOOLCHAIN_COMMIT'],
    'compiler_build_info_sha256': digest(work / 'toolchain/BUILD_INFO'),
    'compiler': subprocess.check_output([str(work / 'toolchain/bin/clang'), '--version'], text=True).splitlines()[0],
    'boot_tools_source': revision(work / 'mkbootimg'),
    'abi_extractor_source': os.environ['ABI_EXTRACTOR_COMMIT'],
    'abi_whitelist_sha256': digest(work / 'original/abi_symbollist.raw'),
    'build_definition_commit': revision(pathlib.Path.cwd()),
    'abi_report': json.loads((work / 'abi-report.json').read_text()),
}
(dist / 'provenance.json').write_text(json.dumps(manifest, indent=2) + '\n')
(dist / 'TEST-CANDIDATE.txt').write_text(
    'Cloud build and static checks passed. Device boot has not been verified.\n'
    'Keep the original boot image. Use temporary boot to test Android, radio, Wi-Fi, camera,\n'
    'manager/driver version and root allow/deny behavior before considering persistent use.\n'
    'This build does not flash or modify a phone.\n')
PY
python3 scripts/write_artifact_checksums.py --directory "$DIST"
if [ "$PUBLISH" = true ]; then
  [ "$(gh api "repos/$REPOSITORY" --jq .visibility)" = private ] || fail 'expected a private artifact repository'
  RELEASE=lunaris-ksunext-v3.4.2-test-candidate
  if gh release view "$RELEASE" --repo "$REPOSITORY" >/dev/null 2>&1; then
    gh release upload "$RELEASE" "$DIST"/* --repo "$REPOSITORY" --clobber
  else
    gh release create "$RELEASE" "$DIST"/* --repo "$REPOSITORY" --prerelease \
      --target "$(git rev-parse HEAD)" --title 'Lunaris Meteoric / KernelSU Next v3.4.2 test candidate' \
      --notes-file "$DIST/TEST-CANDIDATE.txt"
  fi
fi
printf 'Verified test candidate available in %s\n' "$DIST"
