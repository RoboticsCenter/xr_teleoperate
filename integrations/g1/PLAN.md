# G1 integration continuity

This export contains the G1 dashboard implementation as of November 13, 2026.
Private operator conversations, session logs, credentials, live network settings,
and personal calibration were not copied into this branch.

## Implementation

- PICO wrist Trackers or controllers drive either or both arms.
- Headset-relative control follows the headset position and horizontal facing;
  fixed-reference control and source-specific calibration remain available.
- Wuji input and finger mapping reuse the separately maintained original source.
- The webpage supports glove calibration, per-hand following, robot connection,
  PICO setup and distance display, and a laptop camera preview.
- G1 uses the existing normal-motion arm SDK adapter; waist holding is not a
  separate dynamic balance controller.
- Latest included fixes add camera SSH keepalives/reconnection coverage and
  isolate robot-connection test fixtures from machine network configuration.

## Source and configuration

Read IMPORT.md, README.md, vendor/xr_h2/SOURCE.json and sources.lock.json.
Do not replace the original Wuji reader/mapping or modify its external repository.
Native SDKs, model assets, environment paths and hardware bindings still require
machine setup. Do not treat this export as a ready-to-run robot image.

## Validation and next steps

Software validation is recorded in IMPORT.md. Previous hardware observations
belong to the original installation and do not certify a new deployment.
Configure local endpoints, verified SSH identity and input bindings before use;
retain existing personal calibration. Read-only checks must not create a control
publisher or start a hand output service. Robot movement and service changes
require authorization in the active operator session.
