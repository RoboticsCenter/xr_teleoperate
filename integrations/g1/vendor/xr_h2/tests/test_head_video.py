"""XRoboToolkit wire compatibility and real H.264 encode/decode, no robot IO."""
import socket
import importlib.util
import struct
import unittest
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import patch
from head_camera.pico_video import camera_request, encoder, read_command, VideoBridge, exact


def request_payload(ip='127.0.0.1'):
    camera = b'ZED'
    ip = ip.encode()
    return b'\xca\xfe\x01'+struct.pack('<7i', 2560, 720, 60, 4_000_000, 0, 0, 12345)+bytes([len(camera)])+camera+bytes([len(ip)])+ip


class Protocol(unittest.TestCase):
    def test_official_camera_binary_layout(self):
        value = camera_request(request_payload(), '127.0.0.1')
        self.assertEqual(value, {'ip': '127.0.0.1', 'port': 12345, 'fps': 30, 'bitrate': 4_000_000})

    def test_camera_cannot_redirect_to_another_device(self):
        with self.assertRaises(ValueError):
            camera_request(request_payload('192.168.31.1'), '192.168.31.100')

    def test_length_prefix_and_little_endian_command_body(self):
        command, payload = b'OPEN_CAMERA', request_payload()
        body = struct.pack('<I', len(command))+command+struct.pack('<I', len(payload))+payload
        a, b = socket.socketpair()
        try:
            a.sendall(struct.pack('>I', len(body))+body)
            self.assertEqual(read_command(b), ('OPEN_CAMERA', payload))
        finally:
            a.close()
            b.close()

    def test_truncated_camera_request_rejected(self):
        for n in (0, 3, 20, 32, 35):
            with self.assertRaises(ValueError):
                camera_request(request_payload()[:n], '127.0.0.1')

    @unittest.skipUnless(importlib.util.find_spec('av') and importlib.util.find_spec('numpy'),
                         'Run the H.264 check in .runtime-video')
    def test_h264_packets_decode_at_the_headset_frame_size(self):
        import av
        import numpy as np
        encode = encoder(30, 4_000_000)
        decode = av.CodecContext.create('h264', 'r')
        frames = []
        for i in range(3):
            image = np.full((720, 2560, 3), i*60+20, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            frame.pts = i
            for packet in encode.encode(frame):
                frames.extend(decode.decode(av.Packet(bytes(packet))))
        self.assertEqual(len(frames), 3)
        self.assertTrue(all((f.width, f.height) == (2560, 720) for f in frames))


@unittest.skipUnless(importlib.util.find_spec('av') and importlib.util.find_spec('numpy'),
                     'Run video recovery checks in .runtime-video')
class VideoRecovery(unittest.TestCase):
    def setUp(self):
        import av
        import numpy as np
        codec = av.CodecContext.create('mjpeg', 'w')
        codec.width, codec.height, codec.pix_fmt = 1280, 720, 'yuvj420p'
        codec.time_base = Fraction(1, 30)
        frame = av.VideoFrame.from_ndarray(np.full((720,1280,3), 80, dtype=np.uint8), format='rgb24')
        self.jpeg = b''.join(bytes(p) for p in codec.encode(frame.reformat(format='yuvj420p')))

    def source(self, fail_first=False):
        jpeg = self.jpeg
        class Source:
            requests = 0
            def __init__(self, *_args, **_kwargs): pass
            def request(self, *_args):
                Source.requests += 1
                if fail_first and Source.requests == 1:
                    raise TimeoutError('injected source interruption')
            def getresponse(self): return SimpleNamespace(status=200, read=lambda: jpeg)
            def close(self): pass
        return Source

    def listen(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        listener.settimeout(4.)
        self.addCleanup(listener.close)
        return listener

    def packet(self, client):
        client.settimeout(4.)
        return exact(client, struct.unpack('>I', exact(client, 4))[0])

    def test_source_timeout_recovers_without_closing_headset_connection(self):
        listener = self.listen()
        bridge = VideoBridge('http://127.0.0.1:1/api/head-camera.jpg')
        with patch('head_camera.pico_video.HTTPConnection', self.source(fail_first=True)):
            try:
                bridge.start({'ip':'127.0.0.1','port':listener.getsockname()[1], 'fps':30, 'bitrate':4000000})
                client, _ = listener.accept()
                self.addCleanup(client.close)
                self.packet(client)
                self.packet(client)
                status = bridge.status()
                self.assertEqual(status['source_interruptions'], 1)
                self.assertEqual(status['reconnections'], 0)
                self.assertEqual(status['source_error'], '')
            finally:
                bridge.stop()

    def test_headset_socket_reset_reconnects_with_a_decodable_keyframe(self):
        import av
        listener = self.listen()
        bridge = VideoBridge('http://127.0.0.1:1/api/head-camera.jpg')
        with patch('head_camera.pico_video.HTTPConnection', self.source()):
            try:
                bridge.start({'ip':'127.0.0.1','port':listener.getsockname()[1], 'fps':30, 'bitrate':4000000})
                first, _ = listener.accept()
                self.packet(first)
                first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                first.close()
                second, _ = listener.accept()
                self.addCleanup(second.close)
                encoded = self.packet(second)
                decoded = av.CodecContext.create('h264', 'r').decode(av.Packet(encoded))
                self.assertEqual([(f.width, f.height) for f in decoded], [(2560, 720)])
                self.assertGreaterEqual(bridge.status()['reconnections'], 1)
            finally:
                bridge.stop()

    def test_old_control_session_cannot_stop_a_newer_viewer(self):
        bridge = VideoBridge('http://127.0.0.1:1/api/head-camera.jpg')
        bridge.generation = 2
        self.assertFalse(bridge.stop(generation=1))
        self.assertFalse(bridge.stopped.is_set())


if __name__ == '__main__':
    unittest.main()
