"""Offline checks of actual decoded ARM64; no SDK or headset is initialized."""
from pathlib import Path
import tempfile
import unittest

import patch_timing as patch

APK = patch.TWO_OBJECT.parent / 'package' / 'XRoboToolkit-G1-Wrists-1.1.1-two-object.apk'
OUTPUT = Path(__file__).resolve().parent / 'libil2cpp.so'


def fp_register(operand):
    return int(operand.split('.')[0][1:])


def execute_fp(instruction, registers):
    """Interpret only the decoded floating register copies/multiply used here."""
    operands = instruction.op_str.split(', ')
    if instruction.mnemonic in ('mov', 'fmov') and operands[0][0] in 'dv':
        registers[fp_register(operands[0])] = registers[fp_register(operands[1])]
    elif instruction.mnemonic == 'fmul':
        registers[fp_register(operands[0])] = (registers[fp_register(operands[1])] *
                                             registers[fp_register(operands[2])])
    else:
        raise AssertionError('unexpected floating instruction: ' + patch.base.asm(instruction))


def interpreted_arguments(library, milliseconds, controllers_on=True):
    image = patch.base.NativeImage(library)
    regs = {0: milliseconds, 1: 1000.0}
    for i in image.instructions(0x100d3d0, 0x100d3d8):
        execute_fp(i, regs)
    metadata = regs[0]
    # Calls between metadata serialization and controllers may clobber every
    # caller-saved floating register, but ARM64 preserves the low 64 bits of d8.
    for reg in range(8):
        regs[reg] = -123456.0
    if not controllers_on:
        return metadata, []
    execute_fp(next(iter(image.instructions(0x100d57c, 0x100d580))), regs)
    # Interpret actual argument copies in GetLeftRightControllerJsonData. Native
    # calls are recorded, never executed. Their result registers are clobbered.
    calls, side = [], None
    for instruction in image.instructions(0x100f2a4, 0x100f3d8):
        operands = instruction.op_str.split(', ')
        if instruction.mnemonic == 'mov' and operands[0].startswith('v'):
            execute_fp(instruction, regs)
        elif instruction.mnemonic == 'mov' and operands[0] == 'w0':
            side = 0 if operands[1] == 'wzr' else int(operands[1][1:], 0)
        elif instruction.mnemonic == 'bl':
            address = int(operands[0][1:], 0)
            if address in (0x1eb6380, 0x1eb62f0):
                calls.append((side, 'position' if address == 0x1eb6380 else 'rotation', regs[0]))
            for reg in range(8):
                regs[reg] = -123456.0
    return metadata, calls


class TimingPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original, cls.metadata = patch.read_apk(APK)
        cls.patched = OUTPUT.read_bytes()

    def test_actual_original_instructions_reproduce_unit_error(self):
        metadata, calls = interpreted_arguments(self.original, 12345.125)
        self.assertEqual(metadata, 12345125.0)
        self.assertEqual([c[2] for c in calls], [12345125.0] * 4)

    def test_both_controller_position_and_rotation_receive_milliseconds(self):
        for value in (0.0, 16.625, 12345.125, 52865123.125):
            with self.subTest(milliseconds=value):
                metadata, calls = interpreted_arguments(self.patched, value)
                self.assertEqual(metadata, value * 1000)
                self.assertEqual(calls, [(0, 'position', value), (0, 'rotation', value),
                                         (1, 'position', value), (1, 'rotation', value)])

    def test_controller_off_still_serializes_identical_microsecond_metadata(self):
        for value in (16.625, 12345.125, 52865123.125):
            self.assertEqual(interpreted_arguments(self.original, value, False),
                             interpreted_arguments(self.patched, value, False))

    def test_all_head_object_body_hand_and_error_control_flow_is_unchanged(self):
        before, after = patch.base.NativeImage(self.original), patch.base.NativeImage(self.patched)
        changed = {address for address, *_ in patch.CHANGES}
        old = {i.address: bytes(i.bytes) for i in before.instructions(patch.GET_START, patch.GET_END)}
        new = {i.address: bytes(i.bytes) for i in after.instructions(patch.GET_START, patch.GET_END)}
        self.assertEqual(old.keys(), new.keys())
        self.assertEqual([a for a in old if old[a] != new[a]], sorted(changed))
        # Every condition, branch, call and return is byte-identical. Head and
        # Object methods also remain identical by the whole-library check below.
        for instruction in before.instructions(patch.GET_START, patch.GET_END):
            if instruction.mnemonic.startswith(('b', 'cb', 'tb', 'ret')):
                self.assertEqual(old[instruction.address], new[instruction.address])

    def test_two_object_changes_and_every_other_library_byte_are_preserved(self):
        image = patch.base.NativeImage(self.original)
        expected = []
        for address, before, after, *_ in patch.CHANGES:
            expected.extend(image.offset(address)+i for i, pair in enumerate(zip(bytes.fromhex(before), bytes.fromhex(after)))
                            if pair[0] != pair[1])
        actual = [i for i, pair in enumerate(zip(self.original, self.patched)) if pair[0] != pair[1]]
        self.assertEqual(actual, expected)
        self.assertEqual(len(self.original), len(self.patched))
        for address, _, after, *_ in patch.base.CHANGES:
            offset = image.offset(address)
            self.assertEqual(self.patched[offset:offset+4].hex(), after)

    def test_reproduction_and_complete_d8_use_audit(self):
        result, report = patch.analyze_and_patch(self.original, self.metadata)
        self.assertEqual(result, self.patched)
        self.assertEqual(report['original_d8_uses'],
                         ['0x100d2dc', '0x100d3d0', '0x100d3d4', '0x100d57c', '0x100d854'])
        self.assertFalse(report['headset_runtime_verified'])
        self.assertFalse(report['reverse_mapping_root_cause_proven'])

    def test_unrecognized_or_already_patched_inputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'library hash'):
            patch.analyze_and_patch(self.patched, self.metadata)
        with self.assertRaisesRegex(ValueError, 'metadata hash'):
            patch.analyze_and_patch(self.original, self.metadata[:-1] + bytes([self.metadata[-1] ^ 1]))
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b'not an APK'); handle.flush()
            with self.assertRaisesRegex(ValueError, 'APK hash'):
                patch.read_apk(handle.name)


if __name__ == '__main__':
    unittest.main()
