"""Synthetic image checks; no sockets, tracker clients or hardware."""
import copy
from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import pico_distance_image as board


def status():
    return {'has_origin': True, 'needs_zero': False, 'ready': True,
            'selected_sides': ['left', 'right'], 'target_cm': 20., 'current_stage': 'forward',
            'sides': {side: {'live': True, 'displacement_cm': 20., 'forward_cm': 20.,
                            'left_cm': 0., 'up_cm': 0.} for side in ('left', 'right')}}


class DistanceImageTests(unittest.TestCase):
    def texts(self, state):
        calls = []
        original = ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            calls.append(str(text))
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, 'text', record):
            jpeg = board.render_distance_jpeg(state)
        return calls, jpeg

    def test_jpeg_dimensions_mode_units_and_pure_input(self):
        state = status()
        before = copy.deepcopy(state)
        text, jpeg = self.texts(state)
        with Image.open(BytesIO(jpeg)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (1280, 720))
            self.assertEqual(image.mode, 'RGB')
        self.assertEqual(state, before)
        self.assertIn('20.0 cm', text)
        self.assertIn('+20.0 cm', text)
        self.assertTrue(any('PICO' in item and ('仅供参考' in item or 'RAW REFERENCE' in item) for item in text))
        self.assertTrue(any('你输入的实际方向和距离' in item or 'entered physical direction and distance' in item for item in text))

    def test_no_origin_and_invalidated_origin_never_show_old_values(self):
        for origin, needs in ((False, True), (True, True)):
            state = status()
            state.update(has_origin=origin, needs_zero=needs)
            first = board.render_distance_jpeg(state)
            state['sides']['left']['displacement_cm'] = 987.
            state['sides']['right']['forward_cm'] = 654.
            self.assertEqual(first, board.render_distance_jpeg(state))
            text, _ = self.texts(state)
            self.assertEqual(text.count('-- cm'), 8)

    def test_one_stale_side_hidden_other_side_still_visible(self):
        state = status()
        state['ready'] = False
        state['sides']['left']['live'] = False
        first = board.render_distance_jpeg(state)
        state['sides']['left'].update(displacement_cm=999., forward_cm=999.)
        self.assertEqual(first, board.render_distance_jpeg(state))
        text, _ = self.texts(state)
        self.assertEqual(text.count('-- cm'), 4)
        self.assertIn('20.0 cm', text)
        state['sides']['right']['displacement_cm'] = 21.
        self.assertNotEqual(first, board.render_distance_jpeg(state))

    def test_unselected_side_cannot_display_injected_values(self):
        state = status()
        state['selected_sides'] = ['right']
        text, first = self.texts(state)
        self.assertEqual(text.count('-- cm'), 4)
        state['sides']['left']['displacement_cm'] = 999.
        self.assertEqual(first, board.render_distance_jpeg(state))

    def test_pending_zero_and_error_visibly_change_board(self):
        state = status()
        first = board.render_distance_jpeg(state)
        state['zero_pending'] = True
        pending = board.render_distance_jpeg(state)
        self.assertNotEqual(first, pending)
        text, _ = self.texts(state)
        self.assertEqual(text.count('-- cm'), 8)
        state['zero_pending'] = False
        state['calibration_error'] = '等待标定状态。'
        self.assertNotEqual(first, board.render_distance_jpeg(state))

    def test_nonfinite_bool_and_missing_values_are_not_numbers(self):
        state = status()
        state['sides']['left'].update(displacement_cm=float('nan'), forward_cm=float('inf'),
                                      left_cm=True, up_cm=None)
        text, _ = self.texts(state)
        self.assertEqual(text.count('-- cm'), 4)
        self.assertFalse(any('nan' in item or 'inf cm' in item for item in text))
        self.assertTrue(board.render_distance_jpeg(None).startswith(b'\xff\xd8'))


if __name__ == '__main__':
    unittest.main()
