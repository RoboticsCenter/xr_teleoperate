#!/usr/bin/env python3
"""Read network state and listen for DDS discovery without robot command publishers."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import socket
import subprocess
import time


def ip_json(*args):
    result = subprocess.run(
        ['ip', '-j', *args], capture_output=True, text=True, timeout=5, check=True
    )
    return json.loads(result.stdout)


def read_link_value(interface, name):
    try:
        return (Path('/sys/class/net') / interface / name).read_text().strip()
    except OSError:
        return None


def listen_discovery(address, seconds):
    observations = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', 7400))
        # Linux IP_MULTICAST_ALL=0 excludes groups joined by sockets on other NICs.
        sock.setsockopt(socket.IPPROTO_IP, 49, 0)
        sock.setsockopt(
            socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
            socket.inet_aton('239.255.0.1') + socket.inet_aton(address),
        )
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(remaining, 1.0))
            try:
                packet, (host, port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            if packet.startswith(b'RTPS'):
                item = observations.setdefault(host, {'packets': 0, 'source_ports': set()})
                item['packets'] += 1
                item['source_ports'].add(port)
    return [
        {'address': host, 'packets': item['packets'],
         'source_ports': sorted(item['source_ports'])}
        for host, item in sorted(observations.items())
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='enx6c1ff762d908')
    parser.add_argument('--seconds', type=float, default=5.0)
    parser.add_argument('--probe-known', action='store_true',
                        help='Send one ping per previously observed G1-network address.')
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 30:
        parser.error('--seconds must be finite, greater than 0 and at most 30')
    try:
        socket.if_nametoindex(args.interface)
    except OSError:
        parser.error(f'No such interface: {args.interface}')
    interfaces = ip_json('address', 'show', 'dev', args.interface)
    addresses = [item['local'] for dev in interfaces for item in dev.get('addr_info', [])
                 if item['family'] == 'inet']
    result = {
        'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'interface': args.interface, 'interface_state': interfaces,
        'carrier': read_link_value(args.interface, 'carrier'),
        'speed_mbps': read_link_value(args.interface, 'speed'),
        'routes': ip_json('route'), 'ping_results': [], 'dds_discovery': [],
        'limitations': [
            'ARP/RTPS observations do not authenticate or identify a device model.',
            'Cached neighbors may predate this run; unanswered ping does not prove offline.',
            'Only DDS multicast discovery at 239.255.0.1:7400 is observed.',
            'Robot state topics, hand identity and motion readiness are not verified.',
        ],
    }
    if args.probe_known:
        for host in ('192.168.123.120', '192.168.123.161', '192.168.123.164'):
            # With multiple local subnets, select the matching source address;
            # binding just the interface may choose its unrelated first address.
            source = next((a['local'] for d in interfaces for a in d.get('addr_info', [])
                           if a['family'] == 'inet' and ipaddress.ip_address(host) in
                           ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network),
                          args.interface)
            proc = subprocess.run(
                ['ping', '-n', '-I', source, '-c', '1', '-W', '1', host],
                capture_output=True, text=True, timeout=4,
            )
            result['ping_results'].append({
                'address': host, 'source': source, 'returncode': proc.returncode,
                'stdout': proc.stdout, 'stderr': proc.stderr,
            })
    result['neighbors'] = ip_json('neigh', 'show', 'dev', args.interface)
    if addresses:
        try:
            result['dds_discovery'] = listen_discovery(addresses[0], args.seconds)
        except OSError as exc:
            result['discovery_error'] = str(exc)
    else:
        result['discovery_error'] = 'No local IPv4 address; cannot join multicast.'
    result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
