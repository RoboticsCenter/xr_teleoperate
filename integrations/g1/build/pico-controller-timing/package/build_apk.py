#!/usr/bin/env python3
"""Build the controller-timing update using the existing audited packager.

The prior two-object APK and signing key are preserved. This creates a version
3 update with the same local application identity; it does not install it.
"""
from pathlib import Path
import hashlib
import json
import sys

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parents[1] / 'pico-two-object/package'
sys.path.insert(0, str(PREVIOUS))


def main():
    import importlib.util
    spec = importlib.util.spec_from_file_location('reviewed_pico_packager', PREVIOUS / 'build_apk.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    # Reuse the original local certificate without copying private key material.
    keys = HERE / 'keys'
    expected = PREVIOUS / 'keys'
    if not keys.exists():
        keys.symlink_to(expected, target_is_directory=True)
    if keys.resolve() != expected.resolve():
        raise ValueError('Timing update must use the existing G1 Wrists signing key')
    builder.HERE = HERE
    builder.VERSION_CODE = 3
    builder.VERSION_NAME = '1.1.1-g1wrists.2'
    builder.OUTPUT = HERE / 'XRoboToolkit-G1-Wrists-1.1.1-controller-timing.apk'
    previous = json.loads((PREVIOUS.parent / 'native/manifest.json').read_text())
    timing = json.loads((HERE.parent / 'native/manifest.json').read_text())
    previous_apk = PREVIOUS / 'XRoboToolkit-G1-Wrists-1.1.1-two-object.apk'
    if (timing['input_library_sha256'] != previous['patched_library_sha256'] or
            timing['input_apk_sha256'] != hashlib.sha256(previous_apk.read_bytes()).hexdigest()):
        raise ValueError('Timing patch does not extend the existing two-object build')
    combined = dict(previous, patched_library_sha256=timing['patched_library_sha256'],
                    changes=previous['changes'] + timing['changes'])
    proof = HERE / 'combined-native-manifest.json'
    proof.write_text(json.dumps(combined, indent=2) + '\n')
    if '--patch-manifest' not in sys.argv:
        sys.argv.extend(['--patch-manifest', str(proof)])
    builder.main()


if __name__ == '__main__':
    main()
