# Controller prediction time correction

This separate artifact corrects a verified time-unit error in the existing
XRoboToolkit G1 Wrists 1.1.1 APK. It does **not** prove the cause of the reported
backward arm movement. It has not been installed or verified on the headset.
The recorded, robot-stopped forward/back controller test already mapped physical
forward to positive robot X; this patch does not reverse any coordinate axis.

The original `TrackingData.Get` multiplies the predicted display time by 1000
for the microsecond `predictTime` metadata field, then passes that same value to
the controller prediction functions. Their [PICO API documentation](https://pdocor.pico-interactive.com/reference/unity/xr/2.1.3/class_unity_1_1_x_r_1_1_p_x_r_1_1_p_x_r___input.html)
specifies milliseconds. Local C# source and the actual packaged ARM64 binary
both contain this behavior. The controller wrappers forward the value without
conversion. The separate Head API obtains its own prediction time.

The source-level equivalent is [pico-controller-prediction-ms.patch](../../../patches/pico-controller-prediction-ms.patch).
It preserves microsecond metadata and sends the original milliseconds to both
controllers' position and orientation APIs. Head, hand tracking, Object, Body,
pose serialization, coordinate conventions and network behavior are unchanged.

## Exact native change

The input must be the existing, hash-verified two-object G1 Wrists APK. No other
APK is accepted. The existing two-object instructions are verified and retained.

| Address | Before | After |
| --- | --- | --- |
| `0x100d3d0` | `fmul d8, d0, d1` | `fmov d8, d0` |
| `0x100d3d4` | `mov v0.16b, v8.16b` | `fmul d0, d8, d1` |

`d0` initially holds milliseconds; `d1` contains the verified constant 1000.0.
The first instruction now keeps milliseconds in `d8`, while the second produces
the same microsecond metadata in `d0`. The existing controller call subsequently
copies `d8` to its argument. Both left/right position and rotation receive that
unchanged value.

The entire compiled `TrackingData.Get` is audited for all aliases of `d8`:
its only uses are save, these two instructions, the controller argument copy,
and restore. Its low 64 bits are preserved across intervening function calls by
the [ARM64 calling convention](https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst).
No other branch consumes this register. All bytes outside the two instructions
must match the input exactly. The parser and disassembler are reused from the
existing two-object patch without modification.

`manifest.json` records input/output hashes, metadata method identities, native
addresses, exact instruction bytes, constant location, all `d8` uses and changed
byte offsets. `before.txt` and `after.txt` include the complete Get method and the
controller argument paths. The source APK and metadata remain untouched.

## Reproduction and offline validation

```bash
python3 -m venv /tmp/pico-controller-timing-analysis
/tmp/pico-controller-timing-analysis/bin/pip install -r build/pico-controller-timing/native/requirements.txt
/tmp/pico-controller-timing-analysis/bin/python build/pico-controller-timing/native/patch_timing.py \
  --apk build/pico-two-object/package/XRoboToolkit-G1-Wrists-1.1.1-two-object.apk
/tmp/pico-controller-timing-analysis/bin/python -m unittest discover \
  -s build/pico-controller-timing/native -p test_patch_timing.py -v
```

Tests interpret the actual decoded floating-point argument instructions, record
native-call arguments without executing them, and simulate caller-saved register
clobbering. They verify the old unit error, both controllers' corrected arguments,
unchanged metadata with Controller on/off, unchanged Head/Object/Body/control-flow
bytes, preservation of the two-object patch, deterministic reproduction and
rejection of unknown or already-patched inputs. They do not initialize the SDK,
send targets, package/sign an APK, publish a download or install anything.
