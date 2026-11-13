# G1 双臂控制前提的只读核对

更新时间：2026-11-11，实测时间17:12 PST。此记录支持离线 IK 初始化与控制前提审查，不构成实机输出许可。

用户已确认机器人当前**吊装/支撑着**，现场控制模式尚不确定。本轮进行了各一次 `MotionSwitcherClient.CheckMode`、`LocoClient.GetFsmId` 只读查询，以及状态/命令流订阅、DDS 发布者发现和 PC2 进程/网络元数据读取。没有要求用户切模式，没有调用任何模式变更接口，也没有创建双臂/本体命令发布者。

## 本次结论

- 查询成功，返回原文：`code=0`、`{"form":"0","name":"ai"}`。保留此原始结果，不将 `ai`、`form=0` 或 `mode_machine=5` 擅自解释成已确认 Regular mode。
- HG LowState 收到4994个有效样本、0个无效样本；结束时最近样本年龄约0.32ms。14个双臂关节位置/速度均有限，同时记录腿/腰15关节以及 IMU 姿态。
- **实际发现一个 `rt/arm_sdk` writer**，类型 `unitree_hg::msg::dds_::LowCmd_`。因此当前不能声称本集成拥有唯一双臂命令发布权。
- PC2 快照只匹配到 `/unitree/module/master_service/master_service`（PID1221、UID0）；相关 systemd unit 为 `unitree-upgrade.service`，`loaded active running`。没有在该筛选中看到明显 arm/teleop 脚本，不代表其他主机、名称不同或内部服务不存在控制进程。
- 探测进程正常退出0。官方 reader stderr 留有一条 `[Reader] take sample error`，保留原始日志；未出现对应无效样本或过期状态。

完整证据：[g1-mode-readiness-2026-11-11.json](../diagnostics/g1-mode-readiness-2026-11-11.json)。原始 SDK 源码与 SHA256：[g1-mode-source-audit-2026-11-11.json](../diagnostics/g1-mode-source-audit-2026-11-11.json)。

## API 审查与探测范围

实机 SDK2 Python commit 是 `814556d15970dd2ecf1c9984e845ca02ab07e206`，已审查 `motion_switcher_client.py`、`motion_switcher_api.py`、`rpc/client.py`、`rpc/client_base.py`、`rpc/client_stub.py` 和 `core/channel.py`。探测入口 [g1_mode_probe.py](../scripts/g1_mode_probe.py) 在创建 DDS/RPC 客户端前再次校验这6个文件的哈希，不匹配就拒绝发请求。

官方 `MotionSwitcherClient` 构造传入 `enableLease=False`；`Init()` 只记录 API 版本和登记本地调用表。`CheckMode()` 发送服务 `motion_switcher`、API1001、参数 `{}`，解析原始查询响应；改变模式的 `SelectMode` 与 `ReleaseMode` 分别是1002和1003，本次均未调用。[官方客户端](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/comm/motion_switcher/motion_switcher_client.py)、[官方 API ID 定义](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/comm/motion_switcher/motion_switcher_api.py)

脚本额外覆写 `_Call`，仅允许**一次** API1001且参数为空对象，拒绝其他API或第二次调用。`SetTimeout(2.0)` 只设置本地 RPC 等待时间；底层发送与等待响应各使用该超时，本脚本另有整个进程的硬超时。RPC 必须创建它自己的 request writer，但这不是 `rt/arm_sdk` / `rt/lowcmd` 运动发布者；本次准确记录了一个查询请求、零模式变更。[官方 RPC 实现](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/rpc/client_base.py)

本次在 PC2 以 `unitree` 用户使用 tv 环境运行，避免对现有 CycloneDDS 日志文件做权限变更。`--plan` 不导入 DDS/SDK，也不连接网络；正式执行的语义是一次已审查查询：

```bash
python3 scripts/g1_mode_probe.py --plan
# 若脚本在 PC2 上，使用既有 tv 环境：
/home/unitree/miniconda3/envs/tv/bin/python -B g1_mode_probe.py \
  --interface eth0 --seconds 4 --rpc-timeout 2
```

## 当前14关节快照

单位为弧度。下面只展示舍入后的值，离线 IK 应读取 JSON 中原始浮点值：`probe.lowstate.latest.arms`。这些是17:12的历史快照，只能用于离线预览；实际启用前必须重新读取新鲜当前姿态。

| SDK索引 | 关节 | q（rad） |
| --- | --- | --- |
| 15 | 左肩 pitch | 0.107522 |
| 16 | 左肩 roll | 0.056853 |
| 17 | 左肩 yaw | 0.098606 |
| 18 | 左肘 | 1.436190 |
| 19 | 左腕 roll | 0.017197 |
| 20 | 左腕 pitch | 0.245928 |
| 21 | 左腕 yaw | 0.017856 |
| 22 | 右肩 pitch | 0.008880 |
| 23 | 右肩 roll | 0.022746 |
| 24 | 右肩 yaw | -0.224081 |
| 25 | 右肘 | 1.260045 |
| 26 | 右腕 roll | -0.315221 |
| 27 | 右腕 pitch | -0.030272 |
| 28 | 右腕 yaw | -0.430018 |

原始 `mode_machine=5`、`mode_pr=0`；14个臂关节均读到 `mode_raw=1`、`motorstate_raw=0`。IMU rpy约为 `[-0.016010, -0.003428, -0.767498]` rad。这些字段不证明落地支撑、吊具稳定性或可执行运动模式，现场吊装/支撑状态以用户确认和后续现场检查为准。

## 已观察到的命令发布者

使用 CycloneDDS 的 `BuiltinDataReader` 订阅 `BuiltinTopicDcpsPublication`，没有订阅后转发命令、也没有创建 arm writer。共发现132个唯一发布端点，无 discovery API 错误。[官方内置发现 API](https://cyclonedds.io/docs/cyclonedds-python/latest/cyclonedds.builtin.html)

| topic | writer GUID | participant GUID | 原始 instance_state |
| --- | --- | --- | --- |
| `rt/arm_sdk` | `0110828a-fab9-73fe-4893-ec5500000403` | `0110828a-fab9-73fe-4893-ec55000001c1` | `16` |
| `rt/lowcmd` | `01103b75-02e7-c842-7273-d3a600001003` | `01103b75-02e7-c842-7273-d3a6000001c1` | `16` |

两者类型均为 `unitree_hg::msg::dds_::LowCmd_`。发现 writer 说明存在公布的 DDS 写端点，**不单独证明它当时正在发送目标或实际驱动电机**；此记录也没有确定其 IP、进程或用途。动作前应识别其来源和控制关系，不因“PC2 未见 teleop 脚本”就认为没有竞争，更不能据此盲目停止未知官方服务。即便将来没有发现 writer，也不能把短时未发现当成唯一控制权保证。

本轮下一步是让主代理使用该14关节快照做 PICO→右腕/双臂 IK 离线预览，并结合用户的吊装/支撑说明核对现场模式。机器人模式切换、控制权交接和任何目标发布不在本探测脚本范围内。

## 发布者来源进一步定位（17:15 PST）

新增 `--writer-only` 路径只读订阅 `rt/arm_sdk`、`rt/lowstate`、`DCPSPublication` 和 `DCPSParticipant`，**不创建 RPC client、不重复 CheckMode**。5秒窗口内 `rt/arm_sdk` 广告仍存在，但接收命令样本为0，因此无法取得 motor29.q 权重、14目标或命令频率。这里不能下结论“writer没有发送”或“只有广告所以可以接管”；是否匹配、暂时闲置和其他原因尚未区分。

后续纯类 introspection 核实：PC2 的 CycloneDDS Python 为0.10.2，不提供 `get_matched_publications` / `get_matched_publication_data`。因此原始运行报告中的 `matched_publications=[]` 是当时探测器的空占位，**不是证实没有匹配端点**。脚本已改为明确记录 API 可用性；不支持时输出 `null`，不再用空数组混淆两种情况。[可用性核实记录](../diagnostics/g1-mode-writer-api-2026-11-11.json)。此核实不创建DDS实体或发RPC，也没有再订阅一次命令流。

该 writer 对应 participant 的公开属性为：

| 属性 | 观察值 |
| --- | --- |
| `__Hostname` | `Unitree` |
| `__Pid` | `1241` |
| `__ProcessName` | `python3` |
| `__NetworkAddresses` | `udp/239.255.0.1:7400@3,udp/192.168.123.161:37225@3` |

同时在 PC2 eth0 进行7秒只接收 AF_PACKET 观察，仅汇总 RTPS GUID 前缀、IP、UDP端口及计数，没有保存包体。目标 GUID 前缀 `0110828afab973fe4893ec55` 实际来自 **`192.168.123.161`**，收到6个报文，UDP源端口52131、目的为PC2的39464端口。这独立支持来自PC1的网络定位；这些包可能是发现流量，不能据此认作已收到双臂目标。公开属性中的端口37225与本次IP包源端口52131不同，未把它们强行视为同一socket。

PC2 `ss -uapn` 未关联到该远端writer的本地进程，未猜测 PC2 master_service 是其来源，也未停止任何服务。公开PID/进程名属于 DDS 自报属性，尚未通过 PC1 `/proc/1241` 核实。[完整定位证据](../diagnostics/g1-mode-arm-writer-2026-11-11.json)

```bash
# 以下路径只读订阅，零RPC、零控制发布；仅在需要新的有界观测时使用。
/home/unitree/miniconda3/envs/tv/bin/python -B g1_mode_probe.py \
  --interface eth0 --writer-only --seconds 5
```

## 只读 FSM 查询（17:16 PST）

核对实机 `g1/loco/g1_loco_client.py`、`g1_loco_api.py` 与官方固定 commit 的 SHA256 完全一致后，新增 `--fsm-only` 路径，仅允许**一次 service=sport、API7001、参数{}**。没有重复执行 CheckMode。官方 GetFsmId 为7001；7002虽有 GetFsmMode常量，但此版 Python 没有对应公开getter，本次未调用。7101及其他710x/711x属于写接口，均被本路径的白名单拒绝。[官方 LocoClient](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/g1/loco/g1_loco_client.py)、[API定义](https://github.com/unitreerobotics/unitree_sdk2_python/blob/814556d15970dd2ecf1c9984e845ca02ab07e206/unitree_sdk2py/g1/loco/g1_loco_api.py)

实测 `GetFsmId` 返回 `code=0`，响应原文 `{"data":0}`。**FSM ID0是本版官方 SDK `ZeroTorque()` 对应的编号**，`Damp()` 对应编号1；本轮只读取ID，完全没有调用这些动作方法。该结果不等于现场 Regular mode 已确认，也不能仅凭它决定吊装状态下怎样切换控制。完整源码审查、请求和原始响应均保存在 [g1-mode-fsm-2026-11-11.json](../diagnostics/g1-mode-fsm-2026-11-11.json)。

```bash
/home/unitree/miniconda3/envs/tv/bin/python -B g1_mode_probe.py \
  --interface eth0 --fsm-only --rpc-timeout 2
```

## PC1 进程核实的当前边界（17:17 PST）

主代理授权按官方资料核对 PC1 的 SSH 身份并仅尝试有限只读检查。一次 TCP 连接 `192.168.123.161:22` 返回 `Connection refused`，尚未进入 SSH 握手或认证，因此没有保存/替换主机密钥、没有尝试用户名密码、没有读取 `/proc/1241`、没有停止或更改PC1程序。[连接记录](../diagnostics/g1-mode-pc1-ssh-availability-2026-11-11.json)

宇树官方 [XR Teleoperate 网络说明](https://github.com/unitreerobotics/xr_teleoperate/wiki/CycloneDDS-and-UnitreeSDK-%28en%29)确认PC1为`.161`、PC2为`.164`。本次未找到支持把 PC2 的 `unitree/123` 凭据套用于 G1 PC1 的官方资料；不使用第三方文档或其他机器人型号的账号来推断。现阶段可交给宇树/现场维护者核实的具体线索是：**PC1、python3、PID1241、上述participant/writer GUID**。在确认其用途及控制关系之前，继续保持无运动发布的 IK 预览。
