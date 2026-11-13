"""VideoHub adapter tests use generated images and fake RPCs, never hardware."""
import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import pc2_camera_source as source


def jpeg(width=1920, height=1080):
    output = io.BytesIO()
    Image.new('RGB', (width, height), (30, 90, 180)).save(output, format='JPEG')
    return output.getvalue()


class ImageCV:
    """Implements the small OpenCV image API via Pillow, with call recording."""
    IMREAD_COLOR = 1
    IMWRITE_JPEG_QUALITY = 2
    INTER_AREA = 3

    def __init__(self):
        self.resize_calls = []
        self.encode_calls = []

    def imdecode(self, raw, flags):
        try:
            return np.asarray(Image.open(io.BytesIO(bytes(raw))).convert('RGB'))
        except OSError:
            return None

    def resize(self, frame, size, interpolation):
        self.resize_calls.append((size, interpolation))
        return np.asarray(Image.fromarray(frame).resize(size))

    def imencode(self, extension, frame, parameters):
        self.encode_calls.append((extension, parameters))
        output = io.BytesIO()
        Image.fromarray(frame).save(output, format='JPEG', quality=parameters[1])
        return True, np.frombuffer(output.getvalue(), dtype=np.uint8)


class Stop:
    def __init__(self, stop_after_wait=None):
        self.value = False
        self.waits = []
        self.stop_after_wait = stop_after_wait

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def wait(self, duration):
        self.waits.append(duration)
        if self.stop_after_wait == len(self.waits):
            self.set()


class VideoHubImages(unittest.TestCase):
    def test_1080p_downscales_to_preview_size_and_jpeg70(self):
        cv = ImageCV()
        result, width, height = source.transport_jpeg(list(jpeg()), cv)
        self.assertEqual((width, height), (640, 360))
        self.assertEqual(Image.open(io.BytesIO(result)).size, (640, 360))
        self.assertEqual(cv.resize_calls, [((640, 360), cv.INTER_AREA)])
        self.assertEqual(cv.encode_calls, [('.jpg', [cv.IMWRITE_JPEG_QUALITY, 70])])

    def test_non_widescreen_preserves_aspect_and_small_frames_never_upscale(self):
        for size, expected in [((1600, 1200), (480, 360)),
                               ((600, 1000), (216, 360)), ((320, 240), (320, 240))]:
            with self.subTest(size=size):
                cv = ImageCV()
                result, width, height = source.transport_jpeg(jpeg(*size), cv)
                self.assertEqual((width, height), expected)
                self.assertEqual(Image.open(io.BytesIO(result)).size, expected)
                self.assertEqual(bool(cv.resize_calls), size != expected)

    def test_rejects_non_jpeg_malformed_decoding_and_invalid_binary(self):
        for payload in (None, 1000000, b'not a jpeg', b'\xff\xd8broken\xff\xd9', [300, -1]):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(ValueError):
                    source.transport_jpeg(payload, ImageCV())

    def test_encoder_failure_is_reported(self):
        cv = ImageCV()
        cv.imencode = Mock(return_value=(False, None))
        with self.assertRaisesRegex(OSError, 'encoding failed'):
            source.transport_jpeg(jpeg(16, 9), cv)


class VideoHubLifecycle(unittest.TestCase):
    def test_import_is_inert_even_when_sdk_and_opencv_are_unavailable(self):
        code = """
import importlib.abc, importlib.util, sys
class Block(importlib.abc.MetaPathFinder):
 def find_spec(self, name, path=None, target=None):
  if name.startswith(('unitree_sdk2py','cv2')):
   raise AssertionError('Import must not initialize SDK or camera')
sys.meta_path.insert(0,Block())
spec=importlib.util.spec_from_file_location('camera_source',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
        subprocess.run([sys.executable, '-B', '-c', code,
                        str(ROOT / 'scripts/pc2_camera_source.py')], check=True,
                       timeout=5, capture_output=True)

    def test_factory_initializes_only_video_client_with_finite_timeout(self):
        initialize = Mock()
        client = Mock()
        constructor = Mock(return_value=client)
        fake = {
            'unitree_sdk2py': types.ModuleType('unitree_sdk2py'),
            'unitree_sdk2py.core': types.ModuleType('unitree_sdk2py.core'),
            'unitree_sdk2py.core.channel': types.SimpleNamespace(ChannelFactoryInitialize=initialize),
            'unitree_sdk2py.go2': types.ModuleType('unitree_sdk2py.go2'),
            'unitree_sdk2py.go2.video': types.ModuleType('unitree_sdk2py.go2.video'),
            'unitree_sdk2py.go2.video.video_client': types.SimpleNamespace(VideoClient=constructor),
        }
        with patch.dict(sys.modules, fake):
            self.assertIs(source.create_video_client(), client)
        initialize.assert_called_once_with(0, 'eth0')
        constructor.assert_called_once_with()
        client.SetTimeout.assert_called_once_with(1.)
        client.Init.assert_called_once_with()
        client.GetImageSample.assert_not_called()

    def test_rpc_failure_invalid_image_then_valid_response_recovers(self):
        stopped = Stop(stop_after_wait=3)
        client = Mock()
        client.GetImageSample.side_effect = [(3103, None), (0, b'bad'), (0, jpeg(320, 240))]
        with patch.object(source, 'emit') as emit, \
                patch.object(source.time, 'monotonic', return_value=100.):
            source.run(stopped, ImageCV(), client)
        records = [call.args[0] for call in emit.call_args_list]
        self.assertEqual([r['type'] for r in records],
                         ['camera_error', 'camera_error', 'camera_ready', 'camera_frame'])
        self.assertIn('3103', records[0]['error'])
        self.assertEqual(records[2]['device'], 'videohub')
        self.assertEqual((records[3]['width'], records[3]['height']), (320, 240))
        self.assertEqual(records[3]['sequence'], 1)
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(records[3]['jpeg']))).size,
                         (320, 240))
        self.assertEqual(stopped.waits, [2., 2., .2])
        self.assertEqual(client.GetImageSample.call_count, 3)

    def test_preview_period_accounts_for_rpc_encoding_and_slow_transport(self):
        stopped = Stop(stop_after_wait=2)
        client = Mock()
        client.GetImageSample.return_value = (0, jpeg(16, 9))
        # First frame takes 80 ms; the second takes 250 ms. Wait only the
        # remaining period, and never add delay or attempt catch-up on the latter.
        with patch.object(source, 'emit'), \
                patch.object(source.time, 'monotonic',
                             side_effect=[100., 100.08, 100.2, 100.45]):
            source.run(stopped, ImageCV(), client)
        self.assertAlmostEqual(stopped.waits[0], .12)
        self.assertEqual(stopped.waits[1], 0.)
        self.assertEqual(client.GetImageSample.call_count, 2)

    def test_sdk_exception_recovers_without_creating_another_client(self):
        stopped = Stop(stop_after_wait=2)
        client = Mock()
        client.GetImageSample.side_effect = [OSError('RPC unavailable'), (0, jpeg(16, 9))]
        with patch.object(source, 'create_video_client', return_value=client) as create, \
                patch.object(source, 'emit') as emit:
            source.run(stopped, ImageCV())
        create.assert_called_once_with()
        self.assertEqual([c.args[0]['type'] for c in emit.call_args_list],
                         ['camera_error', 'camera_ready', 'camera_frame'])

    def test_stopped_before_run_does_not_construct_client(self):
        stopped = threading.Event()
        stopped.set()
        with patch.object(source, 'create_video_client') as create:
            source.run(stopped)
        create.assert_not_called()

    def test_stop_during_request_discards_response_and_never_requests_again(self):
        for response in [(0, jpeg(16, 9)), (3103, None)]:
            with self.subTest(code=response[0]):
                stopped = Stop()
                client = Mock()
                def request():
                    stopped.set()
                    return response
                client.GetImageSample.side_effect = request
                with patch.object(source, 'emit') as emit, \
                        patch.object(source, 'transport_jpeg') as encode:
                    source.run(stopped, ImageCV(), client)
                client.GetImageSample.assert_called_once_with()
                emit.assert_not_called()
                encode.assert_not_called()
                self.assertEqual(stopped.waits, [])

    def test_stop_during_encoding_discards_frame(self):
        stopped = Stop()
        client = Mock()
        client.GetImageSample.return_value = (0, jpeg(16, 9))
        def encode(*_):
            stopped.set()
            return b'\xff\xd8\xff\xd9', 16, 9
        with patch.object(source, 'emit') as emit, \
                patch.object(source, 'transport_jpeg', side_effect=encode):
            source.run(stopped, ImageCV(), client)
        emit.assert_not_called()
        client.GetImageSample.assert_called_once_with()

    def test_protocol_uses_saved_stdout_despite_sdk_log_redirect(self):
        protocol, logs = io.StringIO(), io.StringIO()
        with patch.object(source, '_PROTOCOL_STDOUT', protocol), source.redirect_stdout(logs):
            print('SDK log')
            source.emit({'type': 'camera_error', 'error': 'example'})
        self.assertEqual(logs.getvalue(), 'SDK log\n')
        self.assertEqual(json.loads(protocol.getvalue()),
                         {'type': 'camera_error', 'error': 'example'})


if __name__ == '__main__':
    unittest.main()
