# Unitree G1 teleoperation dashboard

Local web control for G1 arms and BrainCo Revo2 hands, using PICO wrist Trackers or PICO controllers for arm poses and Wuji gloves for finger poses.

## Current capabilities

- Select left, right, or both arms, with either wrist Trackers or PICO controllers.
- LAUNCH / START / STOP workflow using the existing normal-motion `rt/arm_sdk` adapter.
- Independent Tracker/controller arm calibration, glove personal-model calibration, and open/fist mapping.
- Separate left/right finger following, input diagnostics, robot connection checks, and camera controls.
- PICO installation and distance pages at the configured laptop address.

The implementation reuses the selected `h2/pico-wuji-head-camera` branch geometry/IK and the existing Wuji reader/finger mapping. It does not introduce a new retargeting algorithm. See [the dashboard guide](docs/teleop-dashboard.md) for operation and current hardware-validation limits. Waist posture holding is not independently controlled dynamic torso balance.

## Local access

Run all commands below from this directory (`integrations/g1`).

- Dashboard: <http://127.0.0.1:8025/>
- PICO page: `http://<configured-laptop-ip>:8027/`
- G1 endpoint: the `robot_ip` in local `config/network.json`.

This branch imports the G1 dashboard as an independent integration. The selected
H2 branch source remains pinned in `vendor/xr_h2/SOURCE.json`; the repository's
existing `teleop/` implementation is unchanged. See [import provenance](IMPORT.md).

For controllers, select **PICO 手柄** under **手臂输入**, save, and enable **Head + Controller + Send** in XRoboToolkit. For wrist Trackers, enable **Head + Object + Send** and select the actual left/right serials. Then use the existing input check and LAUNCH → START workflow. Hand input remains Wuji.

## Environment and configuration

This repository saves the integration source. It is not a self-contained image of the commissioned laptop: native SDKs, robot model assets, personal calibration, credentials, and built APKs are local dependencies.

The dashboard uses Python 3.12 with versions recorded in [requirements-dashboard.txt](requirements-dashboard.txt). PICO SDK, Wuji SDK, G1 IK, and robot DDS use their existing separate environments. Their locations and dependencies are described in [the dashboard guide](docs/teleop-dashboard.md), [PICO bring-up](docs/pico-bringup.md), and [the reuse review](docs/xr-h2-branch-reuse.md). Several adapters still use commissioned host paths and device identities; review them before deploying on a different laptop.

Laptop and robot endpoints are loaded from local `config/network.json` (or explicit `G1_LAPTOP_IP` / `G1_ROBOT_IP` environment overrides). See [network configuration](docs/network-config.md).

On a new installation only:

```bash
python3.12 -m venv .venv-dashboard
.venv-dashboard/bin/python -m pip install -r requirements-dashboard.txt
cp config/dashboard.example.json config/dashboard.json
cp config/pico_tracker.example.json config/pico_tracker.json
cp config/network.example.json config/network.json
# Set laptop_ip and robot_ip in config/network.json for your LAN.
```

Do not overwrite existing settings or calibration. The examples leave Tracker serials unbound and automatic hand following disabled. See [local configuration](config/README.md).

With the documented external dependencies present, start the dashboard from an interactive terminal:

```bash
.venv-dashboard/bin/python scripts/teleop_dashboard.py
```

It prompts for the robot SSH password without saving it to Git. Keep only one dashboard instance on the commissioned laptop. For a separate software-only page, use `--simulation --port 8035`; simulation does not establish hardware readiness.

## Source and licensing

- [vendor/xr_h2/SOURCE.json](vendor/xr_h2/SOURCE.json) pins the requested branch and its referenced G1 runtime; the original [upstream license](vendor/xr_h2/LICENSE) is retained.
- [sources.lock.json](sources.lock.json) records the separately maintained Wuji source and audited upstream revisions. It does not install dependencies.
- Wuji reading and finger mapping are imported from `/home/jack-xiang/branco-wuji-teleop/brainco-wuji-teleop`; that external repository is not duplicated here.
- [patches/](patches/) contains local upstream adaptations. Third-party code remains under its original license.

## Verification

Controller support was checked with 127 focused Python tests, 140 UI tests, and browser interaction checks. These verify software behavior, not physical controller-following accuracy or whole-body stability. Individual tests run with `python -m unittest discover -s tests -p 'test_<component>.py'`; UI checks use:

```bash
node --test tests/test_dashboard*_ui.mjs tests/test_pico_distance_ui.mjs
```

The complete test collection includes native SDK/model-dependent checks requiring the separate environments. [PLAN.md](PLAN.md) records the exported implementation status and verification limits. Private session logs and hardware captures remain on the original laptop.

## Documentation

- [Dashboard operation](docs/teleop-dashboard.md)
- [Arm calibration](docs/arm-calibration-web.md) and [glove calibration](docs/glove-calibration-web.md)
- [PICO setup](docs/pico-web-setup.md), [two-Tracker build](docs/pico-two-tracker-build.md), and [distance display](docs/pico-distance-display.md)
- [Hand transport](docs/dashboard-hand-transport.md) and [Wuji reuse](docs/hand-reuse.md)
- [Project continuity](PLAN.md), [contributor instructions](AGENTS.md), and [initial bring-up history](docs/initial-bringup-history.md) (historical examples, not current device state)

Runtime logs, recordings, build products, signing keys, local settings, and personal calibration files are ignored by Git. Historical documentation may reference diagnostics kept only on the laptop.
