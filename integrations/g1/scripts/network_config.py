"""Validated local LAN endpoints shared by dashboard, workers and PICO pages.

The local config is ignored by Git. Environment overrides permit isolated tests
and explicit service configuration without rewriting commissioned settings.
"""
import ipaddress
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {'laptop_ip': '192.168.31.92', 'robot_ip': '192.168.31.65'}
ENVIRONMENT = {'laptop_ip': 'G1_LAPTOP_IP', 'robot_ip': 'G1_ROBOT_IP'}


def load_network(root=None, *, environ=None):
    path = Path(ROOT if root is None else root) / 'config/network.json'
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        saved = {}
    if not isinstance(saved, dict) or set(saved) - set(DEFAULTS):
        raise ValueError('network.json accepts only laptop_ip and robot_ip')
    values = {**DEFAULTS, **saved}
    environment = os.environ if environ is None else environ
    for key, variable in ENVIRONMENT.items():
        if variable in environment:
            values[key] = environment[variable]
        try:
            if not isinstance(values[key], str):
                raise ValueError('not text')
            address = ipaddress.IPv4Address(values[key])
            if address.is_unspecified or address.is_multicast or address.is_loopback or int(address) == 0xffffffff:
                raise ValueError('not a LAN endpoint')
            values[key] = str(address)
        except (ValueError, TypeError):
            raise ValueError(f'{key} must be a unicast LAN IPv4 address') from None
    return values


def laptop_host(root=None):
    return load_network(root)['laptop_ip']


def robot_host(root=None):
    return load_network(root)['robot_ip']
