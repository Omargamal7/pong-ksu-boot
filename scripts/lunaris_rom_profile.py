"""Declarative stripping and RAM/thread optimization profile for Lunaris-AOSP 3.6 Pong."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
from pathlib import Path

SOURCE_ROM_FILENAME = "Lunaris-AOSP-Pong-OFFICIAL-3.6-Gapps-2025120905.zip"
SOURCE_ROM_URL = (
    "https://sourceforge.net/projects/ghosuto/files/Pong/"
    "Lunaris-AOSP-Pong-OFFICIAL-3.6-Gapps-2025120905.zip/download"
)
SOURCE_ROM_SIZE = 2990178628
SOURCE_ROM_SHA256 = "1462e100777330b7fd496fafde705dae6be582dc57c8bb2fea66474c487722ba"
SOURCE_PAYLOAD_SHA256 = "f699dd711a87963d9bf238fd9102d35d2cd0cbd593be31771c3c53b0673f38e8"

OUTPUT_ROM_VERSION = "Lunaris-AOSP-Pong-Minimal-3.6-Pico"
OUTPUT_DISPLAY_VERSION = "v3.6-Minimal-Pico"

# Essential packages that must never be removed from the ROM.
PROTECTED_PATHS: tuple[str, ...] = (
    "product/priv-app/PrebuiltGmsCore",
    "product/priv-app/Phonesky",
    "product/priv-app/ConfigUpdater",
    "product/priv-app/GoogleOneTimeInitializer",
    "product/priv-app/SetupWizardPrebuilt_bd3a",
    "product/priv-app/SettingsIntelligenceGooglePrebuilt",
    "product/priv-app/GoogleDialer",
    "product/priv-app/PrebuiltBugle",
    "product/priv-app/PrebuiltDeskClockGoogle_76007351",
    "product/priv-app/ImsServiceEntitlement",
    "product/app/LatinIMEGooglePrebuilt",
    "product/app/WebViewGoogle",
    "product/app/TrichromeLibrary",
    "product/app/GoogleContacts",
    "product/app/Aperture",
    "system_ext/priv-app/GoogleServicesFramework",
    "system_ext/priv-app/SystemUI",
    "system_ext/priv-app/Settings",
    "system_ext/priv-app/Launcher3QuickStep",
    "system_ext/priv-app/LineageParts",
    "system_ext/priv-app/LineageSettingsProvider",
    "system_ext/priv-app/CarrierConfig",
    "system_ext/priv-app/ims",
    "system_ext/priv-app/qcrilmsgtunnel",
    "system_ext/priv-app/DolbyManager",
    "system_ext/priv-app/Pong_DeviceExtras",
    "system/priv-app/TeleService",
    "system/priv-app/Telecom",
    "system/priv-app/TelephonyProvider",
    "system/priv-app/NetworkStack",
    "system/priv-app/PackageInstaller",
    "system/priv-app/PermissionController",
    "system/priv-app/SettingsProvider",
)

# 1. /product paths to remove (AI/ML bloat, non-Pico Google apps, duplicate launchers).
PRODUCT_REMOVE_PATHS: tuple[str, ...] = (
    # /product/priv-app bloat
    "priv-app/AICorePrebuilt-aicore_20250130.00_RC01",
    "priv-app/ANGLE",
    "priv-app/AndroidAutoStubPrebuilt",
    "priv-app/CarrierLocation",
    "priv-app/CarrierMetrics",
    "priv-app/CreativeAssistant",
    "priv-app/CustomizationBundlePrebuilt",
    "priv-app/DeviceIntelligenceNetworkPrebuilt-astrea_20240329.00_RC02",
    "priv-app/DevicePersonalizationPrebuiltPixel2025-playstore_aiai_20250306.00_RC10",
    "priv-app/GoogleRestorePrebuilt-v793553",
    "priv-app/HotwordEnrollmentOKGoogleHEXAGON",
    "priv-app/HotwordEnrollmentXGoogleHEXAGON",
    "priv-app/Lawnchair",
    "priv-app/PixelAIPrebuilt",
    "priv-app/PrebuiltPixelCoreServices",
    "priv-app/ScribePrebuilt_v8.2.717788270",
    "priv-app/TurboPrebuilt",
    "priv-app/Velvet",
    "priv-app/WallpaperEffect",
    "priv-app/WellbeingPrebuilt",
    # /product/app bloat
    "app/CalendarGooglePrebuilt",
    "app/Camelot",
    "app/Chrome",
    "app/Chrome-Stub",
    "app/DevicePolicyPrebuilt-v10334460",
    "app/GoogleTTS",
    "app/Lawnicons",
    "app/LocationHistoryPrebuilt",
    "app/MarkupGoogle_v2",
    "app/Music",
    "app/NgaResources",
    "app/Photos",
    "app/PixelThemesStub2025_and_newer",
    "app/PrebuiltGoogleAdservicesTvp",
    "app/PrebuiltGoogleTelemetryTvp",
    "app/ThemesStub",
    "app/WallpaperAIPrebuilt_10000372",
    "app/WallpaperEmojiPrebuilt-foldable-wallpaper",
    "app/talkback",
    # /product/etc/permissions for removed packages
    "etc/permissions/android.software.angle.xml",
    "etc/permissions/com.google.android.aicore.xml",
    "etc/permissions/com.google.android.apps.dreamliner.xml",
    "etc/permissions/com.google.android.apps.pixel.subzero.xml",
    "etc/permissions/com.google.android.apps.stargate.support.xml",
    "etc/permissions/com.google.android.apps.stargate.xml",
    "etc/permissions/com.google.android.odad.xml",
    "etc/permissions/org.lineageos.profiles.xml",
    "etc/permissions/privapp-permissions-app.lawnchair.xml",
    "etc/permissions/privapp-permissions-hotword.xml",
    # /product/overlay bindings to AiAi / AiCore / Wellbeing / Flipendo / removed apps
    "overlay/PixelConfigOverlay2018.apk",
    "overlay/PixelConfigOverlay2019.apk",
    "overlay/PixelConfigOverlay2019Midyear.apk",
    "overlay/PixelConfigOverlayCommon.apk",
    "overlay/BtHelper__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/ColumbusService__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/DeviceDiagnostics__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/FaceUnlockOverlay.apk",
    "overlay/FaceUnlock__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/GameSpace__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/LawnchairOverlay.apk",
    "overlay/NfcOverlay.apk",
    "overlay/OmniJaws__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/OmniStyle__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/ParanoidGlyphPhone2__lineage_Pong__auto_generated_rro_product.apk",
    "overlay/PixelAccessibilityMenuOverlay",
    "overlay/PixelBuiltInPrintServiceOverlay",
    "overlay/PixelFlipendoOverlay",
    "overlay/PixelLauncherIconsOverlay",
    "overlay/PixelLauncherNoGestureHintOverlay.apk",
    "overlay/PixelLauncherOverlayCustom.apk",
    "overlay/Updater__lineage_Pong__auto_generated_rro_product.apk",
)

# Prefixes of cosmetic theme packs in /product/overlay to strip so PackageManager
# and OverlayManagerService do not scan and track ~180 unused theme APKs.
PRODUCT_OVERLAY_REMOVE_PREFIXES: tuple[str, ...] = (
    "Aurora",
    "Bars",
    "Bold",
    "DisplayCutout",
    "FaintUI",
    "Forlorn",
    "Gradicon",
    "Hollow",
    "IOS",
    "IconPack",
    "IconShape",
    "Inside",
    "Ios",
    "LandscapeWiFi",
    "Linear",
    "MiniSignal",
    "Navbar",
    "NotchBarKiller",
    "NothingDot",
    "PillsSignal",
    "Plumpy",
    "PowerCyberPunk",
    "PowerDuoline",
    "PowerFluid",
    "PowerIOS",
    "PowerLayers",
    "ROGSignal",
    "RoundWiFi",
    "SeaSignal",
    "SleekSignal",
    "Sneaky",
    "Stroke",
    "Waffle",
    "Wannui",
    "Wavy",
    "WindowsSignal",
    "Xperia",
    "ZigZag",
)

# 2. /system_ext paths to remove (persistent background apps, QTI telemetry, WFD).
SYSTEM_EXT_REMOVE_PATHS: tuple[str, ...] = (
    # /system_ext/app
    "app/AccessibilityMenu",
    "app/Flipendo",
    "app/OmniJaws",
    "app/Profiles",
    "app/QCC",
    "app/ScreenshotEditor",
    "app/UdfpsAnimations",
    "app/UdfpsIcons",
    # /system_ext/priv-app
    "priv-app/AvatarPickerGoogle",
    "priv-app/AxionWidgets",
    "priv-app/BtHelper",
    "priv-app/ColumbusService",
    "priv-app/FaceUnlock",
    "priv-app/GameSpace",
    "priv-app/GlyphAdapter",
    "priv-app/LMOFreeform",
    "priv-app/LMOFreeformSidebar",
    "priv-app/LMOSystemUIClock",
    "priv-app/MoseyApp",
    "priv-app/NexusLauncherRelease",
    "priv-app/OmniStyle",
    "priv-app/ParanoidGlyphPhone2",
    "priv-app/QuickAccessWallet",
    "priv-app/Seedvault",
    "priv-app/SystemUIClocks-BigNum",
    "priv-app/SystemUIClocks-Calligraphy",
    "priv-app/SystemUIClocks-Flex",
    "priv-app/SystemUIClocks-Growth",
    "priv-app/SystemUIClocks-Inflate",
    "priv-app/SystemUIClocks-Metro",
    "priv-app/SystemUIClocks-NumOverlap",
    "priv-app/TurboAdapter",
    "priv-app/Updater",
    "priv-app/WfdService",
    "priv-app/dpmserviceapp",
    # /system_ext/etc/init
    "etc/init/dpmd.rc",
    "etc/init/init.lineage-updater.rc",
    "etc/init/tcmd.rc",
    "etc/init/vendor.qti.hardware.qccsyshal@1.2-service.rc",
    "etc/init/vendor.qti.qccsyshal_aidl-service.rc",
    "etc/init/wfdservice.rc",
    # /system_ext/etc/vintf/manifest
    "etc/vintf/manifest/vendor.qti.qccsyshal_aidl-service.xml",
    # /system_ext/etc/permissions
    "etc/permissions/android.hardware.biometrics.face.xml",
    "etc/permissions/com.android.bluetooth.bthelper.xml",
    "etc/permissions/com.android.hotwordenrollment.common.util.xml",
    "etc/permissions/com.qti.dpmframework.xml",
    "etc/permissions/com.qti.qcc.vendor_qcc.xml",
    "etc/permissions/dpmapi.xml",
    "etc/permissions/permissions_com.stevesoltys.seedvault.xml",
    "etc/permissions/privapp-permissions-com.google.android.mosey.xml",
    "etc/permissions/privapp_allowlist_com.libremobileos.freeform.xml",
    "etc/permissions/privapp_allowlist_com.libremobileos.sidebar.xml",
    "etc/permissions/privapp_whitelist_co.aospa.glyph.xml",
    "etc/permissions/privapp_whitelist_co.aospa.sense.xml",
    "etc/permissions/privapp_whitelist_com.android.axionwidgets.xml",
    "etc/permissions/privapp_whitelist_io.chaldeaprjkt.gamespace.xml",
    "etc/permissions/privapp_whitelist_org.lineageos.profiles.xml",
    "etc/permissions/privapp_whitelist_org.lineageos.updater.xml",
    "etc/permissions/privapp_whitelist_org.protonaosp.columbus.xml",
    "etc/permissions/wfd-system-ext-privapp-permissions-qti.xml",
)

# 3. /system paths to remove (NFC/SE, Print, Dreams, EasterEgg, Tracing/Incident daemons).
# Note: in system-as-root images, /system contents reside under system/ inside system.img.
SYSTEM_REMOVE_PATHS: tuple[str, ...] = (
    "system/app/BasicDreams",
    "system/app/BluetoothMidiService",
    "system/app/BookmarkProvider",
    "system/app/EasterEgg",
    "system/app/GooglePrintRecommendationService",
    "system/app/PacProcessor",
    "system/app/PrintSpooler",
    "system/app/SecureElement",
    "system/app/WallpaperBackup",
    "system/priv-app/BuiltInPrintService",
    "system/priv-app/DeviceDiagnostics",
    "system/priv-app/DynamicSystemInstallationService",
    "system/priv-app/LiveWallpapersPicker",
    "system/priv-app/Tag",
    "system/etc/init/atrace.rc",
    "system/etc/init/dmesgd.rc",
    "system/etc/init/heapprofd.rc",
    "system/etc/init/incidentd.rc",
    "system/etc/init/mdnsd.rc",
    "system/etc/init/perfetto.rc",
    "system/etc/init/simpleperf.rc",
    "system/etc/init/traced_perf.rc",
    "system/etc/permissions/android.software.live_wallpaper.xml",
    "system/etc/permissions/com.android.nfc_extras.xml",
)

# 4. /vendor paths to remove (QTI diag, telemetry, NFC/eSE, WFD, VPP, TUI, eID, Soter).
VENDOR_REMOVE_PATHS: tuple[str, ...] = (
    "etc/init/android.hardware.drm@1.1-service.wfdhdcp.rc",
    "etc/init/com.qualcomm.qti.wifidisplayhal@1.0-service.rc",
    "etc/init/dataadpl.rc",
    "etc/init/dataqti.rc",
    "etc/init/dpmQmiMgr.rc",
    "etc/init/feature_enabler_client.rc",
    "etc/init/init.embmssl_server.rc",
    "etc/init/init.qdmastats.rc",
    "etc/init/init.qti.fm.rc",
    "etc/init/nfc-service-nxp.rc",
    "etc/init/qconfig.rc",
    "etc/init/ssgtzd.rc",
    "etc/init/trusteduilistener.rc",
    "etc/init/vendor.dpmd.rc",
    "etc/init/vendor.qti.diag.rc",
    "etc/init/vendor.qti.esepowermanager@1.1-service.rc",
    "etc/init/vendor.qti.hardware.eid@1.0-service.rc",
    "etc/init/vendor.qti.hardware.soter@1.0-service.rc",
    "etc/init/vendor.qti.hardware.trustedui@1.0-service-qti.rc",
    "etc/init/vendor.qti.hardware.tui_comm@1.0-service-qti.rc",
    "etc/init/vppservice.rc",
    "etc/init/wfdvndservice.rc",
    "etc/vintf/manifest/nfc-service-nxp.xml",
    "etc/vintf/manifest/vendor.qti.diag.hal.service.xml",
    "etc/vintf/manifest/vendor.qti.hardware.qconfig@1.0-service.xml",
    "etc/vintf/manifest/vendor.qti.hardware.vpp@2.0-service.xml",
    "etc/permissions/android.hardware.nfc.hce.xml",
    "etc/permissions/android.hardware.nfc.hcef.xml",
    "etc/permissions/android.hardware.nfc.xml",
    "etc/permissions/com.android.nfc_extras.xml",
    "etc/permissions/com.nxp.mifare.xml",
    "overlay/NTNfcResTarget.apk",
)

# HAL names to strip from /vendor/etc/vintf/manifest.xml so servicemanager/hwservicemanager
# and system_server never wait on or query stripped vendor HALs.
VENDOR_MANIFEST_REMOVE_HALS: tuple[str, ...] = (
    "android.hardware.drm",  # wfdhdcp instance in manifest.xml (widevine is in manifest/ fragment)
    "android.hardware.secure_element",
    "com.dsi.ant",
    "com.qualcomm.qti.dpm.api",
    "vendor.qti.esepowermanager",
    "vendor.qti.hardware.dpmservice",
    "vendor.qti.hardware.eid",
    "vendor.qti.hardware.embmssl",
    "vendor.qti.hardware.fm",
    "vendor.qti.hardware.qccvndhal",
    "vendor.qti.hardware.trustedui",
    "vendor.qti.hardware.tui_comm",
    "vendor.qti.hardware.wifidisplaysession",
)

# 5. Build property overrides per partition to minimize heap bloat, mmap pre-faulting,
# background blur, and telemetry/tracing daemons.
SYSTEM_PROP_OVERRIDES: dict[str, str] = {
    "ro.lunaris.build.version": OUTPUT_ROM_VERSION,
    "ro.lunaris.display.version": OUTPUT_DISPLAY_VERSION,
    "ro.lunaris.package.type": "Pico",
    "persist.traced.enable": "0",
    "debug.atrace.tags.enableflags": "0",
    "dalvik.vm.madvise.vdexfile.size": "0",
    "dalvik.vm.madvise.odexfile.size": "0",
    "dalvik.vm.madvise.artfile.size": "0",
    "dalvik.vm.minidebuginfo": "false",
    "dalvik.vm.dex2oat-minidebuginfo": "false",
    "dalvik.vm.foreground-heap-growth-multiplier": "1.0",
    "dalvik.vm.usap_pool_enabled": "false",
    "ro.custom.blur.enable": "false",
    "persist.sysui.disableBlur": "true",
    "ro.surface_flinger.supports_background_blur": "0",
    "lunaris_udfps_custom": "false",
    "persist.sys.default_launcher": "0",
    "persist.sys.quickswitch_pixel_shipped": "0",
    "ro.logd.size": "64K",
    "ro.logd.size.stats": "32K",
    "ro.logd.size.events": "64K",
    "ro.logd.size.crash": "64K",
}

SYSTEM_EXT_PROP_OVERRIDES: dict[str, str] = {
    "ro.face.sense_service": "false",
    "persist.vendor.dpm.feature": "0",
}

PRODUCT_PROP_OVERRIDES: dict[str, str] = {
    "remote_provisioning.enable_rkpd": "false",
    "ro.opa.eligible_device": "false",
    "ro.atrace.core.services": "",
    "persist.sys.quickswitch_lawnchair_shipped": "0",
}

VENDOR_PROP_OVERRIDES: dict[str, str] = {
    "dalvik.vm.heapstartsize": "8m",
    "dalvik.vm.heapgrowthlimit": "256m",
    "dalvik.vm.heapsize": "512m",
    "dalvik.vm.heaptargetutilization": "0.75",
    "dalvik.vm.heapminfree": "2m",
    "dalvik.vm.heapmaxfree": "8m",
    "ro.surface_flinger.max_frame_buffer_acquired_buffers": "2",
    "ro.surface_flinger.supports_background_blur": "0",
    "persist.vendor.dpm.vndr.feature": "0",
    "persist.vendor.dpm.vndr.halservice.enable": "0",
    "ro.vendor.qti.sys.fw.bservice_enable": "true",
    "ro.vendor.qti.sys.fw.bservice_limit": "4",
    "ro.vendor.qti.sys.fw.bservice_age": "5000",
    "ro.logd.size.stats": "32K",
}

MINIMAL_INIT_RC_CONTENT = """# Lunaris-AOSP Pong Minimal Pico-GMS Runtime Tuning
on early-init
    setprop persist.traced.enable 0
    setprop debug.atrace.tags.enableflags 0

on init
    setprop persist.vendor.dpm.feature 0
    setprop persist.vendor.dpm.vndr.feature 0
    setprop persist.vendor.dpm.vndr.halservice.enable 0
    setprop remote_provisioning.enable_rkpd false

on property:sys.boot_completed=1
    stop traced
    stop traced_probes
    stop traced_perf
    stop heapprofd
    stop incidentd
    stop mdnsd
    stop vendor.diag-router
    stop vendor.qcc-trd
    stop vendor.ssgtzd
    stop vendor.dataadpl
    stop vendor.dataqti
    stop dpmQmiMgr
    stop vendor.dpmd
    stop dpmd
    stop tcmd
    stop qccsyshal@1.2-service
    stop qccsyshal_aidl-service
    stop vendor.nfc_hal_service
    stop qti_esepowermanager_service_1_1
    # Reclaim boot page cache and shrink pre-allocated DMA-BUF page pools
    write /proc/sys/vm/drop_caches 3
    write /proc/sys/vm/swappiness 30
    exec - system system -- /system/bin/device_config put activity_manager max_cached_processes 8
    exec - system system -- /system/bin/device_config put activity_manager max_phantom_processes 4
    exec - system system -- /system/bin/device_config put runtime_native usap_pool_enabled false
"""


def update_build_prop_text(original: str, overrides: dict[str, str]) -> str:
    """Apply key=value overrides to a build.prop string, preserving existing order."""
    lines = original.splitlines()
    remaining = dict(overrides)
    updated_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            updated_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            updated_lines.append(f"{key}={remaining.pop(key)}")
        else:
            updated_lines.append(line)
    if remaining:
        updated_lines.append("")
        updated_lines.append("# Minimal ROM overrides")
        for key, val in remaining.items():
            updated_lines.append(f"{key}={val}")
    return "\n".join(updated_lines) + "\n"


def write_in_place(path: Path, data: bytes) -> None:
    """Overwrite an existing file in-place without unlinking its inode (preserves SELinux xattrs)."""
    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.truncate(0)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def strip_vintf_manifest_hals(xml_text: str, hal_names: tuple[str, ...]) -> tuple[str, list[str]]:
    """Remove specified <hal>...</hal> blocks from a VINTF manifest XML string."""
    removed: list[str] = []
    target_set = set(hal_names)
    pattern = re.compile(r"[ \t]*<hal\b[^>]*>.*?</hal>[ \t]*\r?\n?", re.DOTALL)

    def replacer(match: re.Match[str]) -> str:
        block = match.group(0)
        name_match = re.search(r"<name>\s*([^<\s]+)\s*</name>", block)
        if name_match and name_match.group(1) in target_set:
            removed.append(name_match.group(1))
            return ""
        return block

    updated = pattern.sub(replacer, xml_text)
    return updated, removed


def patch_vbmeta_flags(data: bytes, flags: int = 3) -> bytes:
    """Set AVB flags (offset 120 in AvbVBMetaImageHeader) to disable verity/hashtree checks."""
    if len(data) < 256 or data[:4] != b"AVB0":
        raise ValueError("not a valid AVB0 vbmeta image")
    buf = bytearray(data)
    struct.pack_into(">I", buf, 120, flags)
    return bytes(buf)


def remove_tree_or_file(root: Path, rel_path: str) -> tuple[bool, int]:
    """Remove a file or directory under root and return (removed, bytes_freed)."""
    for protected in PROTECTED_PATHS:
        if rel_path.endswith(protected) or protected.endswith(rel_path):
            raise ValueError(f"refusing to remove protected path: {rel_path}")
    target = root / rel_path
    if not target.exists() and not target.is_symlink():
        return False, 0
    freed = 0
    if target.is_symlink() or target.is_file():
        if not target.is_symlink():
            freed = target.stat().st_size
        target.unlink()
        return True, freed
    for dirpath, _, filenames in os.walk(target):
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if not fpath.is_symlink():
                freed += fpath.stat().st_size
    shutil.rmtree(target)
    return True, freed


def apply_partition_profile(partition: str, mount_dir: Path) -> dict:
    """Apply the minimal ROM stripping and property profile to a mounted partition."""
    removed_items: list[str] = []
    bytes_freed = 0

    if partition == "product":
        for rel in PRODUCT_REMOVE_PATHS:
            ok, freed = remove_tree_or_file(mount_dir, rel)
            if ok:
                removed_items.append(rel)
                bytes_freed += freed
        overlay_dir = mount_dir / "overlay"
        if overlay_dir.is_dir():
            for entry in sorted(overlay_dir.iterdir()):
                if entry.name.startswith(PRODUCT_OVERLAY_REMOVE_PREFIXES):
                    rel = f"overlay/{entry.name}"
                    ok, freed = remove_tree_or_file(mount_dir, rel)
                    if ok:
                        removed_items.append(rel)
                        bytes_freed += freed
        prop_path = mount_dir / "etc/build.prop"
        if prop_path.is_file():
            updated = update_build_prop_text(
                prop_path.read_text(encoding="utf-8", errors="replace"),
                PRODUCT_PROP_OVERRIDES,
            )
            write_in_place(prop_path, updated.encode("utf-8"))

    elif partition == "system_ext":
        for rel in SYSTEM_EXT_REMOVE_PATHS:
            ok, freed = remove_tree_or_file(mount_dir, rel)
            if ok:
                removed_items.append(rel)
                bytes_freed += freed
        prop_path = mount_dir / "etc/build.prop"
        if prop_path.is_file():
            updated = update_build_prop_text(
                prop_path.read_text(encoding="utf-8", errors="replace"),
                SYSTEM_EXT_PROP_OVERRIDES,
            )
            write_in_place(prop_path, updated.encode("utf-8"))

    elif partition == "system":
        for rel in SYSTEM_REMOVE_PATHS:
            ok, freed = remove_tree_or_file(mount_dir, rel)
            if ok:
                removed_items.append(rel)
                bytes_freed += freed
        prop_path = mount_dir / "system/build.prop"
        if prop_path.is_file():
            updated = update_build_prop_text(
                prop_path.read_text(encoding="utf-8", errors="replace"),
                SYSTEM_PROP_OVERRIDES,
            )
            write_in_place(prop_path, updated.encode("utf-8"))
        # Write init.lunaris.minimal.rc and set security.selinux context (u:object_r:system_file:s0)
        init_dir = mount_dir / "system/etc/init"
        if init_dir.is_dir():
            rc_path = init_dir / "init.lunaris.minimal.rc"
            rc_path.write_bytes(MINIMAL_INIT_RC_CONTENT.encode("utf-8"))
            os.chmod(rc_path, 0o644)
            selinux_ctx = b"u:object_r:system_file:s0\x00"
            for candidate in sorted(init_dir.glob("*.rc")):
                if candidate == rc_path:
                    continue
                if hasattr(os, "getxattr"):
                    try:
                        selinux_ctx = os.getxattr(candidate, "security.selinux")
                        break
                    except OSError:
                        pass
            if hasattr(os, "setxattr"):
                try:
                    os.setxattr(rc_path, "security.selinux", selinux_ctx)
                except OSError:
                    pass

    elif partition == "vendor":
        for rel in VENDOR_REMOVE_PATHS:
            ok, freed = remove_tree_or_file(mount_dir, rel)
            if ok:
                removed_items.append(rel)
                bytes_freed += freed
        manifest_path = mount_dir / "etc/vintf/manifest.xml"
        removed_hals: list[str] = []
        if manifest_path.is_file():
            updated_xml, removed_hals = strip_vintf_manifest_hals(
                manifest_path.read_text(encoding="utf-8", errors="replace"),
                VENDOR_MANIFEST_REMOVE_HALS,
            )
            write_in_place(manifest_path, updated_xml.encode("utf-8"))
        prop_path = mount_dir / "build.prop"
        if prop_path.is_file():
            updated = update_build_prop_text(
                prop_path.read_text(encoding="utf-8", errors="replace"),
                VENDOR_PROP_OVERRIDES,
            )
            write_in_place(prop_path, updated.encode("utf-8"))
        return {
            "partition": partition,
            "removed_count": len(removed_items),
            "removed_items": removed_items,
            "removed_vintf_hals": removed_hals,
            "bytes_freed": bytes_freed,
        }
    else:
        raise ValueError(f"unsupported partition for profile: {partition}")

    return {
        "partition": partition,
        "removed_count": len(removed_items),
        "removed_items": removed_items,
        "bytes_freed": bytes_freed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    apply_p = sub.add_parser("apply", help="Apply profile to a mounted partition directory")
    apply_p.add_argument("--partition", required=True, choices=["system", "system_ext", "product", "vendor"])
    apply_p.add_argument("--mount-dir", required=True, type=Path)
    apply_p.add_argument("--report", required=True, type=Path)

    vbmeta_p = sub.add_parser("patch-vbmeta", help="Patch AVB flags=3 on a vbmeta image")
    vbmeta_p.add_argument("--image", required=True, type=Path)

    args = parser.parse_args()
    if args.cmd == "apply":
        report = apply_partition_profile(args.partition, args.mount_dir)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    elif args.cmd == "patch-vbmeta":
        patched = patch_vbmeta_flags(args.image.read_bytes(), flags=3)
        args.image.write_bytes(patched)


if __name__ == "__main__":
    main()
