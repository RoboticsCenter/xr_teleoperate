"""Offline ARM64 control-flow regression checks; no native code is executed."""
from pathlib import Path
import unittest
import zipfile

import patch_il2cpp as patch

APK = Path('/home/jack-xiang/.cache/unitree-g1-pico/XRoboToolkit-PICO-1.1.1.apk')
OUTPUT = Path(__file__).resolve().parent/'libil2cpp.so'


def calls_from_dropdown(library, selection, current_mode):
    """Interpret the decoded dispatch/call block, stopping before UI rendering."""
    image = patch.NativeImage(library)
    instructions = {i.address: i for i in image.instructions(0x100258c, 0x10025fc)}
    registers = dict(w21=selection, w0=current_mode, w1=0, x2=0, wzr=0, xzr=0)
    pc, equal, calls = 0x100258c, False, []
    for _ in range(30):
        if pc in (0x10025dc, 0x10025f0, 0x100268c):
            return calls
        instruction = instructions[pc]
        parts = instruction.op_str.split(', ')
        def value(operand):
            return int(operand[1:], 0) if operand.startswith('#') else registers[operand]
        next_pc = pc+4
        if instruction.mnemonic == 'cmp':
            equal = value(parts[0]) == value(parts[1])
        elif instruction.mnemonic == 'mov':
            registers[parts[0]] = value(parts[1])
        elif instruction.mnemonic in ('b', 'b.eq', 'b.ne'):
            if instruction.mnemonic == 'b' or (equal if instruction.mnemonic == 'b.eq' else not equal):
                next_pc = value(parts[0])
        elif instruction.mnemonic == 'cbz':
            if value(parts[0]) == 0:
                next_pc = value(parts[1])
        elif instruction.mnemonic == 'bl':
            if value(parts[0]) != 0x1ec8ac8:
                raise AssertionError('unexpected SDK target')
            calls.append((registers['w0'], registers['w1']))
            registers['w0'] = 0  # Simulated successful SDK return, never executed.
        else:
            raise AssertionError('unexpected dispatch instruction: '+patch.asm(instruction))
        pc = next_pc
    raise AssertionError('dispatch did not terminate')


class PatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile(APK) as archive:
            cls.original = archive.read(patch.LIB_ENTRY)
            cls.metadata = archive.read(patch.METADATA_ENTRY)
        cls.patched = OUTPUT.read_bytes()

    def test_object_requests_two_from_body_and_already_object(self):
        self.assertEqual(calls_from_dropdown(self.original, 2, 0), [(1, 1)])
        self.assertEqual(calls_from_dropdown(self.original, 2, 1), [])
        self.assertEqual(calls_from_dropdown(self.patched, 2, 0), [(1, 2)])
        self.assertEqual(calls_from_dropdown(self.patched, 2, 1), [(1, 2)])

    def test_body_and_none_behaviors_remain_identical(self):
        for selection in (0, 1):
            for mode in (0, 1):
                with self.subTest(selection=selection, current_mode=mode):
                    self.assertEqual(calls_from_dropdown(self.original, selection, mode),
                                     calls_from_dropdown(self.patched, selection, mode))
        self.assertEqual(calls_from_dropdown(self.patched, 1, 0), [])
        self.assertEqual(calls_from_dropdown(self.patched, 1, 1), [(0, 2)])

    def test_sdk_failure_and_ui_error_handling_remain_identical(self):
        start, end = 0x10015dc, 0x1001720
        self.assertEqual(self.original[start:end], self.patched[start:end])

    def test_reproduction_matches_delivered_library(self):
        reproduced, report = patch.analyze_and_patch(self.original, self.metadata)
        self.assertEqual(reproduced, self.patched)
        self.assertEqual(report['patched_library_sha256'],
                         '7132bada0316f78337ae8f93ede2962d772e0c336ca0650d38224f0f511409aa')
        self.assertEqual(patch.digest(APK.read_bytes()), patch.APK_SHA256)

    def test_unrecognized_input_is_rejected_before_any_patch(self):
        with self.assertRaisesRegex(ValueError, 'library hash'):
            patch.analyze_and_patch(self.patched, self.metadata)
        with self.assertRaisesRegex(ValueError, 'metadata hash'):
            patch.analyze_and_patch(self.original, self.metadata[:-1]+bytes([self.metadata[-1] ^ 1]))


if __name__ == '__main__':
    unittest.main()
