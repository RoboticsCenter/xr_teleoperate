# Wuji doctor 只读审查与左手单次结果

2026-11-11。完成本机 CLI 帮助、官方同版本文档与公开仓库审查后，主代理审阅并授权一次 `default + WG1JA06260822033` 限时检查。**本次左手五指 EMF 断线检查全部通过**；这不代表跟踪置信度已经恢复，也不代表完整 HMS 健康验收通过。没有执行主动诊断、运动、参数写入、校准或固件升级，没有失败重试。

## 实机结果：17:28:47 PST 开始，单次 5.916 秒

原始证据：[元数据与完整报告](../diagnostics/wuji-doctor-default-2026-11-11.json)、[stdout 原文](../diagnostics/wuji-doctor-default-2026-11-11.stdout.json)、[stderr 原文](../diagnostics/wuji-doctor-default-2026-11-11.stderr.log)。

| 项目 | 实测结果 | 能得出的结论 |
| --- | --- | --- |
| 执行 | exit 0，5.916 秒，stderr 空，无超时，无重试 | 一次 default 诊断正常完成 |
| 目标身份 | 唯一设备节点 `wuji_glove: WG1JA06260822033` | 报告目标与已确认左手 SN 一致；本轮没有另读身份参数 |
| EMF 总项 | `emf_disconnect: pass`，五指均 normal | 本次断线检测未报告线圈异常 |
| 各指 | Thumb、Index、Middle、Ring、Pinky 全部 pass/normal | 没有某个手指的 RX/TX 故障提示；报告不含独立 RX/TX 数值或原始线圈波形 |
| 整体 verdict | warn | 不能把 exit 0 等同所有检查均通过 |
| 触觉 | 原始报告为小指 2 个点、1 行的参考性警告 | 佩戴/按压状态未知，测试条件未确认，不能据此判定硬件损坏 |
| 环境 | 多网卡警告；CLI/SDK/Studio 版本自检 skip | 环境工具定位不完整，不表示上述已实测的数据流不存在 |

JSON 顶层 `device` 为 `all`，这是 default 项的总体范围描述；实际只出现上述唯一目标设备节点，没有其他 SN 的诊断结果。此输出不含连接 transport，不能据报告推断一定是直连或排除 Zenoh 桥接。

CLI 路径不在 PATH，环境自检尝试查找其命令时报告未找到；实际程序是通过下述绝对路径运行。自检环境也不是此前隔离的 Python SDK 环境，因此 `wuji_sdk 未安装` 的 skip 不能推翻此前 SDK 2026.11.9 的实采结果。没有为消除这些提示修改 PATH、安装软件或再跑诊断。

## 本机版本与可用参数

| 项目 | 本次读取 |
| --- | --- |
| 可执行文件 | `/home/jack-xiang/.local/opt/wuji-cli/2026.10.26/wuji` |
| 版本 | `2026.10.26` |
| 内嵌 build commit | `fd337026cbd69d49247644db19ba122b8e2146b9` |
| target / profile | `x86_64-unknown-linux-gnu` / `release` |
| 二进制 SHA-256 | `c589bc64bee9a5802033c145d4fbef42ee6c38a027c4b171bb11a55840e7976b` |
| 文件类型 | stripped Linux x86-64 ELF |

`doctor --help` 语法是 `wuji doctor [OPTIONS] [ITEM]`；参数包括 `--list`、`--sn`、`--address`、`--handedness`、`--verbose`、`--yes`、`--help` 和全局 `--json/--jsonl`。省略 ITEM 等于 `default`。

本机帮助没有 `--tactile none`、仅 EMF 选择器、`--timeout`、`--duration` 或 `--count`。不能编造参数跳过触觉或限制内部采样时间。本轮先执行的 `--list --json` 是帮助明确说明的本地无连接查询，只列出两项：

| item | 设备范围 | 本机声明 |
| --- | --- | --- |
| `default` | all | 环境、发现及全部只读设备检查；没有 `effects` 字段 |
| `thumb_opposition` | wuji_hand_2 | 主动对指；`effects` 为 `moves_device`、`needs_operator` |

官方同版本 [doctor 说明文件](https://github.com/wuji-technology/wuji-cli/blob/5b11537b9cb3d80de5462fcf57964313eb3a41ec/skills/wuji-cli-doctor/SKILL.md) 明确区分两项：default 只读；主动对指项会修改 effort limit、MIT gains 并驱动设备。本轮明确选 default，不传 `--yes`，不执行面向 Wuji Hand 2 的主动项。它也不适用于本项目的 BrainCo 手。

## 只读范围、触觉条件与 HMS 版本

官方 [健康诊断说明](https://docs.wuji.tech/docs/zh/wuji-cli/latest/doctor) 将 default 描述为采样约 100 帧，检查 EMF 断线及触觉异常；没有要求用户按压、改变激励或执行标定。触觉应脱下、静置且无按压，否则可能误报。此次该条件未被现场确认，所以保留原始触觉报告而不给出硬件故障结论。

“只读”在这里指设备配置和控制目标不被改变。命令仍需建立会话、订阅数据，可能发生发现通信，并写入主机操作日志；不是完全被动抓包，也不是主机文件系统无写入。公开文档和本机列表足以支持这一明确限定的只读候选，不因核心实现未公开而自动阻塞。

**左手实读固件 1.0.0 不满足官方设备 HMS/故障上报的最低要求。** [2026-10-26 发布说明](https://docs.wuji.tech/docs/en/release-notes#october-2026) 列明 Wuji Glove 的设备故障上报要求固件至少 **1.0.4**。此前日志中的 `without device hms_codes` 与此限制吻合；缺少 HMS 不是设备报告零故障。本轮没有为补齐 HMS 升级固件。

[2026-11-10 发布说明](https://docs.wuji.tech/docs/en/release-notes#november-2026) 说明新 SDK 可在没有设备 HMS 流时独立生成低 EMF 置信度告警。因此需要区分：Python SDK 2026.11.9 的置信度日志、本次旧 CLI 的采样式断线判断、设备原生 HMS。此次 EMF pass 只更新第二项，**不能覆盖 17:23 原过滤器接受 0/361 帧的结果**，也不构成降低原过滤阈值的理由。

## 已执行的精确命令

```bash
env WUJI_NO_UPDATE_CHECK=1 WUJI_TELEMETRY_DISABLE=1 \
  timeout --signal=INT --kill-after=3s 15s \
  /home/jack-xiang/.local/opt/wuji-cli/2026.10.26/wuji \
  doctor default --sn WG1JA06260822033 --verbose --json </dev/null
```

通过 Python subprocess 以相同 argv、环境覆盖和 DEVNULL stdin 启动，stdout/stderr 分开保存；原始 argv、起始 UTC 时间、时长和退出码在元数据中。超时设计为 15 秒发送 INT，再等 3 秒仍未退出则结束进程，约 18 秒上限；本次自行在 5.916 秒结束，没有触发终止。

`WUJI_NO_UPDATE_CHECK=1` 关闭 CLI 后台更新提示，不保证 doctor 的显式软件检查完全不联网；`WUJI_TELEMETRY_DISABLE=1` 是官方支持的使用数据关闭方式，也不等于禁止正常操作日志。没有运行 update、set、calib、主动 item 或任何其他修复建议。

`--sn` 限制诊断身份，不是禁用发现的开关。同版本 [CLI 基础说明](https://github.com/wuji-technology/wuji-cli/blob/5b11537b9cb3d80de5462fcf57964313eb3a41ec/skills/wuji-cli-base/SKILL.md) 说明每条命令独立连接/操作/断开，直连被占用时可回退到只读 Zenoh。本次其他代理暂停采样，没有结束他人程序。若未来要求连发现流量也不能产生，现有 CLI 文档不能提供这个保证，不能靠换成 `--address` 就宣称已满足。

## 公开实现与固定版本依据

官方 [v2026.10.26 release](https://github.com/wuji-technology/wuji-cli/releases/tag/v2026.10.26) 对应公开文档仓库 commit `5b11537b9cb3d80de5462fcf57964313eb3a41ec`。递归树共 27 个条目，包含 README、安装脚本和技能文档，没有 doctor 的 Rust 实现或 Cargo 工程；公开文档 commit 与二进制内嵌 build commit 分别记录，不能声称二者已逐行核对。

同版本 doctor 说明 SHA-256：`3c54521f3c6db2f2b237ffe44b0728ea79a486907e9c4252978175e80b3c0e5a`，metadata version 为 `2026.10.26`；在线 latest 已到 `2026.11.9`。本轮参数依据是本机实际帮助和同版本文件。上述公开 SKILL.md 作为版本文档读取，未安装/更新技能，也未自动执行其中其他流程。

本次不是协议级逐调用的无写审计，执行依据是官方同版本对 default 的只读承诺，并明确排除了已知主动写入路径。此限制与实测 EMF 检查完成可以同时成立。

后续有意义的验证是改善佩戴/线圈距离/环境后，再做已授权范围内的有限置信度采样，或在确认脱下静置后单独解释触觉检查。此次不自动重跑、不扫邻网；没有修改原映射、校准、阈值、PLAN 或其他代理脚本。
