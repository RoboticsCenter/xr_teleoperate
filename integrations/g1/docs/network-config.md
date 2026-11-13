# Teleoperation network configuration

Run the dashboard and PICO page from `integrations/g1`. Endpoints are centralized
in the local, Git-ignored `config/network.json`. Copy `config/network.example.json`
on a new installation, then set `laptop_ip` and `robot_ip` for your actual network.
Do not overwrite an existing installation's settings or calibration.

`G1_LAPTOP_IP` and `G1_ROBOT_IP` override the file for an explicit service or test.
The standalone PICO page also accepts `--host`. Its installation page is
`http://<laptop_ip>:8027/`; the distance page is `/distance`. The local dashboard is
`http://127.0.0.1:8025/`. Keep the PICO and laptop reachable on the same network.

After changing endpoints, reload the relevant dashboard/PICO processes during an
operator-approved interruption. Merely changing the laptop IP does not update an
already-running remote worker. Preserve saved arm/glove calibration and source
bindings when changing network settings.

## Robot identity and Ethernet

The commissioned G1 PC2 wired address is `192.168.123.164`. For a direct Ethernet
link, the laptop needs a compatible address, for example `192.168.123.20/24`, on
the actual connected Ethernet interface. Preserve other required addresses and
the Wi-Fi default route. Do not overwrite the robot's internal Ethernet setup.
A runtime-only NetworkManager address can disappear after a cable disconnection;
check the route again after reconnecting.

Verify physical carrier, route/source address, reachability and the saved SSH
host identity. An activated NetworkManager profile is not proof of a cable link.
The adapter names and glove bindings in historical documents are examples from
one installation and can change after swapping cables or adapters.

Robot SSH uses `StrictHostKeyChecking=yes`, a locally verified
`diagnostics/g1-known_hosts` file and the original `192.168.123.164` host-key alias.
The host-key file is deliberately excluded from this branch. An IP change does
not justify accepting an unverified device key. Credentials are supplied through
the interactive/anonymous-input workflow, not repository files or command arguments.

## Glove routes and PICO input

Wuji gloves require their own working Ethernet paths. Identify the connected
glove by its exact SDK serial rather than by the old adapter profile name. If two
adapters share a subnet, retain explicit per-device destination routes so each
glove is reached through the adapter it is actually connected to. Removing the
robot management cable does not remove the need for glove input connections.

For controller arm input, enable Head + Controller + Send in XRoboToolkit;
for wrist Trackers, enable Head + Object + Send. Keep the headset awake and the
application in the foreground. A TCP connection is not proof of fresh pose input.

## Recovery and validation limits

Robot heartbeat and camera SSH transports use keepalives. The camera closes a
failed SSH child and retries using its existing reconnect path. It reads the
robot's existing VideoHub service rather than opening a competing RealSense
stream. Live frame/pose/hand-feedback ages determine readiness; historical frame
counts and FPS do not prove current activity.

Network checks do not start arm or finger following. Full readiness also needs
fresh PICO and glove data, valid mappings and fresh robot feedback. Link-level
checks or software tests alone are not a physical teleoperation validation.
