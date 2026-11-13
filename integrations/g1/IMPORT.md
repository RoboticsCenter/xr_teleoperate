# Import provenance

This branch adds `integrations/g1` on top of RoboticsCenter/xr_teleoperate main
at `d0de7e681523cef2e78f7599921bba14368ef1ed`. Existing upstream files are unchanged.

The integration is exported from the independently maintained unitree-g1 working
tree at commit `dd35f876b606c7640e7931fccbc1a2aba77e6793`, including its pending
camera SSH keepalive fix and robot-connection test isolation fix. It includes the
full dashboard implementation, not just those two pending changes.

The operator-selected source is kevin-roboticscenter/xr_teleoperate branch
`h2/pico-wuji-head-camera`, pinned at
`053ba99af55c1d8ddc6b87918cb0fa234625886e`. That branch's referenced G1 runtime is
pinned separately. `vendor/xr_h2/SOURCE.json` records every vendored source hash;
licenses are retained. The Wuji reader/finger mapping remains an external
integration and is not replaced or redistributed by this export.

Only tracked source files were copied from the working tree. Local configuration,
credentials, host keys, personal models/calibration, recordings, native binaries
and built APKs are excluded. PLAN.md is a new implementation summary; the private
session history was not copied. README.md and network instructions describe
configuration rather than claiming old laptop addresses are current. Historical
bring-up and mapping documents are concise technical summaries, and the source
manifest omits the observed-robot installation record. Original commissioned
source paths/device defaults still require review before a different deployment;
this export does not refactor the established reader or mapping algorithms.

## Validation

The isolated integration was checked on November 13, 2026:

- 181 unique focused Python tests passed across network configuration, robot
  connection, glove input, hand transport, arm worker, tracker input, arm
  calibration, dashboard API and camera. The initial run had one fixture failure:
  a worker test depended on an excluded private configuration file. Supplying
  explicit synthetic tracker configuration fixed it; all 27 worker tests passed
  on rerun, with the other 154 already passing. No tests were skipped.
- 156 JavaScript UI tests passed.
- Vendored source hashes remain unchanged. Diff whitespace checks passed for
  integration-owned files; upstream vendor files and patch context retain their
  original bytes, including existing whitespace.

The worker test fixture is the only code difference from the copied source
snapshot beyond its already-pending changes. The original installation remains
unchanged. These tests use the existing laptop's installed Wuji source and do not
prove that an otherwise empty machine has the required external dependencies.
This import does not restart services or perform robot movement. Native SDK/model
availability and physical teleoperation must be verified for each deployment.
