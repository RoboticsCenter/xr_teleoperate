#!/usr/bin/env python3
"""One official XRoboToolkit input client for wrist trackers and controllers.

The released Python getters retain Controller/Head/Motion when a later packet
omits those sections. Their global timestamp therefore cannot establish source
freshness. Read the same installed SDK's JSON callback instead; no SDK or headset
binaries are changed, and no client is opened by importing this module.

The v1.1.1 headset removes Controller when its toggle is off, but sends no
per-controller hardware tracking flag. ``packet_pose`` means a fresh, present,
geometrically valid pose, not independently verified hardware tracking. Explicit
tracking flags, when provided by a client, are honored. Stationary poses are valid.
Each accepted wrist pose keeps the valid Head pose from the same SDK packet.
This is packet pairing, not a claim of synchronized hardware capture timestamps.

Callback ABI: XRoboToolkit PC Service SDK/include/PXREARobotSDK.h:
PXREAInit(void *, callback(void *, enum, int, void *), unsigned);
PXREADevStateJson contains char devID[32], char stateJson[16352].
"""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor/xr_h2'))
from pico_tracker.geometry import pose_valid

SAMPLE_HZ = 120
MAX_FRAME_BYTES = 4096
SERVER_DISCONNECT = 1 << 3
DEVICE_MISSING = 1 << 5
DEVICE_STATE_JSON = 1 << 25


def _stamp(value):
    return value if type(value) is int and value > 0 else 0


def _pose(value):
    if isinstance(value, str):
        try:
            value = [float(item) for item in value.split(',')]
        except ValueError:
            return None
    if not pose_valid(value) or any(type(item) not in (int, float) for item in value):
        return None
    return list(value)


def _tracking_allowed(value):
    # Existing headset packets lack these fields. Never invent tracking flags
    # from movement, button values, or a cached SDK getter.
    for name in ('trackingValid', 'isTracked', 'isConnected'):
        if name in value and value[name] not in (True, 1):
            return False
    return True


def decode_packet(raw):
    """Unwrap the SDK's state JSON without retaining omitted source sections."""
    data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    if not isinstance(data, dict):
        raise ValueError('XRoboToolkit packet must be an object')
    if 'value' in data:
        data = json.loads(data['value']) if isinstance(data['value'], str) else data['value']
    if not isinstance(data, dict):
        raise ValueError('XRoboToolkit packet value must be an object')
    return data


class FrameBuilder:
    """Pure packet adapter; timestamps use callback receipt's monotonic clock."""
    def __init__(self, session=None):
        self.session = session or uuid.uuid4().hex
        self.sequence = 0
        self.reset()

    def reset(self):
        """Drop cached source state after a service/device disconnect."""
        self.last_packet_stamp = 0
        self.last_emit = None
        self.tracker_stamp = 0
        self.tracker_stamp_source = None
        self.tracker_sample_mono = None
        self.controller_stamps = {}
        self.controller_stamp_sources = {}
        self.controller_samples = {}
        self.head_sample_mono = None
        self.trackers = {}
        self.controllers = {}
        self.tracker_head_poses = {}
        self.controller_head_poses = {}
        self.previous = {}
        self.changed = {}

    def consume(self, raw, *, sample_mono, sample_at_unix_ns):
        packet = decode_packet(raw)
        packet_stamp = _stamp(packet.get('timeStampNs'))
        if not packet_stamp or packet_stamp <= self.last_packet_stamp:
            return None
        self.last_packet_stamp = packet_stamp
        head = packet.get('Head')
        head_pose = _pose(head.get('pose')) if isinstance(head, dict) else None
        if head_pose is not None and _tracking_allowed(head):
            self.head_sample_mono = sample_mono
        else:
            head_pose = None
        motion = packet.get('Motion')
        motion_stamp = _stamp(motion.get('timeStampNs')) if isinstance(motion, dict) else 0
        if isinstance(motion, dict):
            source_stamp = motion_stamp or packet_stamp
            stamp_source = 'motion' if motion_stamp else 'packet'
            if stamp_source != self.tracker_stamp_source or source_stamp > self.tracker_stamp:
                self.tracker_stamp = source_stamp
                self.tracker_stamp_source = stamp_source
                self.tracker_sample_mono = sample_mono
                joints = motion.get('joints', [])
                joints = joints if isinstance(joints, list) else []
                # v1.1.1 reuses its joints array. len is the current successful
                # query count; trailing entries may belong to disconnected units.
                count = motion.get('len', len(joints))
                count = count if type(count) is int and 0 <= count <= len(joints) else 0
                self.trackers = {}
                self.tracker_head_poses = {}
                if _tracking_allowed(motion) and motion.get('countValid', True) is not False:
                    for joint in joints[:min(count, 3)]:
                        if not isinstance(joint, dict) or not _tracking_allowed(joint):
                            continue
                        serial, pose = joint.get('sn'), _pose(joint.get('p'))
                        if isinstance(serial, str) and serial.strip() and pose is not None:
                            self.trackers[serial.strip()] = pose
                            if head_pose is not None:
                                self.tracker_head_poses[serial.strip()] = list(head_pose)
            if not _tracking_allowed(motion) or motion.get('countValid', True) is False:
                self.trackers = {}
                self.tracker_head_poses = {}
        else:
            self.trackers = {}
            self.tracker_head_poses = {}

        controller = packet.get('Controller')
        controller = controller if isinstance(controller, dict) else {}
        for side in ('left', 'right'):
            item = controller.get(side)
            pose = _pose(item.get('pose')) if isinstance(item, dict) else None
            if pose is None or not _tracking_allowed(item):
                self.controllers.pop(side, None)
                self.controller_head_poses.pop(side, None)
                continue
            controller_stamp = _stamp(item.get('timeStampNs'))
            source_stamp = controller_stamp or packet_stamp
            stamp_source = 'controller' if controller_stamp else 'packet'
            if (stamp_source != self.controller_stamp_sources.get(side) or
                    source_stamp > self.controller_stamps.get(side, 0)):
                self.controller_stamps[side] = source_stamp
                self.controller_stamp_sources[side] = stamp_source
                self.controller_samples[side] = sample_mono
                self.controllers[side] = pose
                self.controller_head_poses.pop(side, None)
                if head_pose is not None:
                    self.controller_head_poses[side] = list(head_pose)
        for serial, pose in self.trackers.items():
            if pose != self.previous.get(serial):
                self.changed[serial] = sample_mono
            self.previous[serial] = pose
        delta = sample_mono - self.last_emit if self.last_emit is not None else 0
        frame = {
            'version': 1, 'source': 'xrobotoolkit_sdk',
            'session': self.session, 'seq': self.sequence,
            'trackers': dict(self.trackers), 'controllers': dict(self.controllers),
            'head_pose': head_pose,
            'tracker_head_poses': {sn: list(pose) for sn, pose in self.tracker_head_poses.items()},
            'controller_head_poses': {side: list(pose) for side, pose in self.controller_head_poses.items()},
            'motion_timestamp_ns': motion_stamp, 'packet_timestamp_ns': packet_stamp,
            'timestamp_source': 'motion' if motion_stamp else 'packet',
            'tracker_sample_mono': self.tracker_sample_mono,
            'controller_sample_mono': max(self.controller_samples.values(), default=None),
            'controller_sample_mono_by_side': dict(self.controller_samples),
            'controller_validity_source': 'packet_pose',
            'head_sample_mono': self.head_sample_mono,
            'sample_at_unix_ns': sample_at_unix_ns, 'sample_mono': sample_mono,
            'sample_hz': round(1 / max(delta, 1e-6), 1) if delta else 0,
            'pose_age_ms': {sn: round((sample_mono - self.changed[sn]) * 1000, 1)
                            for sn in self.trackers},
        }
        self.sequence += 1
        self.last_emit = sample_mono
        return frame


class DeviceStateJson(ctypes.Structure):
    _fields_ = [('devID', ctypes.c_char * 32), ('stateJson', ctypes.c_char * 16352)]


Callback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int,
                           ctypes.c_int, ctypes.c_void_p)


def sdk_library_path():
    """Find the existing SDK package without importing its native extension."""
    spec = importlib.util.find_spec('xrobotoolkit_sdk')
    if spec is None or spec.origin is None:
        raise RuntimeError('Run with the existing PICO Python environment; xrobotoolkit_sdk is missing')
    library = Path(spec.origin).parent / 'libPXREARobotSDK.so'
    if not library.is_file():
        raise RuntimeError('Installed XRoboToolkit package has no libPXREARobotSDK.so')
    return library


class RawSdkClient:
    """A single SDK client; callback copies only the newest complete packet."""
    def __init__(self, *, library_path=None, loader=ctypes.CDLL):
        self.library_path = library_path
        self.loader = loader
        self.library = None
        self.callback = None
        self.lock = threading.Lock()
        self.latest = None
        self.generation = 0
        self.started = False

    def start(self):
        if self.started:
            return
        self.library = self.loader(str(self.library_path or sdk_library_path()))
        self.callback = Callback(self._receive)
        self.library.PXREAInit.argtypes = [ctypes.c_void_p, Callback, ctypes.c_uint]
        self.library.PXREAInit.restype = ctypes.c_int
        self.library.PXREADeinit.argtypes = []
        self.library.PXREADeinit.restype = ctypes.c_int
        mask = SERVER_DISCONNECT | DEVICE_MISSING | DEVICE_STATE_JSON
        result = self.library.PXREAInit(None, self.callback, mask)
        if result != 0:
            raise RuntimeError(f'PXREAInit failed: {result}')
        self.started = True

    def _receive(self, context, event, status, user_data):
        if event in (SERVER_DISCONNECT, DEVICE_MISSING):
            with self.lock:
                self.generation += 1
                self.latest = None
        elif event == DEVICE_STATE_JSON and user_data:
            # The SDK's stack-allocated struct expires on callback return.
            data = ctypes.cast(user_data, ctypes.POINTER(DeviceStateJson)).contents
            raw = bytes(data.stateJson)
            received = (raw, time.monotonic(), time.time_ns())
            with self.lock:
                self.latest = received

    def take_latest(self):
        with self.lock:
            latest, self.latest = self.latest, None
            return self.generation, latest

    def close(self):
        if self.started:
            # The C API joins its callback threads before returning. Keep the
            # callback alive until then; never run a second Python SDK init.
            self.library.PXREADeinit()
            self.started = False


def write_frame(fd, frame):
    data = (json.dumps(frame, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(data) > MAX_FRAME_BYTES:
        return False
    try:
        return os.write(fd, data) == len(data)
    except BlockingIOError:
        return False  # Pipe writes <= PIPE_BUF are atomic; never queue old poses.


def main():
    # Native SDK writes C stdout; keep it out of the JSONL channel.
    json_fd = os.dup(1)
    os.dup2(2, 1)
    os.set_blocking(json_fd, False)
    client = RawSdkClient()
    builder = FrameBuilder()
    generation = 0
    try:
        client.start()
        while True:
            tick = time.monotonic()
            current_generation, latest = client.take_latest()
            if current_generation != generation:
                builder.reset()
                generation = current_generation
            if latest is not None:
                raw, sample_mono, sample_at = latest
                try:
                    frame = builder.consume(raw, sample_mono=sample_mono,
                                            sample_at_unix_ns=sample_at)
                except (ValueError, TypeError, UnicodeError):
                    frame = None
                if frame is not None:
                    write_frame(json_fd, frame)
            time.sleep(max(0., 1 / SAMPLE_HZ - (time.monotonic() - tick)))
    finally:
        client.close()
        os.close(json_fd)


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
