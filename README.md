# pong-ksu-boot

Off-device KernelSU-Next patcher for **Nothing Phone (2)** (codename `Pong`, GKI `android12-5.10`).

The `Patch boot.img with KernelSU-Next` workflow takes the stock `boot.img` (uploaded
as an asset named `boot.img` on the `input` release), patches it in **LKM mode** with the
official `ksud boot-patch` on a Linux runner (keeps LineageOS's own kernel), and uploads
the result as the `boot-kernelsu` artifact.

## Use
1. Upload a stock `boot.img` to the `input` release (asset name `boot.img`).
2. Run the workflow (Actions → *Patch boot.img with KernelSU-Next* → Run).
3. Download the `boot-kernelsu` artifact.

## Flash (fastboot)
```
fastboot flash boot boot-kernelsu.img
# then disable AVB verification so the patched boot will boot:
fastboot --disable-verity --disable-verification flash vbmeta vbmeta.img
```
Source of the boot image: `lineage-23.2-*-nightly-Pong-signed.zip` (payload.bin → `boot`).
