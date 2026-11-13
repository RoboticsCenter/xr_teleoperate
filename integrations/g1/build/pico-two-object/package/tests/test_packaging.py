from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_DIR))
import build_apk as builder


class PackagingTests(unittest.TestCase):
    def test_application_id_change_keeps_classes_and_external_permissions(self):
        xml = f'''<manifest xmlns:android="{builder.ANDROID}" package="{builder.OLD_PACKAGE}">
          <uses-permission android:name="com.picovr.permission.HAND_TRACKING"/>
          <permission android:name="{builder.OLD_PACKAGE}.DATA"/>
          <application android:name=".App" android:extractNativeLibs="true">
            <activity android:name="MainActivity"/>
            <provider android:name=".DataProvider" android:authorities="{builder.OLD_PACKAGE}.data;other.provider"
                android:readPermission="{builder.OLD_PACKAGE}.DATA"/>
          </application>
        </manifest>'''
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "AndroidManifest.xml"
            path.write_text(xml)
            builder.update_manifest(path)
            root = ET.parse(path).getroot()
        self.assertEqual(root.get("package"), builder.PACKAGE)
        self.assertEqual(root.find("application").get(builder.A + "name"), builder.OLD_PACKAGE + ".App")
        self.assertEqual(root.find("application/activity").get(builder.A + "name"), builder.OLD_PACKAGE + ".MainActivity")
        self.assertEqual(root.find("application/provider").get(builder.A + "authorities"), builder.PACKAGE + ".data;other.provider")
        self.assertEqual(root.find("application/provider").get(builder.A + "readPermission"), builder.PACKAGE + ".DATA")
        self.assertEqual(root.find("uses-permission").get(builder.A + "name"), "com.picovr.permission.HAND_TRACKING")

    def test_exact_native_patch_matches_published_proof(self):
        original_apk = Path.home() / ".cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk"
        proof = json.loads((PACKAGE_DIR.parent / "native/manifest.json").read_text())
        patched = (PACKAGE_DIR.parent / "native/libil2cpp.so").read_bytes()
        with zipfile.ZipFile(original_apk) as source:
            original = source.read(builder.LIB_PATH)
        offsets = builder.validate_patch(original, patched, proof)
        self.assertEqual(offsets, [int(value, 16) for value in proof["modified_byte_offsets"]])
        self.assertEqual(len(offsets), 3)

    def test_even_rehashed_native_extra_change_is_rejected(self):
        original_apk = Path.home() / ".cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk"
        proof = json.loads((PACKAGE_DIR.parent / "native/manifest.json").read_text())
        patched = bytearray((PACKAGE_DIR.parent / "native/libil2cpp.so").read_bytes())
        patched[0] ^= 1
        proof["patched_library_sha256"] = builder.sha256(patched)
        with zipfile.ZipFile(original_apk) as source:
            original = source.read(builder.LIB_PATH)
        with self.assertRaisesRegex(ValueError, "undeclared"):
            builder.validate_patch(original, patched, proof)

    def test_payload_check_rejects_unrelated_dex_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, dex in (("original.apk", b"original dex"), ("changed.apk", b"modified dex")):
                with zipfile.ZipFile(root / name, "w") as archive:
                    archive.writestr("classes.dex", dex)
                    archive.writestr("resources.arsc", b"resources")
                    archive.writestr(builder.LIB_PATH, b"library")
            with self.assertRaisesRegex(ValueError, "classes.dex"):
                builder.verify_payload(root / "original.apk", root / "changed.apk", b"library")

    def test_rebuild_restores_original_native_and_unity_storage_methods(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_methods = {builder.LIB_PATH: zipfile.ZIP_DEFLATED, "assets/bin/Data/data.unity3d": zipfile.ZIP_STORED,
                                "classes.dex": zipfile.ZIP_STORED, "resources.arsc": zipfile.ZIP_STORED}
            with zipfile.ZipFile(root / "original.apk", "w") as archive:
                for name, method in original_methods.items():
                    archive.writestr(name, name.encode(), compress_type=method)
            with zipfile.ZipFile(root / "rebuilt.apk", "w") as archive:
                for name in original_methods:
                    archive.writestr(name, name.encode(), compress_type=zipfile.ZIP_DEFLATED)
            builder.preserve_zip_methods(root / "original.apk", root / "rebuilt.apk", root / "normalized.apk")
            with zipfile.ZipFile(root / "normalized.apk") as archive:
                self.assertEqual({name: archive.getinfo(name).compress_type for name in original_methods}, original_methods)


if __name__ == "__main__":
    unittest.main()
