# Pong boot images

Cloud-only boot-image preparation for Nothing Phone (2), codename `Pong`.

## Installed Lunaris: replace built-in KernelSU with KernelSU Next

The historical Meteoric source and actual Lunaris config passed the small ABI
probe: all 155 original exports from the three test objects matched, including
`module_layout`, `I_BDEV` and `__alloc_skb`. The initial reconstructed ACK baseline
failed compatibility and produced no boot candidate. The full original-export
comparison remains mandatory before a candidate can be packaged.

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
diagnostics are retained separately and do not provide a boot candidate.

The boot image keeps the original's AVB layout: it fills the 96 MiB partition
and ends with an unsigned (`NONE`) hash footer for `boot` that reuses the
original's salt, properties, rollback settings and release string. Only the
payload size and digest change; `boot-lunaris-ksunext-v3.4.2.avb.json` records
both footers. The footer is added with `avbtool.py` from AOSP `external/avb`
commit `761178607206f4cb2af79ed9eec52d8cbd814adb` (SHA-256
`e5a664a38db623da00f080219bc0ee60a640a9dc4a872803616fae4938ac749b`). The
uploaded original's own footer digest does not match its payload; the
`lunaris-boot-input` release describes that image as extracted from the phone. No
OEM signature is added and the vbmeta partition is not changed, so its record
for `boot`, if any, will not match this image either.

**A passing build is a test candidate.** Static ABI checks cannot establish
hardware compatibility. Keep the original `boot-current.img` available for
recovery. Temporary boot must confirm Android, radio, Wi-Fi, camera, the Next
manager/driver version, and root allow/deny behavior before persistent use.
No data wipe, vbmeta modification or inactive-slot change is part of this workflow.

Validate the build definitions:

```sh
python3 -m unittest discover -s tests -v
bash -n scripts/build_lunaris_kernel.sh
# AVB footer tests need the pinned avbtool.py:
AVBTOOL=/path/to/avbtool.py python3 -m unittest tests.test_avb_footer -v
```

## Experimental: KernelSU Next dev-susfs (SUSFS v2.3.0 built in)

**Actions → EXPERIMENTAL Build Lunaris KernelSU Next dev-susfs** is a separate
prototype. It never replaces the release workflow's artifacts. It differs only in:

- Root core: [pershoot/KernelSU-Next](https://github.com/pershoot/KernelSU-Next)
  `dev-susfs` commit `d21e0a66c887ae4bc75b7c9f83e41761819ec09a`. That is official
  `dev` at `b427a3f9c242` plus the official `test` branch's Samsung commits and
  pershoot's SUSFS v2.3.0 integration. It is not a KernelSU Next release. Its
  Kbuild reports driver version 33405 and tag v3.4.2, which the build asserts.
- Kernel side: simonpunk [susfs4ksu](https://gitlab.com/simonpunk/susfs4ksu)
  `gki-android12-5.10` commit `f43926dc94b8ffbf9b74321313536600f69367b1`
  (`SUSFS_VERSION v2.3.0`). Its README says the patches target official
  KernelSU; pairing them with dev-susfs is untested upstream. One hunk needs
  fuzz 1 (Android's 16K padding changed `show_smap`); the build allows only that
  hunk and checks where it lands.
- The kernel patch calls `ksu_handle_sys_read(fd)` with one argument while
  dev-susfs defines three; the build fails if dev-susfs ever starts reading the
  two extra parameters.
- Configuration: Samsung helpers off, `CONFIG_KSU_SUSFS` and all nine SUSFS
  features on, checked by `verify_lunaris_config.py --profile next-susfs`. The
  same 14,523-export ABI comparison, repack and AVB footer apply.
- Manager: dev-susfs speaks KernelSU UAPI 6. The v3.4.2 release's `ksud`
  requires an exact match (UAPI 5) and skips post-fs-data otherwise. The
  workflow therefore tries to fetch the official CI manager built from the same
  `dev` commit (run 39297115788, artifact 12209218898) and checks its signing
  certificate the way the kernel does (`scripts/verify_manager_apk.py`). That
  cross-repository download is untested; if it fails, download the `manager`
  artifact from that run while signed in to GitHub.

## Parallel Codespaces backup

Create a Codespace for this repository using the **Lunaris KernelSU Next cloud
build** configuration. The configuration requires at least **8 cores / 32 GB RAM /
64 GB storage**. Codespace creation has reported "no machine types are
available" with both this and the earlier 16-core minimum; the cause is not
identified. Whether 64 GB leaves the 45 GiB the script needs is also unconfirmed:

https://github.com/codespaces/new?repo=1293663412&ref=master

The post-create command starts the build in the background. Read its progress in
`work/cloud/session.log`; compiled objects and downloads remain in `work/cloud/`
for incremental retries. A passing build publishes the same verified files to
the repository prerelease `lunaris-ksunext-v3.4.2-test-candidate`. No phone resources
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

## Lunaris 3.6 Minimal ROM (Pico GApps / Lowest Running Heads & Idle RAM)

Run **Actions → Build Lunaris Minimal ROM → Run workflow** (defined in
[`.github/workflows/build-lunaris-minimal-rom.yml`](.github/workflows/build-lunaris-minimal-rom.yml))
or run [`scripts/build_lunaris_minimal_rom.sh`](scripts/build_lunaris_minimal_rom.sh)
on an `x86_64` Linux cloud runner / Codespace to repack the official
`Lunaris-AOSP-Pong-OFFICIAL-3.6-Gapps-2025120905.zip` (SHA-256
`1462e100777330b7fd496fafde705dae6be582dc57c8bb2fea66474c487722ba`) into
`Lunaris-AOSP-Pong-Minimal-3.6-Pico.zip`.

- [`scripts/lunaris_rom_profile.py`](scripts/lunaris_rom_profile.py):
  Declaratively strips ~2.1 GB of uncompressed bloat across `/product`,
  `/system_ext`, `/system`, and `/vendor` while protecting core telephony,
  camera, Bluetooth, Wi-Fi, UDFPS, QuickStep launcher, and Pico GApps
  (`PrebuiltGmsCore`, `Phonesky`, `GoogleServicesFramework`, `GoogleDialer`,
  `PrebuiltBugle`, `GoogleContacts`, `LatinIMEGooglePrebuilt`, `WebViewGoogle`).
  Removes Qualcomm telemetry/tracing/diagnostic daemons (`diag-router`,
  `qcc-trd`, `qccsyshal`, `dpmd`, `dpmQmiMgr`, `tcmd`, `ssgtzd`, `dataadpl`,
  `dataqti`, `traced`, `heapprofd`, `incidentd`), AI/ML services (`AICore`,
  `DevicePersonalizationServices`/`AiAi`, `PrivateComputeServices`/`astrea`,
  `PixelAI`, `Velvet`, `Wellbeing`, `Turbo`, `HotwordEnrollment`), NFC/eSE,
  Print/mDNS, Wi-Fi Display, TrustedUI/Soter/eID/VPP, and ~180 RRO theme
  overlays. Tunes Dalvik heap & vdex/odex madvise properties, disables
  WindowBlur, caps cached/phantom process pools via
  `/system/etc/init/init.lunaris.minimal.rc`, and patches `vbmeta_system` and
  `vbmeta_vendor` AVB flags (`flags=3`).
- [`scripts/lunaris_payload.py`](scripts/lunaris_payload.py):
  Pure-Python `CrAU` v2 OTA payload extractor, parallel `XZ` (`CHECK_NONE`) +
  `ZERO` block encoder, RSA-2048 PKCS#1 v1.5 SHA-256 signer, and A/B OTA zip
  repacker.

