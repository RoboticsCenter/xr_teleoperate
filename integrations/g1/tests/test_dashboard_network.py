from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from teleop_dashboard import PicoService


def table(host_hex):
    return ('header\n'
            f'0: 00000000:{63901:04X} 00000000:0000 0A\n'
            f'1: 0100007F:{60061:04X} 00000000:0000 0A\n'
            f'2: {host_hex}:{63901:04X} 0100007F:9999 01\n')


class PicoNetworkStatus(unittest.TestCase):
    def test_old_network_tcp_connection_is_not_reported_on_new_network(self):
        with patch('teleop_dashboard.laptop_host', return_value='10.1.10.62'), patch.object(Path, 'read_text', return_value=table('5C1FA8C0')):
            status = PicoService().status()
        self.assertTrue(status['running'])
        self.assertFalse(status['headset_connected'])
        self.assertIn('10.1.10.62', status['detail'])

    def test_current_ipv4_and_ipv4_mapped_tcp_connections_are_reported(self):
        for raw in ('3E0A010A', '0000000000000000FFFF00003E0A010A'):
            with self.subTest(raw=raw), patch('teleop_dashboard.laptop_host', return_value='10.1.10.62'), patch.object(Path, 'read_text', return_value=table(raw)):
                self.assertTrue(PicoService().status()['headset_connected'])


if __name__ == '__main__':
    unittest.main()
