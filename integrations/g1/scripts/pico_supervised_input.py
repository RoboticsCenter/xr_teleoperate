"""Explicit 10-second legacy-PICO input policy; pure checks, no hardware I/O.

Acceptance is an operator-supervised input decision, never a vendor tracking
validity assertion. Robot feedback, ownership, control mode and output limits
remain separate gates. Instantiating this class explicitly selects this policy.
"""
import copy
import math


POLICY_ID = 'supervised_position_only_v1'
TRACKER_WINDOW_POLICY_ID = 'supervised_tracker_window_v1'


def finite(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('invalid_' + label)
    return float(value)


def integer(value, label, minimum=1):
    if type(value) is not int or not minimum <= value < 2**63:
        raise ValueError('invalid_' + label)
    return value


def vector(value, size, label):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError('invalid_' + label)
    return [finite(x, label) for x in value]


class _PositionPolicy:
    """Shared Tracker, reference and limited-IK validation; no hardware I/O."""
    initial_fields = {}
    def __init__(self, sn):
        if not isinstance(sn, str) or not sn.strip():
            raise ValueError('explicit_right_sn_required')
        self.sn = sn
        self.reference = self.origin = self.previous = None
        self.origin_ns = None
        self.started_ns = None
        self.latched = False
        self.reference_ids = set()

    def set_reference(self, reference):
        if not isinstance(reference, dict):
            raise ValueError('reference_required')
        ident = reference.get('reference_id')
        if not isinstance(ident, str) or not ident or ident in self.reference_ids:
            raise ValueError('fresh_unique_reference_required')
        if reference.get('right_sn') != self.sn or reference.get('state_valid') is not True:
            raise ValueError('reference_identity_or_state_invalid')
        if reference.get('clock_domain') != 'local_monotonic' or reference.get('reference_kind') != 'live':
            raise ValueError('live_local_reference_required')
        integer(reference.get('received_monotonic_ns'), 'reference_receipt')
        integer(reference.get('state_sequence'), 'reference_state_sequence', 0)
        q = vector(reference.get('q_rad'), 14, 'reference_q')
        axes = reference.get('axis_config')
        if (not isinstance(axes, dict) or not isinstance(axes.get('id'), str) or not axes['id']
                or axes.get('pose_unit') != 'm' or type(axes.get('verified')) is not bool):
            raise ValueError('explicit_axis_unit_configuration_required')
        matrix = axes.get('T_robot_axes_xr_axes')
        if not isinstance(matrix, list) or len(matrix) != 3:
            raise ValueError('invalid_axis_rotation')
        rows = [vector(r, 3, 'axis_rotation') for r in matrix]
        for i in range(3):
            for j in range(3):
                if abs(sum(rows[i][k]*rows[j][k] for k in range(3))-(i == j)) > 1e-6:
                    raise ValueError('invalid_axis_rotation')
        a,b,c = rows
        det = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
        if abs(det-1.) > 1e-6:
            raise ValueError('invalid_axis_handedness')
        self.reference = copy.deepcopy(reference)
        self.reference['q_rad'] = q
        self.reference_ids.add(ident)
        self.origin = self.previous = self.origin_ns = None
        self.started_ns = None
        self.latched = False

    def _sample(self, sample, now_ns):
        if not isinstance(sample, dict) or sample.get('type') != 'sample':
            raise ValueError('normalized_pico_sample_required')
        if sample.get('source') != 'native_json_callback' or sample.get('snapshot_atomic') is not True:
            raise ValueError('atomic_native_packet_required')
        if sample.get('issues') != []:
            raise ValueError('pico_packet_has_issues')
        seq = integer(sample.get('packet_sequence'), 'packet_sequence')
        receipt = integer(sample.get('local_monotonic_ns'), 'packet_receipt')
        stamp = integer(sample.get('app_timestamp_ns'), 'application_timestamp')
        if receipt > now_ns or now_ns-receipt > 100_000_000:
            raise ValueError('stale_or_future_pico_packet')
        if sample.get('app_focused') is not True or type(sample.get('input_mode')) is not int or sample['input_mode'] not in (1,2):
            raise ValueError('app_focus_and_controller_mode_required')
        if not isinstance(sample.get('device_id'), str) or not sample['device_id']:
            raise ValueError('missing_headset_identity')
        if type(sample.get('motion_declared_count')) is not int or sample['motion_declared_count'] != 1:
            raise ValueError('single_tracker_policy_requires_len_one')
        trackers = sample.get('trackers')
        if not isinstance(trackers, list) or len(trackers) != 1 or not isinstance(trackers[0], dict):
            raise ValueError('single_normalized_tracker_required')
        tracker = trackers[0]
        if tracker.get('serial_number') != self.sn or tracker.get('representation_ok') is not True:
            raise ValueError('right_tracker_identity_or_representation')
        if sample.get('tracking_valid') is False or tracker.get('tracking_valid') is False or tracker.get('confidence') in (2,3):
            raise ValueError('tracking_explicitly_invalid')
        if tracker.get('confidence') is not None or tracker.get('tracking_valid') is not None or sample.get('tracking_valid') is not None:
            raise ValueError('use_vendor_policy_when_tracking_metadata_is_present')
        pose = vector(tracker.get('xyzw_pose'), 7, 'tracker_pose')
        if max(abs(x) for x in pose[:3]) < 1e-6:
            raise ValueError('zero_position_excluded_by_supervised_policy')
        norm = math.sqrt(sum(x*x for x in pose[3:]))
        if not .99 <= norm <= 1.01:
            raise ValueError('invalid_tracker_quaternion')
        return seq, receipt, stamp, pose

    def _ik(self, ik, sample, pose, first):
        if not isinstance(ik, dict) or ik.get('accepted') is not True or ik.get('mode') != 'live_input_preview':
            raise ValueError('accepted_live_ik_required')
        if ik.get('tracking_valid') is not None:
            raise ValueError('ik_must_preserve_unknown_vendor_tracking')
        for ik_key, sample_key in (('source_packet_sequence','packet_sequence'),
                                   ('source_received_monotonic_ns','local_monotonic_ns'),
                                   ('source_app_timestamp_ns','app_timestamp_ns'),
                                   ('sequence','packet_sequence')):
            if integer(ik.get(ik_key), ik_key) != sample[sample_key]:
                raise ValueError('ik_source_packet_mismatch')
        if (ik.get('reference_id') != self.reference['reference_id']
                or ik.get('right_sn') != self.sn
                or ik.get('calibration_id') != self.reference['axis_config']['id']
                or ik.get('reference_received_monotonic_ns') != self.reference['received_monotonic_ns']):
            raise ValueError('ik_reference_or_axis_id_mismatch')
        if ik.get('left_arm') != 'fixed' or ik.get('orientation') != 'held_at_robot_reference':
            raise ValueError('fixed_left_and_orientation_required')
        if abs(finite(ik.get('position_scale'), 'ik_scale')-.02) > 1e-12:
            raise ValueError('supervised_position_scale_must_be_point02')
        q = vector(ik.get('q_candidate_rad'), 14, 'ik_q')
        qref = self.reference['q_rad']
        if any(abs(q[i]-qref[i]) > 1e-9 for i in range(7)):
            raise ValueError('left_arm_changed')
        if max(abs(a-b) for a,b in zip(q,qref)) > .03+1e-9:
            raise ValueError('joint_reference_excursion')
        if first and (ik.get('status') != 'origin_captured' or max(abs(a-b) for a,b in zip(q,qref)) > 1e-9):
            raise ValueError('initial_candidate_must_equal_reference')
        if not first and ik.get('status') != 'candidate':
            raise ValueError('unexpected_origin_reset')
        origin = pose if first else self.origin
        raw_delta = [pose[i]-origin[i] for i in range(3)]
        reported_raw = vector(ik.get('raw_tracker_delta_m'), 3, 'ik_raw_delta')
        if any(abs(a-b) > 1e-8 for a,b in zip(raw_delta,reported_raw)):
            raise ValueError('ik_delta_not_from_current_tracker')
        matrix = self.reference['axis_config']['T_robot_axes_xr_axes']
        scaled = [.02*sum(row[i]*raw_delta[i] for i in range(3)) for row in matrix]
        magnitude = math.sqrt(sum(x*x for x in scaled))
        factor = min(1., .01/magnitude) if magnitude else 1.
        expected = [x*factor for x in scaled]
        bounded = vector(ik.get('bounded_delta_m'), 3, 'ik_bounded_delta')
        if math.sqrt(sum(x*x for x in bounded)) > .01+1e-9 or any(abs(a-b) > 1e-8 for a,b in zip(expected,bounded)):
            raise ValueError('ik_translation_limit_or_mapping_mismatch')
        residuals = ik.get('residuals')
        if not isinstance(residuals, dict):
            raise ValueError('missing_ik_residuals')
        for side in ('left','right'):
            r = residuals.get(side)
            if not isinstance(r, dict) or not 0 <= finite(r.get('position_m'), 'position_residual') <= .002 or not 0 <= finite(r.get('rotation_rad'), 'rotation_residual') <= .03:
                raise ValueError('ik_residual_limit')
        return q

    def evaluate(self, packet, ik_result, *, now_ns):
        result = {'input_policy_id': self.policy_id, 'supervised_input_accepted': False,
                  'tracking_valid': None, 'vendor_tracking_valid': None,
                  'tracker_sample_freshness_verified': False,
                  'requires_separate_robot_output_gates': True,
                  'motion_ready': False, 'commands_published': 0,
                  'reason': 'not_evaluated', 'max_session_s': 10., 'state': 'WAIT',
                  **self.initial_fields}
        try:
            if self.reference is None:
                raise ValueError('set_reference_required')
            if self.latched:
                raise ValueError('policy_latched_new_reference_required')
            integer(now_ns, 'local_now')
            seq, receipt, stamp, pose = self._sample(packet, now_ns)
            controls = self._control_state(packet, seq, receipt, stamp)
            first = self.origin is None
            if receipt < self.reference['received_monotonic_ns']:
                raise ValueError('packet_predates_reference')
            if first and receipt-self.reference['received_monotonic_ns'] > 200_000_000:
                raise ValueError('origin_too_late_after_reference')
            if self.started_ns is not None and now_ns-self.started_ns >= 10_000_000_000:
                raise ValueError('supervised_ten_second_window_expired')
            if self.previous is not None:
                old = self.previous
                if packet['device_id'] != old['device_id']:
                    raise ValueError('headset_changed')
                if seq <= old['packet_sequence'] or receipt <= old['local_monotonic_ns'] or stamp <= old['app_timestamp_ns']:
                    raise ValueError('replayed_or_regressed_packet')
                if receipt-old['local_monotonic_ns'] > 200_000_000:
                    raise ValueError('packet_stream_gap')
                old_pose = old['trackers'][0]['xyzw_pose']
                dot = abs(sum(a*b for a,b in zip(pose[3:],old_pose[3:])))
                dot /= math.sqrt(sum(x*x for x in pose[3:])*sum(x*x for x in old_pose[3:]))
                if math.dist(pose[:3],old_pose[:3]) > .1 or 2*math.acos(min(1.,dot)) > .5:
                    raise ValueError('tracker_pose_jump')
            q = self._ik(ik_result, packet, pose, first)
            if first:
                self.origin, self.origin_ns = list(pose), receipt
            self.previous = copy.deepcopy(packet)
            result.update(sequence=seq, local_monotonic_ns=receipt, app_timestamp_ns=stamp,
                          reference_id=self.reference['reference_id'], right_sn=self.sn,
                          calibration_id=self.reference['axis_config']['id'],
                          axes_physically_verified=self.reference['axis_config']['verified'],
                          reference_max_joint_delta_rad=max(abs(a-b) for a,b in zip(q,self.reference['q_rad'])))
            result.update(self._transition(controls, seq, receipt, now_ns))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.latched = True
            result['reason'] = str(exc)
            result['state'] = 'LATCHED'
        return result

    def _active_result(self, receipt, now_ns):
        if self.started_ns is None:
            self.started_ns = receipt
        return dict(supervised_input_accepted=True, reason='accepted_under_operator_supervised_policy',
                    state='ACTIVE', elapsed_s=(now_ns-self.started_ns)/1e9,
                    remaining_s=max(0.,10.-(now_ns-self.started_ns)/1e9))


class SupervisedInputPolicy(_PositionPolicy):
    """Waits for trigger; faults or release after activation require a new reference."""
    policy_id = POLICY_ID
    initial_fields = {'controller_connected_verified': False}

    def _control_state(self, sample, seq, receipt, stamp):
        controller_active = sample['input_mode'] == 1
        if not controller_active and self.started_ns is not None:
            raise ValueError('controller_mode_lost_after_activation')
        enable = sample.get('controller_enable')
        if not isinstance(enable, dict) or type(enable.get('held')) is not bool:
            raise ValueError('right_trigger_state_unavailable')
        if (enable.get('source_id') != 'pico_right_trigger' or enable.get('control') != 'trigger'
                or enable.get('source') != 'same_native_packet.Controller.right'):
            raise ValueError('wrong_controller_enable_source')
        if (integer(enable.get('sequence'), 'enable_sequence') != seq
                or integer(enable.get('local_monotonic_ns'), 'enable_receipt') != receipt
                or integer(enable.get('app_timestamp_ns'), 'enable_app_timestamp') != stamp):
            raise ValueError('controller_enable_packet_mismatch')
        if (enable.get('app_focused') is not True
                or enable.get('controller_input_active') is not controller_active
                or type(enable.get('input_mode')) is not int or enable['input_mode'] != sample['input_mode']
                or enable.get('packet_structure_ok') is not True):
            raise ValueError('controller_enable_focus_mode_or_structure_invalid')
        analog, threshold = finite(enable.get('trigger'), 'right_trigger'), finite(enable.get('threshold'), 'trigger_threshold')
        if not .8 <= threshold <= 1 or not 0 <= analog <= 1:
            raise ValueError('invalid_trigger_threshold_or_analog')
        if enable['held'] is not (controller_active and analog >= threshold):
            raise ValueError('trigger_held_analog_mismatch')
        return enable['held'], controller_active

    def _transition(self, controls, seq, receipt, now_ns):
        held, controller_active = controls
        result = dict(source_id='pico_right_trigger', held=held, enable_sequence=seq)
        if not controller_active:
            result.update(reason='waiting_for_controller', elapsed_s=0., remaining_s=10.)
        elif not held:
            if self.started_ns is not None:
                raise ValueError('right_trigger_released_after_activation')
            result.update(reason='waiting_for_trigger', elapsed_s=0., remaining_s=10.)
        else:
            result.update(self._active_result(receipt, now_ns))
        return result


class TrackerWindowPolicy(_PositionPolicy):
    """Explicitly selected 10-second Tracker window, without controller input.

    The first accepted origin's original receive time starts the window. The
    operator supervises this limited test using the G1 remote; this policy does
    not read or fabricate a controller-held/valid flag. Advancing app timestamps
    cannot distinguish a frozen Tracker pose from normal stationary tracking.
    """
    policy_id = TRACKER_WINDOW_POLICY_ID

    def _control_state(self, sample, seq, receipt, stamp):
        return None

    def _transition(self, controls, seq, receipt, now_ns):
        return dict(source_id='pico_right_tracker', **self._active_result(receipt, now_ns))
