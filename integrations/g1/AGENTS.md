# G1 integration instructions

Read PLAN.md before changing this integration and keep verified decisions and
validation limits current. Run commands from this directory.

Reuse the original Wuji reader and finger mapping described in sources.lock.json.
The external Wuji repository is not an editable part of this integration.
Do not replace its retargeting algorithm.

Keep software/simulation checks separate from hardware verification. Read-only
checks must not instantiate controllers that publish targets or start hand
output services. Follow the active operator's authorization for robot movement,
network changes and service operations. Never publish credentials, personal
calibration, recorded operator sessions or machine runtime configuration.
