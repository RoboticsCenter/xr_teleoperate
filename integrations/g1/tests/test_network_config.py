import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from network_config import DEFAULTS, laptop_host, load_network, robot_host


class NetworkConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'config').mkdir()

    def save(self, value):
        (self.root / 'config/network.json').write_text(json.dumps(value))

    def test_existing_defaults_without_configuration(self):
        self.assertEqual(load_network(self.root, environ={}), DEFAULTS)

    def test_local_endpoints_and_environment_override(self):
        self.save({'laptop_ip': '10.1.10.62', 'robot_ip': '10.1.10.75'})
        self.assertEqual(load_network(self.root, environ={})['robot_ip'], '10.1.10.75')
        with patch.dict('os.environ', {'G1_LAPTOP_IP': '10.1.10.63', 'G1_ROBOT_IP': '10.1.10.76'}):
            self.assertEqual(laptop_host(self.root), '10.1.10.63')
            self.assertEqual(robot_host(self.root), '10.1.10.76')
        self.assertEqual(json.loads((self.root / 'config/network.json').read_text())['robot_ip'], '10.1.10.75')

    def test_bad_config_and_endpoint_rejected(self):
        for value in ([], {'password': 'not-accepted'}, {'robot_ip': None},
                      {'robot_ip': '10.1.10.75;echo bad'}, {'robot_ip': 'https://10.1.10.75'},
                      {'laptop_ip': '0.0.0.0'}, {'laptop_ip': '127.0.0.1'},
                      {'laptop_ip': '224.0.0.1'}, {'laptop_ip': '255.255.255.255'}):
            with self.subTest(value=value):
                self.save(value)
                with self.assertRaises(ValueError):
                    load_network(self.root, environ={})

    def test_invalid_environment_override_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            load_network(self.root, environ={'G1_ROBOT_IP': ''})


if __name__ == '__main__':
    unittest.main()
