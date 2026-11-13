# Controller timing update candidate

`XRoboToolkit-G1-Wrists-1.1.1-controller-timing.apk` is a locally signed update
to `com.xrobotoolkit.client.g1wrists`, version `1.1.1-g1wrists.2` / code 3.
It preserves two Object trackers and corrects the controller prediction time
unit. See [the native audit](../native/README.md) for the exact change.

This candidate has **not been installed or tested on the headset**. The existing
download page and original two-object APK remain unchanged. The robot dashboard
remains in the user's working fixed-direction mode. This is not evidence that
headset-following mapping has been repaired.

SHA-256: `2b53f07a51fddd3a941c815e747e603d95829b8d60dbbb276fa75288d987c4c8`.

Packaging reused the existing local certificate; signature, version, ZIP integrity
and alignment passed. All 25 other asset/DEX/native payload entries match the
official APK exactly. Relative to that APK, the library has eight changed bytes:
three for the existing two-object feature and five for the timing correction.
The previous signed APK is preserved. `package-manifest.json` contains the
generated verification report.

To reproduce, first generate the native artifact as documented above, then set
`G1_WRISTS_DEBUG_STOREPASS` securely to the existing local keystore password and
run:

```bash
python3 build/pico-controller-timing/package/build_apk.py
```

The wrapper reuses the existing reviewed packager and Android tools. It combines
the two exact native patch proofs, retains the original application identity,
and writes outputs here. Its `keys` symlink points to the original local signing
directory; keys, passwords and generated artifacts stay outside Git. It does not
install the APK or start a service.
