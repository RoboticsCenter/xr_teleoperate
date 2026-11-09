#!/usr/bin/env bash
# Start the G1 head-camera stream (teleimager-server on PC2) after a robot reboot.
#
# What it does:
#   1. Switches off Unitree's video_hub_pc4 service (it grabs the RealSense on every boot).
#   2. Waits until the camera is free.
#   3. (Re)starts teleimager-server on PC2 in the background (log: PC2:~/teleimager_server.log).
#   4. Pulls frames on this PC to confirm the stream works.
#
# Usage:  bash start_camera_server.sh [network_interface]
# Env:    PC2_PASS (default 123), PC2_HOST (default 192.168.123.164), TV_PYTHON
# Undo:   bash start_camera_server.sh --videohub-on   (gives the camera back to the Unitree app)
set -euo pipefail

IFACE="enx6c1ff772bfe8"
VIDEOHUB_STATE="off"
for a in "$@"; do
  case "$a" in
    --videohub-on) VIDEOHUB_STATE="on" ;;
    *) IFACE="$a" ;;
  esac
done
PC2_HOST="${PC2_HOST:-192.168.123.164}"
PC2_PASS="${PC2_PASS:-123}"
TV_PYTHON="${TV_PYTHON:-$HOME/miniconda3/envs/tv/bin/python}"
TELEOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Password-based SSH without sshpass: OpenSSH's askpass hook.
ASKPASS="$(mktemp)"
trap 'rm -f "$ASKPASS"' EXIT
printf '#!/bin/sh\necho "%s"\n' "$PC2_PASS" > "$ASKPASS"; chmod 700 "$ASKPASS"
pc2() {
  SSH_ASKPASS="$ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}" setsid \
    ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "unitree@$PC2_HOST" \
    "PC2_PASS='$PC2_PASS'; $1" < /dev/null
}

echo "== [1/4] Turning video_hub_pc4 $VIDEOHUB_STATE"
timeout 40 "$TV_PYTHON" - "$IFACE" "$VIDEOHUB_STATE" <<'EOF'
import sys
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
NAME = "video_hub_pc4"
ChannelFactoryInitialize(0, sys.argv[1])
c = RobotStateClient(); c.SetTimeout(5.0); c.Init()
code = c.ServiceSwitch(NAME, sys.argv[2] == "on")
_, lst = c.ServiceList()
status = {s.name: s.status for s in lst or []}.get(NAME)
print(f"   switch code={code}, status={status} (0 = running, 1 = stopped)")
EOF
if [ "$VIDEOHUB_STATE" = "on" ]; then
  pc2 'pkill -x teleimager-serv && echo "   stopped teleimager-server" || true'
  echo "Done: the Unitree app has the camera again."; exit 0
fi

echo "== [2/4] Waiting for the camera to be free on PC2"
pc2 '
for i in $(seq 1 20); do
  if ! echo "$PC2_PASS" | sudo -S -p "" fuser /dev/video* >/dev/null 2>&1; then echo "   camera free"; break; fi
  [ $i -eq 20 ] && { echo "   ERROR: camera still in use:"; echo "$PC2_PASS" | sudo -S -p "" fuser -v /dev/video*; exit 1; }
  sleep 1
done
lsusb | grep -q 8086: || { echo "   ERROR: RealSense not on USB. Re-seat the head camera cable."; exit 1; }
VID=$(grep -A12 "^head_camera:" ~/teleimager/cam_config_server.yaml | awk "/video_id:/{print \$2}")
[ -e /dev/video$VID ] || { echo "   ERROR: /dev/video$VID (head_camera video_id) missing"; ls /dev/video*; exit 1; }
echo "   using /dev/video$VID"
'

echo "== [3/4] (Re)starting teleimager-server on PC2"
pc2 '
pkill -x teleimager-serv && sleep 2 || true
cd ~/teleimager
setsid nohup ~/miniconda3/envs/teleimager/bin/teleimager-server > ~/teleimager_server.log 2>&1 < /dev/null &
for i in $(seq 1 15); do
  ss -ltn | grep -q ":55555 " && ss -ltn | grep -q ":60000 " && break
  sleep 1
done
pgrep -ax teleimager-serv >/dev/null || { echo "   ERROR: server exited:"; tail -20 ~/teleimager_server.log; exit 1; }
ss -ltn | grep -q ":55555 " || { echo "   ERROR: head camera stream (port 55555) not up:"; tail -20 ~/teleimager_server.log; exit 1; }
echo "   server running (pid $(pgrep -x teleimager-serv | head -1)), log: ~/teleimager_server.log"
'

echo "== [4/4] Checking the stream from this PC"
cd "$TELEOP_DIR"
timeout 30 "$TV_PYTHON" -u - "$PC2_HOST" <<'EOF'
import sys, time, os
from teleimager.image_client import ImageClient
c = ImageClient(host=sys.argv[1], request_bgr=True)
n, t0, shape = 0, time.time(), None
while time.time() - t0 < 3:
    f = c.get_head_frame()
    if f is not None and f.bgr is not None:
        n += 1; shape = f.bgr.shape
    time.sleep(0.01)
print(f"   received {n} frames in 3 s, shape {shape}", flush=True)
os._exit(0 if n > 0 else 1)
EOF

echo
echo "Camera stream is up. Now (re)start the teleop program, e.g.:"
echo "  python teleop_hand_and_arm.py --arm=G1_29 --input-mode=controller --motion --network-interface=$IFACE"
