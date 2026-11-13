"""Read-only tracker snapshots. RAM-backed files are atomic and private to this user."""
import json
import os
from pathlib import Path
import time

from .geometry import pose_valid

RUNTIME = Path('/dev/shm') / f'h2-pico-tracker-{os.getuid()}'
FRAME_PATH = RUNTIME / 'latest.json'


def read_frame(path=FRAME_PATH, max_age=.5):
    try:
        frame = json.loads(Path(path).read_text())
        age = time.monotonic()-frame.get('sample_mono_on_robot', frame['received_mono'])
        if not 0 <= age <= max_age:
            return None
        return frame
    except (OSError, ValueError, KeyError, TypeError):
        return None


class TrackerInput:
    def __init__(self, root, frame_path=FRAME_PATH):
        self.root, self.frame_path = Path(root), frame_path
        self.config_path = self.root / 'config/pico_tracker.json'

    def config(self):
        try:
            return json.loads(self.config_path.read_text())
        except FileNotFoundError:
            return {'left_sn': '', 'offset_tracker_m': [0., 0., 0.]}

    def select(self, serial, side='left'):
        if side not in ('left', 'right'):
            raise ValueError('Choose left or right tracker')
        frame = read_frame(self.frame_path)
        if serial and (not frame or serial not in frame.get('trackers', {})):
            return False, 'Choose a currently connected tracker serial number'
        config = self.config()
        other = 'right' if side == 'left' else 'left'
        if serial and config.get(other+'_sn') == serial:
            return False, 'Use a different tracker serial for each wrist'
        config[side+'_sn'] = serial
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(config)+'\n')
        tmp.replace(self.config_path)
        return True, (f'{side.title()} wrist tracker selected; START captures the neutral arm pose' if serial else
                      f'{side.title()} wrist tracker assignment cleared')

    def status(self, both=False, head_only=False):
        frame = read_frame(self.frame_path)
        config = self.config()
        serial = config.get('left_sn', '')
        right_serial = config.get('right_sn', '')
        sides = () if head_only else ('left', 'right') if both else ('left',)
        trackers = frame.get('trackers', {}) if frame else {}
        ages = frame.get('pose_age_ms', {}) if frame else {}
        head_ready = bool(frame and pose_valid(frame.get('head_pose')))
        missing = [side for side in sides if config.get(side+'_sn') not in trackers]
        stale = [side for side in sides if ages.get(config.get(side+'_sn'), 1e9) >= 1000]
        duplicate = not head_only and both and serial and serial == right_serial
        ready = bool(frame and not missing and not stale and head_ready and not duplicate)
        message = ('Tracker input disconnected' if not frame else
                   'Use a different tracker serial for each wrist' if duplicate else
                   f'Choose the {missing[0]} wrist tracker' if missing else
                   'Enable Head in XRoboToolkit' if not head_ready else
                   f'Move the {stale[0]} tracker to verify its pose is updating' if stale else
                   'Headset ready; START captures forward' if head_only else
                   ('Both wrist trackers ready; START captures neutral' if both else
                    'Left wrist tracker ready; START captures neutral'))
        return {'connected': bool(frame), 'ready': ready, 'left_sn': serial,
                'right_sn': right_serial, 'required_sides': list(sides),
                'serial_numbers': list(trackers), 'trackers': trackers,
                'pose_age_ms': ages, 'head_ready': head_ready, 'message': message,
                'timestamp_source': frame.get('timestamp_source') if frame else None,
                'transport_delay_ms': round(frame.get('transport_delay_ms', 0), 1) if frame else None,
                'sample_hz': frame.get('sample_hz', 0) if frame else 0}
