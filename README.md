# Pong boot images

Cloud-only boot-image preparation for Nothing Phone (2), codename `Pong`.

## Installed Lunaris: replace built-in KernelSU with KernelSU Next

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

The build reuses the Meteoric recipe/configuration and AOSP repacking approach
from `Omargamal7/pong-nos41-kernel-build` commit
`e56490538b35ec3095a401fc1c1aba975910e590`. It starts from clean ACK commit
`744d5c916fbbd6fb15f92c69c32de440d7345c6f` and integrates exactly one root core:
official **KernelSU Next v3.4.2**, commit
`4c706b5938665866ebe16944a481a7b91e3396af`, built in with `CONFIG_KSU=y`.
No SUSFS patches are added. Thin LTO and runner disk cleanup address resource
failures in the earlier workflow. The prior recipe is a reconstruction baseline;
it is not proof of an exact reproduction of the shipped Meteoric source.

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
