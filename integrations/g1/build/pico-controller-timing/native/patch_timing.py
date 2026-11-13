#!/usr/bin/env python3
"""Pinned, offline PICO controller prediction-unit correction; never installs."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
import struct
import zipfile

# Reuse the existing, reviewed metadata/ELF parser. Do not edit the tracker patch.
TWO_OBJECT = Path(__file__).resolve().parents[2] / 'pico-two-object' / 'native'
_spec = importlib.util.spec_from_file_location('g1_two_object_patch', TWO_OBJECT / 'patch_il2cpp.py')
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)

APK_SHA256 = '31e8344f6c80bb80234b48817800854005a35f35d023c642a4ce36e7f9a2de32'
LIB_SHA256 = '7132bada0316f78337ae8f93ede2962d772e0c336ca0650d38224f0f511409aa'
METADATA_SHA256 = base.METADATA_SHA256
LIB_ENTRY, METADATA_ENTRY = base.LIB_ENTRY, base.METADATA_ENTRY
GET_START, GET_END = 0x100d2d8, 0x100d864
CHANGES = (
    (0x100d3d0, '0808611e', '0840601e', 'fmul d8, d0, d1', 'fmov d8, d0',
     'Keep the original prediction time in milliseconds in callee-saved d8.'),
    (0x100d3d4, '001da84e', '0009611e', 'mov v0.16b, v8.16b', 'fmul d0, d8, d1',
     'Multiply only the metadata argument d0 by 1000, preserving microseconds on the wire.'),
)
METHODS = (
    ('get', 'Assembly-CSharp.dll', 'TrackingData', 'Get', GET_START),
    ('controllers', 'Assembly-CSharp.dll', 'TrackingData', 'GetLeftRightControllerJsonData', 0x100f284),
    ('position', 'Unity.XR.PICO.dll', 'PXR_Input', 'GetControllerPredictPosition', 0x1eb6380),
    ('rotation', 'Unity.XR.PICO.dll', 'PXR_Input', 'GetControllerPredictRotation', 0x1eb62f0),
    ('head', 'Unity.XR.PICO.dll', 'PXR_System', 'GetPredictedMainSensorStateNew', 0x1ecd374),
    ('display_time', 'PICO.TobSupport.dll', 'PXR_Enterprise', 'GetPredictedDisplayTime', 0x1bca348),
    ('body', 'Assembly-CSharp.dll', 'TrackingData', 'GetBodyTracking', 0x100f5a8),
    ('motion', 'Assembly-CSharp.dll', 'TrackingData', 'GetMotionTracking', 0x100fc94),
)


def read_apk(path):
    data = Path(path).read_bytes()
    base.require(base.digest(data) == APK_SHA256, 'unrecognized two-object APK hash')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(LIB_ENTRY), archive.read(METADATA_ENTRY)


def audit(library, metadata):
    base.require(base.digest(library) == LIB_SHA256, 'unrecognized two-object library hash')
    base.require(base.digest(metadata) == METADATA_SHA256, 'unrecognized metadata hash')
    native, md = base.NativeImage(library), base.Metadata(metadata)
    methods = {}
    for key, assembly, type_name, name, expected in METHODS:
        method = native.map_method(md.method(assembly, type_name, name))
        base.require(method['virtual_address'] == expected, 'method address differs: ' + key)
        methods[key] = method

    instructions = {i.address: base.asm(i) for i in native.instructions(GET_START, GET_END)}
    expected = {
        0x100d2dc: 'str d8, [sp, #0x130]',
        0x100d3bc: 'bl #0x1bca348',
        0x100d3c0: 'adrp x8, #0x594000',
        0x100d3c4: 'ldr d1, [x8, #0x68]',
        0x100d3d0: 'fmul d8, d0, d1',
        0x100d3d4: 'mov v0.16b, v8.16b',
        0x100d3d8: 'bl #0x1ea0a50',  # JsonData implicit conversion of d0.
        0x100d4d4: 'bl #0x1ecd374',  # Independent head prediction.
        0x100d574: 'cbz w8, #0x100d5a4',  # Controller-off branch unchanged.
        0x100d57c: 'mov v0.16b, v8.16b',
        0x100d580: 'bl #0x100f284',
        0x100d6dc: 'bl #0x100f5a8',
        0x100d780: 'bl #0x100fc94',
        0x100d854: 'ldr d8, [sp, #0x130]',
        0x100d85c: 'ret',
        0x100d860: 'bl #0xef2570',  # Existing null-reference error path.
    }
    for address, text in expected.items():
        base.require(instructions.get(address) == text, 'instruction differs at ' + hex(address))
    constant = struct.unpack_from('<d', library, native.offset(0x594068))[0]
    base.require(constant == 1000.0, 'prediction metadata multiplier differs')

    # Complete Get function audit: no other instruction consumes or changes the
    # d8/v8/s8/h8/b8 register. d8 is callee-saved under the ARM64 ABI.
    d8_uses = []
    for instruction in native.instructions(GET_START, GET_END):
        operands = instruction.op_str.replace(',', ' ').replace('[', ' ').replace(']', ' ').split()
        if any(operand.split('.')[0] in ('d8', 'v8', 's8', 'h8', 'b8') for operand in operands):
            d8_uses.append(instruction.address)
    base.require(d8_uses == [0x100d2dc, 0x100d3d0, 0x100d3d4, 0x100d57c, 0x100d854],
                 'unexpected d8 use in TrackingData.Get')

    controller = {i.address: base.asm(i) for i in native.instructions(0x100f284, 0x100f498)}
    for address, text in {
        0x100f2a4: 'mov v8.16b, v0.16b',
        0x100f2dc: 'mov w0, wzr', 0x100f2e0: 'mov v0.16b, v8.16b',
        0x100f2e8: 'bl #0x1eb6380',
        0x100f2f0: 'mov w0, wzr', 0x100f2f4: 'mov v0.16b, v8.16b',
        0x100f304: 'bl #0x1eb62f0',
        0x100f3ac: 'mov w0, #1', 0x100f3b0: 'mov v0.16b, v8.16b',
        0x100f3b8: 'bl #0x1eb6380',
        0x100f3c0: 'mov w0, #1', 0x100f3c4: 'mov v0.16b, v8.16b',
        0x100f3d4: 'bl #0x1eb62f0',
    }.items():
        base.require(controller.get(address) == text, 'controller argument path differs at ' + hex(address))
    # Confirm the existing two-object changes are already present.
    for address, _, after, _, _, _ in base.CHANGES:
        base.require(library[native.offset(address):native.offset(address)+4].hex() == after,
                     'existing two-object patch is missing')
    return native, methods, d8_uses


def analyze_and_patch(library, metadata):
    native, methods, d8_uses = audit(library, metadata)
    patched, changes = bytearray(library), []
    for address, before, after, before_asm, after_asm, purpose in CHANGES:
        offset = native.offset(address)
        base.require(library[offset:offset+4].hex() == before, 'patch preimage differs')
        replacement = bytes.fromhex(after)
        decoded = list(native.disassembler.disasm(replacement, address))
        base.require(len(decoded) == 1 and base.asm(decoded[0]) == after_asm,
                     'replacement instruction differs')
        patched[offset:offset+4] = replacement
        changes.append(dict(virtual_address=hex(address), file_offset=hex(offset),
                            original_bytes=before, patched_bytes=after,
                            original_asm=before_asm, patched_asm=after_asm, purpose=purpose))
    patched = bytes(patched)
    offsets = [i for i, (old, new) in enumerate(zip(library, patched)) if old != new]
    expected_offsets = [native.offset(address)+i for address, before, after, *_ in CHANGES
                        for i, (old, new) in enumerate(zip(bytes.fromhex(before), bytes.fromhex(after)))
                        if old != new]
    base.require(offsets == expected_offsets, 'unexpected changed bytes')
    report = dict(
        schema='pico-controller-prediction-ms-native-v1', input_apk_sha256=APK_SHA256,
        input_library_sha256=LIB_SHA256, metadata_sha256=METADATA_SHA256,
        patched_library_sha256=base.digest(patched), library_entry=LIB_ENTRY,
        methods=methods, get_function_range=[hex(GET_START), hex(GET_END)],
        original_d8_uses=[hex(a) for a in d8_uses],
        multiplier=dict(virtual_address='0x594068', value=1000.0), changes=changes,
        modified_byte_count=len(offsets), modified_byte_offsets=[hex(i) for i in offsets],
        preserved_two_object_patch=True, metadata_prediction_unit='microseconds',
        controller_prediction_unit='milliseconds', apk_signed=False, apk_installed=False,
        headset_runtime_verified=False, reverse_mapping_root_cause_proven=False,
        unchanged='All other library bytes, metadata, native SDK libraries, source APK, Head/Object/Body branches and two-object patch remain unchanged.',
        sources=[
            'https://github.com/XR-Robotics/XRoboToolkit-Unity-Client',
            'https://pdocor.pico-interactive.com/reference/unity/xr/2.1.3/class_unity_1_1_x_r_1_1_p_x_r_1_1_p_x_r___input.html',
            'https://github.com/ARM-software/abi-aa/blob/main/aapcs64/aapcs64.rst',
        ])
    return patched, report


def listing(library):
    native = base.NativeImage(library)
    lines = []
    for label, start, end in [
        ('TrackingData.Get (complete)', GET_START, GET_END),
        ('TrackingData.GetLeftRightControllerJsonData', 0x100f284, 0x100f498),
        ('PXR_Input.GetControllerPredictRotation/Position', 0x1eb62f0, 0x1eb6410),
    ]:
        lines.append('\n' + label)
        for i in native.instructions(start, end):
            lines.append(f'{i.address:08x}  file={native.offset(i.address):08x}  {i.bytes.hex()}  {base.asm(i)}')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    library, metadata = read_apk(args.apk)
    patched, report = analyze_and_patch(library, metadata)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'libil2cpp.so').write_bytes(patched)
    (args.output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    (args.output / 'before.txt').write_text(listing(library))
    (args.output / 'after.txt').write_text(listing(patched))
    base.require(base.digest(args.apk.read_bytes()) == APK_SHA256, 'input APK changed during analysis')
    print(json.dumps(dict(patched_library=str(args.output / 'libil2cpp.so'),
                         sha256=report['patched_library_sha256'],
                         modified_bytes=report['modified_byte_count'])))


if __name__ == '__main__':
    main()
