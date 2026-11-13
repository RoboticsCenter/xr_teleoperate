# Local settings

Only `*.example.json` templates are versioned. The running laptop's configuration and personal calibration files remain local and are ignored by Git.

On a **new installation only**, copy `dashboard.example.json` to `dashboard.json` and `pico_tracker.example.json` to `pico_tracker.json`. Do not overwrite an existing installation's settings. The templates disable automatic hand following and leave Tracker serial numbers unbound.

Select the arm input source and bind Trackers in the dashboard. PICO controllers do not need Tracker serials. The dashboard saves Tracker and controller arm calibration separately, plus each glove's own calibration. Create personal glove models using the documented calibration process; do not copy another operator's model.

SSH credentials are entered at dashboard startup or passed through anonymous stdin, never stored in these files.

Network endpoints are configured separately in `network.json`, using `network.example.json` as a template. Set `laptop_ip` to the laptop's LAN IPv4 address and `robot_ip` to the verified G1 address, then reload the dashboard and PICO page services. `G1_LAPTOP_IP` and `G1_ROBOT_IP` explicitly override these values for a process. The saved robot SSH host-key identity is retained when its IP changes. See [network configuration](../docs/network-config.md).
