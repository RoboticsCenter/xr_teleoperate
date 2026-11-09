#!/usr/bin/env bash
# Run the whole VR teleop stack ON THE G1's PC2 (no Ethernet tether to a laptop).
#
#   Quest  --Wi-Fi-->  PC2 (teleimager-server + teleop_hand_and_arm.py)  --eth0-->  robot
#
# Usage (from any machine on the same Wi-Fi):
#   ssh -t unitree@<PC2 Wi-Fi IP>
#   bash ~/xr_teleoperate/teleop/start_teleop_pc2.sh            # arms + joystick walking (--motion)
#   bash ~/xr_teleoperate/teleop/start_teleop_pc2.sh --no-motion # arms only (debug mode, legs released!)
#   bash ~/xr_teleoperate/teleop/start_teleop_pc2.sh --camera-only
# Then on the Quest open  https://<PC2 Wi-Fi IP>:8012/?ws=wss://<PC2 Wi-Fi IP>:8012
# Env: PC2_PASS (sudo password, default 123), WIFI_IF (default wlan0)
set -uo pipefail

PC2_PASS="${PC2_PASS:-123}"
WIFI_IF="${WIFI_IF:-wlan0}"
TV_PY="$HOME/miniconda3/envs/tv/bin/python"
TELEIMAGER_BIN="$HOME/miniconda3/envs/teleimager/bin/teleimager-server"
TELEOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOTION="--motion"; CAMERA_ONLY=0
for a in "$@"; do
  case "$a" in
    --no-motion) MOTION="" ;;
    --camera-only) CAMERA_ONLY=1 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done
sudo_() { echo "$PC2_PASS" | sudo -S -p "" "$@"; }

echo "== [1/4] Switching off Unitree video_hub_pc4 (it grabs the head camera on boot)"
timeout 40 "$TV_PY" - <<'EOF'
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
ChannelFactoryInitialize(0, "eth0")
c = RobotStateClient(); c.SetTimeout(5.0); c.Init()
code = c.ServiceSwitch("video_hub_pc4", False)
_, lst = c.ServiceList()
print(f"   switch code={code}, status={ {s.name: s.status for s in lst or []}.get('video_hub_pc4') } (1 = stopped)")
EOF

echo "== [2/4] Waiting for the camera to be free"
pkill -x teleimager-serv && sleep 2   # our own previous server
for i in $(seq 1 20); do
  sudo_ fuser /dev/video* >/dev/null 2>&1 || { echo "   camera free"; break; }
  [ "$i" -eq 20 ] && { echo "   ERROR: camera still in use:"; sudo_ fuser -v /dev/video*; exit 1; }
  sleep 1
done
lsusb | grep -q 8086: || { echo "   ERROR: RealSense not on USB. Re-seat the head camera cable."; exit 1; }

start_server() {
  pkill -x teleimager-serv && sleep 2
  (cd ~/teleimager && setsid nohup "$TELEIMAGER_BIN" > ~/teleimager_server.log 2>&1 < /dev/null &)
  for _ in $(seq 1 15); do ss -ltn | grep -q ":55555 " && break; sleep 1; done
}
count_frames() {
  (cd "$TELEOP_DIR" && timeout 20 "$TV_PY" -u - <<'EOF'
import time, os
from teleimager.image_client import ImageClient
c = ImageClient(host="127.0.0.1", request_bgr=True)
n, t0 = 0, time.time()
while time.time() - t0 < 3:
    f = c.get_head_frame()
    if f is not None and f.bgr is not None: n += 1
    time.sleep(0.01)
print(n, flush=True); os._exit(0)
EOF
  ) 2>/dev/null | tail -1
}

echo "== [3/4] Starting teleimager-server and checking frames"
start_server
N=$(count_frames); N=${N:-0}
if [ "$N" -lt 10 ]; then
  echo "   only $N frames -> resetting the camera USB and retrying"
  pkill -x teleimager-serv; sleep 1
  D=$(dirname "$(grep -l 252943061790 /sys/bus/usb/devices/*/serial | head -1)")
  sudo_ sh -c "echo 0 > $D/authorized; sleep 2; echo 1 > $D/authorized"; sleep 5
  start_server
  N=$(count_frames); N=${N:-0}
fi
[ "$N" -ge 10 ] || { echo "   ERROR: no camera frames ($N). Log:"; tail -20 ~/teleimager_server.log; exit 1; }
echo "   camera OK: $N frames in 3 s (log: ~/teleimager_server.log)"
[ "$CAMERA_ONLY" -eq 1 ] && exit 0

IP=$(ip -4 -o addr show "$WIFI_IF" | awk '{print $4}' | cut -d/ -f1)
echo "== [4/4] Starting teleop ${MOTION:-(arms only, debug mode)}"
echo
echo "   Quest URL:  https://${IP:-<PC2 Wi-Fi IP>}:8012/?ws=wss://${IP:-<PC2 Wi-Fi IP>}:8012"
echo "   Order: wait for 'websocket is connected' -> take the start pose -> press r.  q = quit"
echo
cd "$TELEOP_DIR"
# Unbuffered output avoids inheriting a locked stdout buffer when Vuer forks
# while the background logging thread is writing to the SSH terminal.
exec "$TV_PY" -u teleop_hand_and_arm.py --arm=G1_29 --input-mode=controller $MOTION \
  --network-interface=eth0 --img-server-ip=127.0.0.1 --headless
