#!/usr/bin/env python3
"""Exactly verified IL2CPP patch for official XRoboToolkit PICO 1.1.1 ARM64.

No APK rewriting/signing/installing; output is a separate library and evidence.
Metadata layouts follow Perfare/Il2CppDumper MetadataClass.cs/Il2CppClass.cs.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile

from capstone import Cs, CS_ARCH_ARM64, CS_MODE_ARM
from elftools.elf.elffile import ELFFile

APK_SHA256 = '6b2bb282405673d24abcb1980e3478b8f1052e90f7207b1f24cc56a59f8d8261'
LIB_SHA256 = 'e57bacfe26b69cffb19445ee0b216455029f1bd3e2d722c501802bee1f020855'
METADATA_SHA256 = 'bfddbb00a313d402693a2101fc833627150a61ad302999f6c42d05470d891c44'
LIB_ENTRY = 'lib/arm64-v8a/libil2cpp.so'
METADATA_ENTRY = 'assets/bin/Data/Managed/Metadata/global-metadata.dat'
CHANGES = (
    (0x10025bc, '61000054', '03000014', 'b.ne #0x10025c8', 'b #0x10025c8',
     'Object selection always calls the SDK check, including when already in MotionTracking mode.'),
    (0x10025cc, '21008052', '41008052', 'mov w1, #1', 'mov w1, #2',
     'Object mode passes MotionTrackerNum.TWO instead of ONE as argument 2.'),
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Metadata:
    def __init__(self, data):
        require(struct.unpack_from('<II', data) == (0xfab11baf, 29), 'metadata must be original version 29')
        self.data = data
        self.strings = self.u32(24)
        self.methods = self.u32(48)
        self.types = self.u32(160)
        self.images = self.u32(168)

    def u32(self, offset):
        return struct.unpack_from('<I', self.data, offset)[0]

    def u16(self, offset):
        return struct.unpack_from('<H', self.data, offset)[0]

    def string(self, index):
        start = self.strings+index
        return self.data[start:self.data.index(0, start)].decode('utf-8')

    def type(self, assembly, name):
        matches = []
        for image in range(self.images, self.images+self.u32(172), 40):
            if self.string(self.u32(image)) != assembly:
                continue
            first, count = struct.unpack_from('<iI', self.data, image+8)
            for index in range(first, first+count):
                offset = self.types+88*index
                if self.string(self.u32(offset)) == name:
                    matches.append((index, offset))
        require(len(matches) == 1, 'metadata type must be unique: '+assembly+'/'+name)
        return matches[0]

    def method(self, assembly, type_name, method_name):
        type_index, offset = self.type(assembly, type_name)
        first, count = self.u32(offset+36), self.u16(offset+64)
        matches = []
        for index in range(first, first+count):
            method = self.methods+32*index
            if self.string(self.u32(method)) == method_name:
                require(self.u32(method+4) == type_index, 'method declaring type mismatch')
                matches.append(dict(assembly=assembly, type=type_name, method=method_name,
                    metadata_index=index, declaring_type_index=type_index,
                    token=self.u32(method+20), parameter_count=self.u16(method+30)))
        require(len(matches) == 1, 'metadata method must be unique: '+method_name)
        return matches[0]

    def enum(self, assembly, type_name):
        _, offset = self.type(assembly, type_name)
        first, count = self.u32(offset+32), self.u16(offset+68)
        defaults = {struct.unpack_from('<iii', self.data, off)[0]:
                    struct.unpack_from('<iii', self.data, off)[2]
                    for off in range(self.u32(64), self.u32(64)+self.u32(68), 12)}
        result = {}
        for index in range(first, first+count):
            name = self.string(self.u32(self.u32(96)+12*index))
            if name == 'value__':
                continue
            require(index in defaults, 'enum literal has no default value')
            encoded = self.data[self.u32(72)+defaults[index]]
            # IL2CPP v29 I4 constants use compressed signed integers. These
            # specific enums contain only the one-byte nonnegative values 0..3.
            require(encoded < 0x80 and encoded % 2 == 0, 'unsupported enum constant encoding')
            result[name] = encoded >> 1
        return result


class NativeImage:
    def __init__(self, data):
        self.data = data
        self.elf = ELFFile(io.BytesIO(data))
        require(self.elf.elfclass == 64 and self.elf.little_endian and
                self.elf['e_machine'] == 'EM_AARCH64', 'expected little-endian ARM64 ELF')
        self.loads = [segment for segment in self.elf.iter_segments() if segment['p_type'] == 'PT_LOAD']
        self.relocations = {item['r_offset']: item['r_addend'] for item in
            self.elf.get_section_by_name('.rela.dyn').iter_relocations() if item['r_info_type'] == 1027}
        self.disassembler = Cs(CS_ARCH_ARM64, CS_MODE_ARM)

    def offset(self, va):
        for segment in self.loads:
            if segment['p_vaddr'] <= va < segment['p_vaddr']+segment['p_filesz']:
                return va-segment['p_vaddr']+segment['p_offset']
        raise ValueError('virtual address outside file-backed PT_LOAD: '+hex(va))

    def address(self, offset):
        for segment in self.loads:
            if segment['p_offset'] <= offset < segment['p_offset']+segment['p_filesz']:
                return offset-segment['p_offset']+segment['p_vaddr']
        raise ValueError('file offset outside PT_LOAD')

    def pointer(self, va):
        if va in self.relocations:
            return self.relocations[va]
        return struct.unpack_from('<Q', self.data, self.offset(va))[0]

    def map_method(self, method):
        name = method['assembly'].encode()+b'\0'
        require(self.data.count(name) == 1, 'module name string is not unique')
        name_va = self.address(self.data.index(name))
        candidates = [va for va, value in self.relocations.items() if value == name_va]
        require(len(candidates) == 1, 'codegen module is not uniquely mapped')
        module = candidates[0]
        count, table = self.pointer(module+8), self.pointer(module+16)
        index = (method['token'] & 0xffffff)-1
        require(0 <= index < count, 'method token outside codegen table')
        va = self.pointer(table+8*index)
        return dict(method, codegen_module_va=module, method_pointer_count=count,
                    method_table_va=table, method_table_index=index,
                    virtual_address=va, file_offset=self.offset(va))

    def instructions(self, start, end):
        return list(self.disassembler.disasm(self.data[self.offset(start):self.offset(start)+end-start], start))


def asm(instruction):
    return (instruction.mnemonic+' '+instruction.op_str).strip()


def analyze_and_patch(library, metadata):
    require(digest(library) == LIB_SHA256, 'unrecognized original library hash')
    require(digest(metadata) == METADATA_SHA256, 'unrecognized original metadata hash')
    md, native = Metadata(metadata), NativeImage(library)
    enum_values = {
        'TrackingData.TrackingType': md.enum('Assembly-CSharp.dll', 'TrackingType'),
        'MotionTrackerMode': md.enum('Unity.XR.PICO.dll', 'MotionTrackerMode'),
        'MotionTrackerNum': md.enum('Unity.XR.PICO.dll', 'MotionTrackerNum'),
    }
    require(enum_values == {'TrackingData.TrackingType': {'None': 0, 'Body': 1, 'Motion': 2},
        'MotionTrackerMode': {'BodyTracking': 0, 'MotionTracking': 1},
        'MotionTrackerNum': {'NONE': 0, 'ONE': 1, 'TWO': 2, 'THREE': 3}}, 'enum values differ')
    mapped = {}
    for key, assembly, type_name, method_name in (
        ('ui', 'Assembly-CSharp.dll', 'UIOperate', 'OnBodyModeDrop'),
        ('sdk', 'Unity.XR.PICO.dll', 'PXR_MotionTracking', 'CheckMotionTrackerModeAndNumber'),
        ('mode_getter', 'Unity.XR.PICO.dll', 'PXR_MotionTracking', 'GetMotionTrackerMode'),
        ('plugin', 'Unity.XR.PICO.dll', 'MotionTracking', 'UPxr_CheckMotionTrackerModeAndNumber'),
        ('native_import', 'Unity.XR.PICO.dll', 'MotionTracking', 'Pxr_CheckMotionTrackerModeAndNumber')):
        mapped[key] = native.map_method(md.method(assembly, type_name, method_name))
    require(mapped['ui']['token'] == 0x60001a0 and mapped['ui']['parameter_count'] == 1 and
            mapped['ui']['virtual_address'] == 0x1002534, 'unexpected UI method mapping')
    require(mapped['sdk']['virtual_address'] == 0x1ec8ac8 and mapped['sdk']['parameter_count'] == 2,
            'unexpected SDK method mapping')
    instructions = {i.address: asm(i) for i in native.instructions(0x1002534, 0x1002720)}
    expected = {
        0x1002588: 'bl #'+hex(mapped['mode_getter']['virtual_address']),
        0x100258c: 'cmp w21, #2', 0x1002590: 'b.eq #0x10025b8',
        0x1002594: 'cmp w21, #1', 0x1002598: 'b.ne #0x100268c',
        0x100259c: 'cbz w0, #0x10025ec', 0x10025a0: 'mov w1, #2',
        0x10025a4: 'mov w0, wzr', 0x10025ac: 'bl #0x1ec8ac8',
        0x10025b8: 'cmp w0, #1', 0x10025bc: 'b.ne #0x10025c8',
        0x10025c0: 'mov w21, wzr', 0x10025c4: 'b #0x10025dc',
        0x10025c8: 'mov w0, #1', 0x10025cc: 'mov w1, #1',
        0x10025d0: 'mov x2, xzr', 0x10025d4: 'bl #0x1ec8ac8',
        0x10025d8: 'mov w21, w0',
    }
    for address, text in expected.items():
        require(instructions[address] == text, 'unexpected surrounding instruction at '+hex(address))
    wrapper = [asm(i) for i in native.instructions(0x1ec8ac8, 0x1ec8ad0)]
    require(wrapper == ['mov x2, xzr', 'b #'+hex(mapped['plugin']['virtual_address'])],
            'SDK wrapper does not pass mode/count unchanged to plugin')
    plugin = {i.address: asm(i) for i in native.instructions(0x1ef8b08, 0x1ef8c88)}
    for address, text in {0x1ef8b2c: 'mov w19, w1', 0x1ef8b30: 'mov w20, w0',
            0x1ef8bd4: 'cmp w20, #0', 0x1ef8bd8: 'csel w19, wzr, w19, eq',
            0x1ef8bdc: 'mov w0, w20', 0x1ef8be0: 'mov w1, w19',
            0x1ef8be4: 'bl #'+hex(mapped['native_import']['virtual_address'])}.items():
        require(plugin[address] == text, 'native-import argument path differs')
    patched, changes = bytearray(library), []
    for address, before, after, before_asm, after_asm, reason in CHANGES:
        offset = native.offset(address)
        require(library[offset:offset+4].hex() == before, 'patch preimage differs')
        changed = bytes.fromhex(after)
        decoded = list(native.disassembler.disasm(changed, address))
        require(len(decoded) == 1 and asm(decoded[0]) == after_asm, 'replacement instruction differs')
        patched[offset:offset+4] = changed
        changes.append(dict(virtual_address=hex(address), file_offset=hex(offset),
            original_bytes=before, patched_bytes=after, original_asm=before_asm,
            patched_asm=after_asm, purpose=reason))
    patched = bytes(patched)
    changed_offsets = [i for i, (before, after) in enumerate(zip(library, patched)) if before != after]
    require(changed_offsets == [0x10015bc, 0x10015bf, 0x10015cc], 'unexpected byte changes')
    report = dict(schema='pico-two-object-native-v1', original_apk_sha256=APK_SHA256,
        original_library_sha256=digest(library), original_metadata_sha256=digest(metadata),
        patched_library_sha256=digest(patched), library_entry=LIB_ENTRY,
        metadata_version=29, enums=enum_values, methods=mapped, changes=changes,
        modified_byte_count=len(changed_offsets), modified_byte_offsets=[hex(i) for i in changed_offsets],
        apk_signed=False, apk_installed=False, headset_runtime_verified=False,
        unchanged='All other library bytes, metadata, native SDK libraries, and original APK remain unchanged.',
        sources=[
            'https://github.com/XR-Robotics/XRoboToolkit-Unity-Client',
            'https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Il2Cpp/MetadataClass.cs',
            'https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Il2Cpp/Il2CppClass.cs',
            'https://github.com/Perfare/Il2CppDumper/blob/master/Il2CppDumper/Utils/Il2CppExecutor.cs'])
    return patched, report


def listing(library):
    native = NativeImage(library)
    text = []
    for label, start, end in [('UIOperate.OnBodyModeDrop', 0x1002534, 0x1002720),
            ('PXR_MotionTracking.CheckMotionTrackerModeAndNumber', 0x1ec8ac8, 0x1ec8ad0),
            ('MotionTracking.UPxr_CheckMotionTrackerModeAndNumber', 0x1ef8b08, 0x1ef8c88)]:
        text.append('\n'+label)
        for instruction in native.instructions(start, end):
            text.append(f'{instruction.address:08x}  file={native.offset(instruction.address):08x}  '
                        f'{instruction.bytes.hex()}  {asm(instruction)}')
    return '\n'.join(text)+'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    original = args.apk.read_bytes()
    require(digest(original) == APK_SHA256, 'unrecognized original APK hash')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        library, metadata = archive.read(LIB_ENTRY), archive.read(METADATA_ENTRY)
    patched, report = analyze_and_patch(library, metadata)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/'libil2cpp.so').write_bytes(patched)
    (args.output/'manifest.json').write_text(json.dumps(report, indent=2)+'\n')
    (args.output/'before.txt').write_text(listing(library))
    (args.output/'after.txt').write_text(listing(patched))
    require(digest(args.apk.read_bytes()) == APK_SHA256, 'original APK changed during analysis')
    print(json.dumps(dict(patched_library=str(args.output/'libil2cpp.so'),
                          sha256=report['patched_library_sha256'], modified_bytes=3)))


if __name__ == '__main__':
    main()
