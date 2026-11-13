"""Conservative local age bounds for SSH state; this is not clock synchronization."""
import math


class FeedbackTimeBounds:
    def __init__(self, max_age_s=.1):
        if not math.isfinite(max_age_s) or not 0 < max_age_s <= .5:
            raise ValueError('max age must be in (0, .5]')
        self.max_age_ns = int(max_age_s * 1e9)
        self.clock_id, self.last_request, self.previous = None, -1, {}

    def convert(self, reply, rid, sent_ns, received_ns):
        if any(type(v) is not int or v < 0 for v in (rid, sent_ns, received_ns)) or received_ns < sent_ns:
            raise ValueError('invalid local request timing')
        if reply.get('type') != 'feedback_snapshot' or reply.get('request_id') != rid or rid <= self.last_request:
            raise ValueError('wrong or repeated snapshot reply')
        cid, remote_now = reply.get('clock_id'), reply.get('server_send_monotonic_ns')
        if not isinstance(cid, str) or not cid.startswith('linux-monotonic:') or type(remote_now) is not int:
            raise ValueError('missing remote clock identity/time')
        if self.clock_id is not None and self.clock_id != cid:
            raise ValueError('remote clock changed')
        self.clock_id, self.last_request = cid, rid
        result = {'request_id': rid, 'roundtrip_s': (received_ns-sent_ns)/1e9,
                  'source_clock_id': cid, 'streams': {}, 'commands_published': 0}
        for topic, record in reply.get('streams', {}).items():
            if record is None or record.get('error') or record.get('payload') is None:
                result['streams'][topic] = {'status': 'missing_or_invalid', 'source': record}
                continue
            seq, remote_received = record.get('sequence'), record.get('received_monotonic_ns')
            if type(seq) is not int or seq < 1 or type(remote_received) is not int or not 0 <= remote_received <= remote_now:
                raise ValueError('invalid remote sample timing')
            remote_age = remote_now - remote_received
            age_bound = received_ns - sent_ns + remote_age
            previous = self.previous.get(topic)
            if previous is not None and seq < previous['sequence']:
                raise ValueError('sample sequence regressed')
            if previous is not None and seq == previous['sequence']:
                if record != previous['source']:
                    raise ValueError('cached sample changed without advancing sequence')
                estimated_ns = previous['conservative_received_monotonic_ns']
                age_bound = max(age_bound, received_ns-estimated_ns)
            else:
                if previous is not None and remote_received <= previous['source']['received_monotonic_ns']:
                    raise ValueError('new sample did not advance remote receipt time')
                estimated_ns = sent_ns - remote_age
            entry = {'sequence': seq, 'source': record,
                     'conservative_received_monotonic_ns': estimated_ns,
                     'age_upper_bound_s': age_bound/1e9,
                     'status': 'fresh_bound' if 0 <= age_bound <= self.max_age_ns and estimated_ns >= 0 else 'stale_bound'}
            self.previous[topic] = entry
            result['streams'][topic] = entry
        return result
