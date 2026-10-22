#!/usr/bin/env bash
# Persistent cloud/Codespaces counterpart to the GitHub Actions build.
set -euo pipefail

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"
mkdir -p work/cloud
if [ "${1:-}" = --background ]; then
  shift
  nohup bash "$SCRIPT_DIR/build_lunaris_cloud.sh" "$@" \
    > work/cloud/session.log 2>&1 < /dev/null &
  printf '%s\n' "$!" > work/cloud/build.pid
  printf 'Cloud build started. Follow: tail -f work/cloud/session.log\n'
  exit 0
fi
PUBLISH=true
if [ "${1:-}" = --no-publish ]; then PUBLISH=false; shift; fi
[ "$#" = 0 ] || fail 'usage: build_lunaris_cloud.sh [--background] [--no-publish]'
[ "$(uname -m)" = x86_64 ] || fail 'this cloud build requires Linux x86_64'
command -v flock >/dev/null || fail 'flock is required'
exec 9>work/cloud/build.lock
flock -n 9 || fail 'another cloud build already holds the build lock'

REPOSITORY=Omargamal7/pong-ksu-boot
COMMON_COMMIT=744d5c916fbbd6fb15f92c69c32de440d7345c6f
NEXT_COMMIT=4c706b5938665866ebe16944a481a7b91e3396af
TOOLCHAIN_COMMIT=54220fd601050b350b2af7adc913089ebf0e7aed
BOOT_TOOLS_COMMIT=d2bb0af5ba6d3198a3e99529c97eda1be0b5a093
ABI_EXTRACTOR_COMMIT=2d094bc9c220b6b50f8215cbc3c9e581210ce1a6
BOOT_SHA256=f86ca8c2a1f731a921813cecf0de1d693ed795f684cfb10cf1140bee4781cc7d
MANAGER_SHA256=774a5511f7034a4930e881114e202319cbdc34c5aaaa5c7c6b6d2902364e4c58
export COMMON_COMMIT NEXT_COMMIT TOOLCHAIN_COMMIT BOOT_TOOLS_COMMIT ABI_EXTRACTOR_COMMIT
WORK="$REPO_DIR/work/cloud"
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
fetch_pinned "$WORK/source" https://android.googlesource.com/kernel/common "$COMMON_COMMIT"
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
    if scmversion.read_text() != '-Meteoric\n':
        raise SystemExit('refusing to remove an unexpected .scmversion')
    scmversion.unlink()
PY

if [ ! -x "$WORK/toolchain/bin/clang" ]; then
  mkdir -p "$WORK/toolchain"
  curl --fail --location --retry 4 \
    "https://github.com/LineageOS/android_prebuilts_clang_kernel_linux-x86_clang-r416183b/archive/$TOOLCHAIN_COMMIT.tar.gz" \
    --output "$WORK/clang.tar.gz"
  tar -xzf "$WORK/clang.tar.gz" -C "$WORK/toolchain" --strip-components=1
  rm "$WORK/clang.tar.gz"
fi
# A Codespace is persistent: do not delete its SDKs, caches or unrelated files.
export CCACHE_DIR="$WORK/ccache"
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
cp "$WORK/build/Image" "$WORK/build/used.config" "$WORK/build/config.diff" "$WORK/build/config-drift.json" \
  "$WORK/build/kernelrelease.txt" "$WORK/build/Module.symvers" "$WORK/build/System.map" \
  "$WORK/build/abi_symbollist.raw" "$WORK/build/build.log" "$WORK/abi-report.json" "$DIST/"
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
    'root': 'KernelSU Next v3.4.2 / 33355, built in', 'susfs': False,
    'toolchain_source': os.environ['TOOLCHAIN_COMMIT'],
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
(cd "$DIST" && sha256sum -- * > SHA256SUMS)
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
