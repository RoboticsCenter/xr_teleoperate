# Headset reference mapping

The current `headset_heading_v1` reference follows headset translation and
horizontal facing. Pitch and roll do not tilt the arm axes; gravity remains up.
Controller and wrist-Tracker sources retain separate calibration and bindings.
See [the dashboard guide](teleop-dashboard.md#跟随头显的参考坐标).

Moving the headset changes the reference origin continuously. Turning the
headset changes the horizontal reference axes. In fixed-reference mode, the
reference captured at START remains fixed. Compare these modes with the same
neutral posture and a clearly identified movement before changing signs or gain.

The worker records `raw_input` at neutral capture and accepted targets, including
world Head/controller poses, same-packet Head pairs, reference mode, selection,
source sequence and timestamps. These fields permit replay of the reference
transform without treating an IK target as a measured robot hand position.

A separate [PICO timing build candidate](../build/pico-controller-timing/package/README.md)
corrects the inspected microsecond/millisecond API argument mismatch. Offline
instruction/argument checks do not establish the installed headset version or
prove physical tracking accuracy. The original operator recordings and session
history are deliberately excluded from this public integration.
