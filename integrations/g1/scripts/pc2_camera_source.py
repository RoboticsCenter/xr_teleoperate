#!/usr/bin/env python3
"""Read the existing G1 VideoHub RGB service; send JSONL JPEG over SSH stdout.

Only an explicit run creates the official image RPC client. No camera device is
opened, no robot controller is imported, and no service is started or stopped.
The SSH owner closing stdin ends this process and its image requests.
Laptop preview is bounded to 640x360, JPEG quality 70, and at most 5 requests/s.
"""
import base64
from contextlib import redirect_stdout
import json
import signal
import sys
import threading
import time

RPC_TIMEOUT_S = 1.0
# Fit the shared 2.4 GHz Wi-Fi link; these affect video preview only.
MAX_WIDTH = 640
MAX_HEIGHT = 360
JPEG_QUALITY = 70
MAX_PREVIEW_FPS = 5.0
MIN_FRAME_PERIOD_S = 1.0 / MAX_PREVIEW_FPS
_PROTOCOL_STDOUT = sys.stdout


def create_video_client():
    """Initialize only the official VideoHub image request API on PC2 eth0."""
    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.go2.video.video_client import VideoClient

    ChannelFactoryInitialize(0, 'eth0')
    client = VideoClient()
    client.SetTimeout(RPC_TIMEOUT_S)
    client.Init()
    return client


def transport_jpeg(payload, cv):
    """Validate a VideoHub JPEG and bound its transport size without upscaling."""
    import numpy as np

    if not isinstance(payload, (bytes, bytearray, memoryview, list, tuple)):
        raise ValueError('VideoHub returned an invalid JPEG payload')
    try:
        jpeg = bytes(payload)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('VideoHub returned an invalid JPEG payload') from exc
    if not jpeg.startswith(b'\xff\xd8') or not jpeg.endswith(b'\xff\xd9'):
        raise ValueError('VideoHub returned an invalid JPEG image')
    frame = cv.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv.IMREAD_COLOR)
    if frame is None or len(frame.shape) != 3 or frame.shape[2] != 3:
        raise ValueError('VideoHub JPEG could not be decoded as RGB')
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError('VideoHub JPEG has invalid image dimensions')
    scale = min(1., MAX_WIDTH / width, MAX_HEIGHT / height)
    if scale < 1.:
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
        frame = cv.resize(frame, (width, height), interpolation=cv.INTER_AREA)
    ok, encoded = cv.imencode('.jpg', frame, [cv.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise OSError('VideoHub JPEG transport encoding failed')
    return bytes(encoded), int(width), int(height)


def emit(record):
    print(json.dumps(record, separators=(',', ':')), file=_PROTOCOL_STDOUT, flush=True)


def run(stopped=None, cv=None, client=None):
    stopped = stopped or threading.Event()
    if stopped.is_set():
        return
    if cv is None:
        import cv2 as cv
    sequence = 0
    ready = False
    while not stopped.is_set():
        try:
            if client is None:
                client = create_video_client()
            if stopped.is_set():
                break
            request_started = time.monotonic()
            code, payload = client.GetImageSample()
            # An RPC may have been in flight when the SSH owner requested STOP.
            if stopped.is_set():
                break
            if code != 0:
                raise RuntimeError('VideoHub image request failed (code %s)' % code)
            jpeg, width, height = transport_jpeg(payload, cv)
            if stopped.is_set():
                break
            if not ready:
                emit({'type': 'camera_ready', 'device': 'videohub', 'format': 'JPEG'})
                ready = True
            sequence += 1
            emit({'type': 'camera_frame', 'sequence': sequence,
                  'width': width, 'height': height,
                  'jpeg': base64.b64encode(jpeg).decode('ascii')})
            # Include RPC, encoding and SSH backpressure in the period. No
            # catch-up bursts or frame queue when producing a frame takes longer.
            elapsed = time.monotonic() - request_started
            stopped.wait(max(0., MIN_FRAME_PERIOD_S - elapsed))
        except BrokenPipeError:
            raise
        except Exception as exc:
            if stopped.is_set():
                break
            ready = False
            emit({'type': 'camera_error', 'error': str(exc)})
            stopped.wait(2.)  # Branch retry delay; no session deadline.
    # This SDK has no public VideoClient.Close(). Its reader threads are daemon
    # threads; the dedicated SSH process exits and releases its own DDS resources.
    # The robot's VideoHub/master service remains running and owns the camera.


def main():
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stopped.set())

    def watch_owner():
        for _ in sys.stdin:
            stopped.set()
            return
        stopped.set()

    threading.Thread(target=watch_owner, daemon=True).start()
    try:
        # SDK diagnostics must not prefix an otherwise valid protocol JSON line.
        with redirect_stdout(sys.stderr):
            run(stopped)
    except BrokenPipeError:
        pass


if __name__ == '__main__':
    main()
