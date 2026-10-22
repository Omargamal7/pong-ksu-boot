# Pong boot images

Cloud-only boot-image preparation for Nothing Phone (2), codename `Pong`.

## Installed Lunaris: replace built-in KernelSU with KernelSU Next

**Source compatibility investigation is in progress.** The initial cloud build
compiled successfully with enough disk space, but its reconstructed ACK baseline
failed the original kernel's ABI check. No boot candidate was published. Run
**Actions → Diagnose Meteoric source ABI fingerprints** to check source and
configuration candidates with three small object builds before another full
kernel build. Passing this probe still requires the complete ABI comparison.

Run **Actions → Build Lunaris KernelSU Next → Run workflow**. The workflow builds
on a GitHub-hosted Ubuntu runner. Nothing runs on the phone, and no workflow
flashes a partition.

The input is the installed Lunaris image uploaded as `boot-current.img` in this
repository's `lunaris-boot-input` release. It is **not an unmodified ROM download**:
it already contains the older KernelSU. The verified input is:

- Boot SHA-256: `f86ca8c2a1f731a921813cecf0de1d693ed795f684cfb10cf1140bee4781cc7d`.
- Boot header v4, partition size 100663296 bytes (96 MiB).
- Kernel `5.10.237-Meteoric`; saved config SHA-256
  `a8b21ecc239eb1cbb9a72ccca8c5fa98c9833c2b003472fc840e099ef26ab778`.

The build uses clean Meteoric source from
`Pong-Development/kernel_nothing_sm8475_old` commit
`7cccdf9b9c80932d8402e6fe1d51e56fff7fbdb2` with
`vendor/meteoric_defconfig`, the historical Lunaris device recipe. It uses the
original Android Clang 20 `r547379` build 13290119, pinned to official prebuilt
commit `2d4dcbff7cb42f360f368bacf51cfe711fd1cb0f`. It integrates exactly one root
core: official **KernelSU Next v3.4.2**, commit
`4c706b5938665866ebe16944a481a7b91e3396af`, built in with `CONFIG_KSU=y`.
No SUSFS patches are added. Runner disk cleanup and bounded caches provide
storage headroom; the actual recipe retains its original LTO setting of none.
The earlier `pong-nos41-kernel-build` workflow supplies the repacking approach,
while the uploaded kernel and historical Lunaris sources determine this profile.

Before compiling the full kernel, the build validates configuration transitions,
prepares generated headers and compiles the Next driver objects. This catches
source/API problems before spending time on the rest of the kernel.

Meteoric's [pinned source](https://github.com/Pong-Development/kernel_nothing_sm8475_old/blob/7cccdf9b9c80932d8402e6fe1d51e56fff7fbdb2/kernel/Makefile) explains the mismatch: `kernel/Makefile` embeds
`arch/arm64/configs/stock_defconfig` instead of the active build configuration.
That file's hash exactly matches the uploaded image's saved config. The
historical [Lunaris device recipe from December 7, 2025](https://github.com/Pong-Development/device_nothing_Pong/blob/7a4ab944183da07a5547f4f8a3041f7ee418a188/BoardConfig.mk) instead selects
`vendor/meteoric_defconfig`. The diagnostic workflow checks this actual recipe
and the saved config separately using the original Android Clang 20 build.

The full build resolves that actual defconfig before integrating Next and saves
it as `baseline.config`. The validator permits only the reviewed Next settings
against that baseline. KASAN hardware tags and shadow call stacks are preserved;
CFI remains disabled, matching the original binary. The embedded stock config
is retained as input evidence and never used as the active build config.

Before packaging, the workflow extracts the original kernel's exported-symbol
CRCs and checks every original export against the rebuilt kernel's
`Module.symvers`. Missing exports, changed CRCs, incomplete extraction, unexpected
configuration changes, or a changed kernel release fail the build. The original
ramdisk and every boot header field except kernel length must survive repacking.
The output must fit the original partition.

A passing run provides `boot-lunaris-ksunext-v3.4.2.img`, the matching official
manager APK, SHA-256 checksums, build/configuration logs and provenance. Failure
diagnostics are retained separately and do not provide a boot candidate. The
repacked image is compact and has no copied AVB footer or OEM signature.

**A passing build is a test candidate.** Static ABI checks cannot establish
hardware compatibility. Keep the original `boot-current.img` available for
recovery. Temporary boot must confirm Android, radio, Wi-Fi, camera, the Next
manager/driver version, and root allow/deny behavior before persistent use.
No data wipe, vbmeta modification or inactive-slot change is part of this workflow.

Validate the build definitions:

```sh
python3 -m unittest discover -s tests -v
bash -n scripts/build_lunaris_kernel.sh
```

## Parallel Codespaces backup

Create a Codespace for this repository using the **Lunaris KernelSU Next cloud
build** configuration. Select a machine with **16 cores / 128 GB storage** so the
persistent build has more disk headroom than the standard Actions runner:

https://github.com/codespaces/new?repo=1293663412&ref=master

The post-create command starts the build in the background. Read its progress in
`work/cloud/session.log`; compiled objects and downloads remain in `work/cloud/`
for incremental retries. A passing build publishes the same verified files to
the private prerelease `lunaris-ksunext-v3.4.2-test-candidate`. No phone resources
are used and no partition is flashed. Stop the Codespace after collecting the
result to avoid leaving cloud compute running.

```sh
tail -f work/cloud/session.log
# Resume a failed build without deleting its compiled objects:
bash scripts/build_lunaris_cloud.sh --background
```

The script requires at least 45 GiB free before its first source fetch and keeps
the builder's 35 GiB pre-compilation check. It refuses unrelated edits in its
private source checkout. Use `--no-publish` when only local Codespace artifacts
are wanted. Existing Actions runs continue independently.

## Earlier Lineage module patcher

`Patch boot.img with KernelSU-Next` is the older, independent LKM workflow. It
downloads `boot.img` from the `input` release and adds a root module. It does
not remove an existing built-in KernelSU core and must not be used for this
Lunaris replacement.
