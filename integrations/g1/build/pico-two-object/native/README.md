# Two Object tracker native patch

This directory contains a separately produced `libil2cpp.so` for the official
XRoboToolkit PICO 1.1.1 APK. The original APK is unchanged. Packaging, signing and
headset installation are separate steps; these files do not claim headset testing.

`manifest.json` records the exact input/output SHA256 values, metadata method
tokens, CodeGenModule/method-table addresses, enum constants, two changed
instructions and three changed file bytes. `before.txt` and `after.txt` contain
the full relevant function disassembly and SDK argument forwarding path.

The IL2CPP v29 metadata maps `Assembly-CSharp.dll / UIOperate.OnBodyModeDrop`
token `0x060001a0` to virtual address `0x1002534`. The Object branch passes
`mode=1, count=1` to the method independently mapped as
`PXR_MotionTracking.CheckMotionTrackerModeAndNumber` at `0x1ec8ac8`.

Two instructions change:

| Virtual address | File offset | Original | Patched |
| --- | --- | --- | --- |
| `0x10025bc` | `0x10015bc` | `b.ne #0x10025c8` | `b #0x10025c8` |
| `0x10025cc` | `0x10015cc` | `mov w1, #1` | `mov w1, #2` |

The first forces the Object-count check even when the current mode is already
Object. The second requests TWO. Both changes match the staged C# source change.
Body/None paths, SDK-version checks, native SDK calls and SDK-error UI handling
are unchanged. No runtime, license, authentication or signature verification is
disabled by this native patch.

The metadata layouts and compressed v29 integer handling were checked against
[Il2CppDumper metadata definitions](https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Il2Cpp/MetadataClass.cs),
[CodeGenModule definitions](https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Il2Cpp/Il2CppClass.cs)
and [default-value decoding](https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Utils/Il2CppExecutor.cs).
This is a small explicit reader for the pinned original binary, not a heuristic
byte-pattern patcher. Different input hashes, token mappings, enums, surrounding
instructions or patch preimages are rejected.

Reproduce in a separate Python environment:

```bash
python3 -m venv /tmp/pico-object-analysis
/tmp/pico-object-analysis/bin/pip install -r build/pico-two-object/native/requirements.txt
/tmp/pico-object-analysis/bin/python build/pico-two-object/native/patch_il2cpp.py \
  --apk /home/jack-xiang/.cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk \
  --output /tmp/pico-object-reproduced
/tmp/pico-object-analysis/bin/python -m unittest discover \
  -s build/pico-two-object/native -p test_patch_il2cpp.py -v
```

The tests interpret the actual decoded ARM64 dropdown dispatch without executing
native code. They cover entering Object from Body, reselecting Object, unchanged
Body/None paths, unchanged error handling, deterministic output and refusal of
unrecognized inputs.
