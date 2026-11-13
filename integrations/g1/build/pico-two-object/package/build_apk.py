#!/usr/bin/env python3
"""Package a reviewed two-Tracker native patch as an independent Android app.

Generated files remain next to this script. Never invokes adb, installs an app,
starts hardware, accepts SDK licences, or changes the upstream APK.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "tools"
ANDROID_TOOLS = TOOLS / "android-15"
APKTOOL = TOOLS / "apktool_3.0.3.jar"
APKSIGNER = ANDROID_TOOLS / "lib" / "apksigner.jar"
ANDROID = "http://schemas.android.com/apk/res/android"
A = "{" + ANDROID + "}"
OLD_PACKAGE = "com.xrobotoolkit.client"
PACKAGE = "com.xrobotoolkit.client.g1wrists"
LABEL = "XRoboToolkit G1 Wrists"
VERSION_CODE = 2
VERSION_NAME = "1.1.1-g1wrists.1"
APK_SHA256 = "6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261"
LIB_SHA256 = "e57bacfe26b69cffb19445ee0b216455029f1bd3e2d722c501802bee1f020855"
ORIGINAL_CERT_SHA256 = "468b842eb1ce2b3023a73068f0862ac13bd4c6760c32c963dc191b4f3b0dc851"
LIB_PATH = "lib/arm64-v8a/libil2cpp.so"
OUTPUT = HERE / "XRoboToolkit-G1-Wrists-1.1.1-two-object.apk"
ET.register_namespace("android", ANDROID)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(command: list[str | Path], log: Path, *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True, env=env)
    output = result.stdout + result.stderr
    log.write_text(output)
    if result.returncode:
        raise RuntimeError(f"{Path(str(command[0])).name} failed ({result.returncode}); see {log}\n{output[-4000:]}")
    return output


def rename_scoped(value: str) -> str:
    if value == OLD_PACKAGE or value.startswith(OLD_PACKAGE + "."):
        return PACKAGE + value[len(OLD_PACKAGE):]
    return value


def update_manifest(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    if root.get("package") != OLD_PACKAGE:
        raise ValueError("Unexpected original Android package")
    original_classes = []
    authorities = []
    class_attributes = {
        "application": ("name", "backupAgent", "manageSpaceActivity", "appComponentFactory"),
        "activity": ("name", "parentActivityName"),
        "activity-alias": ("name", "targetActivity", "parentActivityName"),
        "service": ("name",), "receiver": ("name",), "provider": ("name",),
        "instrumentation": ("name",),
    }
    for element in root.iter():
        # Component Java namespaces must survive the application ID change.
        for key in class_attributes.get(element.tag, ()):
            value = element.get(A + key)
            if value and not value.startswith("@"):
                if value.startswith("."):
                    value = OLD_PACKAGE + value
                elif "." not in value:
                    value = OLD_PACKAGE + "." + value
                element.set(A + key, value)
                original_classes.append(value)
        for key in ("authorities", "permission", "readPermission", "writePermission", "targetPackage", "sharedUserId"):
            value = element.get(A + key)
            if value:
                changed = ";".join(rename_scoped(part) for part in value.split(";"))
                element.set(A + key, changed)
                if key == "authorities":
                    authorities.append({"original": value, "packaged": changed})
        if element.tag in {"permission", "permission-group", "permission-tree", "uses-permission", "uses-permission-sdk-23"}:
            value = element.get(A + "name")
            if value:
                element.set(A + "name", rename_scoped(value))
    root.set("package", PACKAGE)
    root.set(A + "versionCode", str(VERSION_CODE))
    root.set(A + "versionName", VERSION_NAME)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return {"authorities": authorities, "java_component_names": original_classes,
            "extract_native_libs": root.find("application").get(A + "extractNativeLibs")}


def update_resources(decoded: Path) -> int:
    count = 0
    for path in decoded.glob("res/values*/strings.xml"):
        tree = ET.parse(path)
        changed = False
        for element in tree.getroot():
            if element.tag == "string" and element.get("name") == "app_name":
                element.text = LABEL
                changed = True
                count += 1
        if changed:
            tree.write(path, encoding="utf-8", xml_declaration=True)
    if count == 0:
        raise ValueError("No application label resource found")
    path = decoded / "apktool.yml"
    config = path.read_text()
    config, code_count = re.subn(r"(?m)^  versionCode: .*$", f"  versionCode: {VERSION_CODE}", config)
    config, name_count = re.subn(r"(?m)^  versionName: .*$", f"  versionName: {VERSION_NAME}", config)
    if code_count != 1 or name_count != 1:
        raise ValueError("Unexpected apktool version metadata")
    path.write_text(config)
    return count


def validate_patch(original: bytes, patched: bytes, proof: dict) -> list[int]:
    if proof.get("original_apk_sha256") != APK_SHA256:
        raise ValueError("Native proof does not match the pinned original APK")
    if sha256(original) != LIB_SHA256 or proof.get("original_library_sha256") != LIB_SHA256:
        raise ValueError("Unexpected original libil2cpp.so")
    if len(original) != len(patched) or sha256(patched) != proof.get("patched_library_sha256"):
        raise ValueError("Native patch does not match its reviewed proof")
    declared = set()
    changes = proof.get("changes", [])
    if not changes:
        raise ValueError("Native proof contains no declared changes")
    for change in changes:
        offset = change["file_offset"]
        if isinstance(offset, str):
            offset = int(offset, 0)
        before = bytes.fromhex(change["original_bytes"])
        after = bytes.fromhex(change["patched_bytes"])
        if not before or len(before) != len(after) or offset < 0 or offset + len(before) > len(original):
            raise ValueError("Invalid declared native patch extent")
        if original[offset:offset + len(before)] != before or patched[offset:offset + len(after)] != after:
            raise ValueError("Native patch bytes disagree with the proof")
        region = set(range(offset, offset + len(before)))
        if declared.intersection(region):
            raise ValueError("Native patch regions overlap")
        declared.update(region)
    actual = [i for i, (left, right) in enumerate(zip(original, patched)) if left != right]
    if not actual or not set(actual).issubset(declared):
        raise ValueError("Native patch has undeclared bytes or is empty")
    return actual


def preserve_zip_methods(original_apk: Path, rebuilt: Path, output: Path) -> dict:
    """Keep original compressed-library and raw Unity data storage semantics."""
    report = {}
    with zipfile.ZipFile(original_apk) as original, zipfile.ZipFile(rebuilt) as source, zipfile.ZipFile(output, "w") as target:
        original_info = {item.filename: item for item in original.infolist()}
        for info in source.infolist():
            if re.fullmatch(r"META-INF/[^/]+\.(SF|RSA|DSA|EC)", info.filename, re.IGNORECASE) or info.filename.upper() == "META-INF/MANIFEST.MF":
                continue
            copied = copy.copy(info)
            if info.filename in original_info:
                copied.compress_type = original_info[info.filename].compress_type
                copied.date_time = original_info[info.filename].date_time
            else:
                copied.date_time = (2026, 1, 1, 0, 0, 0)
            copied.extra = b""  # zipalign will construct fresh alignment padding.
            copied.comment = b""
            target.writestr(copied, source.read(info), compresslevel=9)
            if info.filename.startswith("lib/") or info.filename in {"resources.arsc", "assets/bin/Data/data.unity3d"}:
                report[info.filename] = copied.compress_type
    return report


def verify_payload(original_apk: Path, final_apk: Path, patched: bytes) -> dict:
    preserved = []
    with zipfile.ZipFile(original_apk) as original, zipfile.ZipFile(final_apk) as final:
        if final.testzip() is not None:
            raise ValueError("APK ZIP integrity check failed")
        payload_names = {name for name in original.namelist() if name.startswith(("assets/", "lib/")) or re.fullmatch(r"classes\d*\.dex", name)}
        final_payload_names = {name for name in final.namelist() if name.startswith(("assets/", "lib/")) or re.fullmatch(r"classes\d*\.dex", name)}
        if payload_names != final_payload_names:
            raise ValueError("APK asset, library, or DEX entry set changed")
        for name in sorted(payload_names):
            expected = patched if name == LIB_PATH else original.read(name)
            if final.read(name) != expected:
                raise ValueError(f"Unexpected payload modification: {name}")
            if final.getinfo(name).compress_type != original.getinfo(name).compress_type:
                raise ValueError(f"Unexpected payload compression change: {name}")
            if name != LIB_PATH:
                preserved.append(name)
        if final.getinfo("resources.arsc").compress_type != zipfile.ZIP_STORED:
            raise ValueError("resources.arsc must be uncompressed")
    return {"unchanged_payload_entries": preserved, "unchanged_payload_count": len(preserved),
            "patched_library_sha256": sha256(patched), "zip_integrity": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-apk", type=Path, default=Path.home() / ".cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk")
    parser.add_argument("--patched-lib", type=Path, default=HERE.parent / "native/libil2cpp.so")
    parser.add_argument("--patch-manifest", type=Path, default=HERE.parent / "native/manifest.json")
    args = parser.parse_args()
    key_env = os.environ.copy()
    if not key_env.get("G1_WRISTS_DEBUG_STOREPASS"):
        parser.error("Set G1_WRISTS_DEBUG_STOREPASS to the password for the local signing keystore "
                     "before packaging; for a new keystore, supply a new password. Do not commit it.")
    original_apk = args.original_apk.resolve()
    if sha256(original_apk.read_bytes()) != APK_SHA256:
        raise ValueError("The original APK does not match the pinned official release")
    proof = json.loads(args.patch_manifest.read_text())
    patched = args.patched_lib.read_bytes()
    with zipfile.ZipFile(original_apk) as source:
        original_library = source.read(LIB_PATH)
    changed_offsets = validate_patch(original_library, patched, proof)

    from setup_tools import DOWNLOADS, digest
    for item in DOWNLOADS:
        if digest(TOOLS / item["name"], item["algorithm"]) != item["checksum"]:
            raise ValueError("Packaging tool checksum mismatch; run setup_tools.py")
    logs = HERE / "logs"
    logs.mkdir(exist_ok=True)
    framework = TOOLS / "framework"
    framework.mkdir(exist_ok=True)
    java_signer = ["java", "--enable-native-access=ALL-UNNAMED", "-jar", APKSIGNER]
    original_signature = run(java_signer + ["verify", "--verbose", "--print-certs", original_apk], logs / "original-signature.txt")
    if ORIGINAL_CERT_SHA256 not in original_signature:
        raise ValueError("Original signing certificate changed")

    keys = HERE / "keys"
    keys.mkdir(mode=0o700, exist_ok=True)
    keys.chmod(0o700)
    key = keys / "g1-wrists-local.p12"
    # A local development key, unrelated to the official publisher.
    if not key.exists():
        run(["keytool", "-genkeypair", "-keystore", key, "-storetype", "PKCS12", "-alias", "g1wrists",
             "-storepass:env", "G1_WRISTS_DEBUG_STOREPASS", "-keypass:env", "G1_WRISTS_DEBUG_STOREPASS",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=G1 Wrists Local Development,OU=Local,O=Local Development"], logs / "key-generation.txt", env=key_env)
    key.chmod(0o600)

    work_parent = HERE / "work"
    work_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=work_parent) as temporary:
        work = Path(temporary)
        decoded = work / "decoded"
        run(["java", "-jar", APKTOOL, "d", "-s", "-p", framework, "-o", decoded, original_apk], logs / "decode.txt")
        manifest_changes = update_manifest(decoded / "AndroidManifest.xml")
        manifest_changes["label_resources_updated"] = update_resources(decoded)
        shutil.copyfile(args.patched_lib, decoded / LIB_PATH)
        shutil.copyfile(decoded / "AndroidManifest.xml", logs / "packaged-manifest.xml")
        rebuilt = work / "rebuilt.apk"
        run(["java", "-jar", APKTOOL, "b", "--aapt", ANDROID_TOOLS / "aapt2", "-p", framework, "-o", rebuilt, decoded], logs / "rebuild.txt")
        normalized = work / "normalized.apk"
        methods = preserve_zip_methods(original_apk, rebuilt, normalized)
        aligned = work / "aligned.apk"
        run([ANDROID_TOOLS / "zipalign", "-P", "16", "-f", "-v", "4", normalized, aligned], logs / "alignment.txt")
        signed = work / "signed.apk"
        run(java_signer + ["sign", "--ks", key, "--ks-key-alias", "g1wrists", "--ks-pass", "env:G1_WRISTS_DEBUG_STOREPASS",
            "--key-pass", "env:G1_WRISTS_DEBUG_STOREPASS", "--v4-signing-enabled", "false", "--out", signed, aligned],
            logs / "signing.txt", env=key_env)
        signature = run(java_signer + ["verify", "--verbose", "--print-certs", signed], logs / "packaged-signature.txt")
        if ORIGINAL_CERT_SHA256 in signature:
            raise ValueError("Independent package must not claim the original signing identity")
        run([ANDROID_TOOLS / "zipalign", "-c", "-P", "16", "-v", "4", signed], logs / "alignment-check.txt")
        badging = run([ANDROID_TOOLS / "aapt2", "dump", "badging", signed], logs / "packaged-badging.txt")
        manifest_dump = run([ANDROID_TOOLS / "aapt2", "dump", "xmltree", signed, "--file", "AndroidManifest.xml"], logs / "packaged-manifest-tree.txt")
        if f"package: name='{PACKAGE}' versionCode='{VERSION_CODE}' versionName='{VERSION_NAME}'" not in badging:
            raise ValueError("Final compiled package or version is wrong")
        if f"application-label:'{LABEL}'" not in badging:
            raise ValueError("Final compiled application label is wrong")
        if 'package="' + OLD_PACKAGE + '"' in manifest_dump:
            raise ValueError("Final manifest still has original package ID")
        payload = verify_payload(original_apk, signed, patched)
        shutil.copyfile(signed, OUTPUT)

    if sha256(original_apk.read_bytes()) != APK_SHA256:
        raise ValueError("Original APK changed during packaging")
    certificate_match = re.search(r"Signer #1 certificate SHA-256 digest: ([a-f0-9]+)", signature)
    report = {
        "schema": "g1-wrists-apk-package-v1", "output_apk": str(OUTPUT),
        "output_apk_sha256": sha256(OUTPUT.read_bytes()), "output_size": OUTPUT.stat().st_size,
        "package": PACKAGE, "application_label": LABEL,
        "version_code": VERSION_CODE, "version_name": VERSION_NAME,
        "original_apk": str(original_apk), "original_apk_sha256": APK_SHA256,
        "original_certificate_sha256": ORIGINAL_CERT_SHA256,
        "local_certificate_sha256": certificate_match.group(1) if certificate_match else None,
        "patch_manifest": str(args.patch_manifest.resolve()), "patch_manifest_sha256": sha256(args.patch_manifest.read_bytes()),
        "native_changed_byte_offsets": changed_offsets, "manifest_changes": manifest_changes,
        "zip_storage_methods": methods, "payload_verification": payload,
        "signature_verified": True, "zipalign_verified": True,
        "original_apk_unchanged": True, "installed_on_headset": False, "headset_runtime_verified": False,
        "notes": ["Locally signed independent package; the original app is not overwritten.",
                  "Original compressed native libraries and extractNativeLibs=true preserved.",
                  "Alignment check is for ZIP storage; not a claim of 16 KB ELF compatibility.",
                  "No entitlement, tracking quality, calibration, or licence checks bypassed."],
    }
    (HERE / "package-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"apk": str(OUTPUT), "sha256": report["output_apk_sha256"],
                      "package": PACKAGE, "installed_on_headset": False}, indent=2))


if __name__ == "__main__":
    main()
