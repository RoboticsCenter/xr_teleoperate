#!/usr/bin/env python3
"""Adapt the H2 TeleImager JPEG view to XRoboToolkit Remote Vision.

Control framing follows XRoboToolkit CameraRequestSerializer and
NetworkDataProtocolSerializer; video framing follows Orin-Video-Sender.
The original camera drivers and headset application are unchanged. A mono
head image is repeated for the two display eyes, without claiming stereo.
"""
import argparse
from fractions import Fraction
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import socketserver
import struct
import threading
import time
from urllib.parse import urlsplit


def exact(sock, length):
    data = bytearray()
    while len(data) < length:
        part = sock.recv(length-len(data))
        if not part:
            raise EOFError
        data.extend(part)
    return bytes(data)


def read_command(sock):
    size = struct.unpack('>I', exact(sock, 4))[0]
    if not 8 <= size <= 4096:
        raise ValueError('Invalid XRoboToolkit command size')
    data = exact(sock, size)
    n = struct.unpack_from('<I', data)[0]
    if not 0 < n <= 128 or 8+n > len(data):
        raise ValueError('Invalid command name')
    command = data[4:4+n].decode().rstrip('\0')
    length = struct.unpack_from('<I', data, 4+n)[0]
    if 8+n+length != len(data):
        raise ValueError('Incomplete command')
    return command, data[8+n:]


def camera_request(data, peer):
    if len(data) < 33 or data[:3] != b'\xca\xfe\x01':
        raise ValueError('Unsupported CameraRequest version')
    width, height, fps, bitrate, hevc, mode, port = struct.unpack_from('<7i', data, 3)
    offset = 31
    strings = []
    for _ in range(2):
        if offset >= len(data):
            raise ValueError('Missing camera request string')
        n = data[offset]
        offset += 1
        if offset+n > len(data):
            raise ValueError('Incomplete camera request string')
        strings.append(data[offset:offset+n].decode())
        offset += n
    camera, ip = strings
    if ip != peer:
        raise ValueError('Video destination must be the requesting headset')
    if camera != 'ZED' or hevc:
        raise ValueError('Select ZEDMINI / H.264 in XRoboToolkit Remote Vision')
    if width != 2560 or height != 720 or not 1 <= fps <= 120 or not 1 <= port <= 65535:
        raise ValueError('Select ZEDMINI 2560x720 side-by-side video')
    return {'ip': ip, 'port': port, 'fps': min(fps, 30),
            'bitrate': max(1_000_000, min(bitrate, 8_000_000))}


def encoder(fps, bitrate):
    import av
    codec = av.CodecContext.create('libx264', 'w')
    codec.width, codec.height, codec.pix_fmt = 2560, 720, 'yuv420p'
    codec.time_base, codec.framerate = Fraction(1, fps), Fraction(fps)
    codec.bit_rate = bitrate
    codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency',
                     'x264-params': 'keyint=30:min-keyint=30:scenecut=0:bframes=0:repeat-headers=1'}
    codec.open()
    return codec


class VideoBridge:
    def __init__(self, source):
        self.source = urlsplit(source)
        if self.source.scheme != 'http':
            raise ValueError('Use the robot dashboard HTTP JPEG source')
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.stopped = threading.Event()
        self.worker = None
        self.generation = 0
        self.state = {'streaming': False, 'fps': 0., 'frames': 0, 'error': '',
                      'source_error': '', 'source_interruptions': 0, 'reconnections': 0,
                      'source': 'H2 head left eye, repeated for both eyes'}

    def status(self):
        with self.lock:
            return dict(self.state)

    def stop(self, generation=None):
        with self.operation_lock:
            if generation is not None and generation != self.generation:
                return False
            self.stopped.set()
            if self.worker:
                self.worker.join(timeout=3)
                if self.worker.is_alive():
                    raise RuntimeError('Previous video connection is still closing')
            self.worker = None
            with self.lock:
                self.state.update(streaming=False, error='', source_error='')
            return True

    def start(self, request):
        with self.operation_lock:
            self.stop()
            self.generation += 1
            self.stopped.clear()
            with self.lock:
                self.state.update(streaming=False, fps=0., frames=0, error='', source_error='',
                                  source_interruptions=0, reconnections=0)
            self.worker = threading.Thread(target=self._stream, args=(request,), daemon=True)
            self.worker.start()
            return self.generation

    def _stream(self, request):
        import av
        import numpy as np
        frames_sent = 0
        try:
            while not self.stopped.is_set():
                source = HTTPConnection(self.source.hostname, self.source.port or 80, timeout=1.)
                try:
                    with socket.create_connection((request['ip'], request['port']), timeout=2.) as video:
                        video.settimeout(.5)
                        video.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        video.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128*1024)
                        codec = encoder(request['fps'], request['bitrate'])
                        jpeg_decoder = av.CodecContext.create('mjpeg', 'r')
                        pts, window_start, window_frames = 0, time.monotonic(), 0
                        while not self.stopped.is_set():
                            tick = time.monotonic()
                            try:
                                source.request('GET', self.source.path)
                                response = source.getresponse()
                                jpeg = response.read()
                                if response.status != 200:
                                    raise OSError(f'Camera returned HTTP {response.status}')
                            except (OSError, HTTPException) as exc:
                                source.close()
                                with self.lock:
                                    self.state.update(streaming=False, source_error=f'Robot camera: {exc}')
                                    self.state['source_interruptions'] += 1
                                self.stopped.wait(.1)
                                continue
                            decoded = jpeg_decoder.decode(av.Packet(jpeg))[0]
                            if (decoded.width, decoded.height) != (1280, 720):
                                decoded = decoded.reformat(width=1280, height=720)
                            rgb = decoded.to_ndarray(format='rgb24')
                            frame = av.VideoFrame.from_ndarray(np.concatenate((rgb, rgb), axis=1), format='rgb24')
                            frame.pts, frame.time_base = pts, Fraction(1, request['fps'])
                            for packet in codec.encode(frame):
                                data = bytes(packet)
                                video.sendall(struct.pack('>I', len(data))+data)
                            pts += 1
                            frames_sent += 1
                            window_frames += 1
                            with self.lock:
                                self.state.update(streaming=True, frames=frames_sent, error='', source_error='')
                                if time.monotonic()-window_start >= 1.:
                                    self.state['fps'] = round(window_frames/(time.monotonic()-window_start), 1)
                                    window_start, window_frames = time.monotonic(), 0
                            self.stopped.wait(max(0., 1/request['fps']-(time.monotonic()-tick)))
                except OSError as exc:
                    with self.lock:
                        self.state.update(streaming=False, error=f'PICO video connection: {exc}')
                        self.state['reconnections'] += 1
                    self.stopped.wait(.5)
                finally:
                    source.close()
        except Exception as exc:
            with self.lock:
                self.state['error'] = str(exc)
        finally:
            with self.lock:
                self.state['streaming'] = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=13579)
    parser.add_argument('--status-port', type=int, default=13581)
    args = parser.parse_args()
    bridge = VideoBridge(args.source)

    class Control(socketserver.BaseRequestHandler):
        def handle(self):
            generation = None
            try:
                while True:
                    command, data = read_command(self.request)
                    if command == 'OPEN_CAMERA':
                        generation = bridge.start(camera_request(data, self.client_address[0]))
                    elif command == 'CLOSE_CAMERA':
                        if generation is not None:
                            bridge.stop(generation)
            except EOFError:
                pass
            except Exception as exc:
                with bridge.lock:
                    bridge.state['error'] = str(exc)
            finally:
                if generation is not None:
                    bridge.stop(generation)

    class Status(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            data = json.dumps(bridge.status()).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    status = ThreadingHTTPServer(('127.0.0.1', args.status_port), Status)
    threading.Thread(target=status.serve_forever, daemon=True).start()
    with Server((args.host, args.port), Control) as server:
        print(f'PICO Remote Vision listening on {args.host}:{args.port}', flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            bridge.stop()
            status.shutdown()


if __name__ == '__main__':
    main()
